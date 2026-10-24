package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

type Getter interface {
	Get(context.Context, capture.Request) (*service.Image, error)
}

type API struct {
	cfg      config.Config
	service  Getter
	requests chan struct{}
	draining atomic.Bool
	check    func(context.Context) error
}

func New(c config.Config, s Getter, check func(context.Context) error) *API {
	return &API{cfg: c, service: s, requests: make(chan struct{}, c.MaxRequests), check: check}
}
func (a *API) Drain() { a.draining.Store(true) }

func (a *API) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	allow := "GET"
	if r.URL.Path == "/capture" {
		allow = "GET, POST"
	}
	if r.Method != http.MethodGet && !(r.URL.Path == "/capture" && r.Method == http.MethodPost) {
		w.Header().Set("Allow", allow)
		fail(w, 405, "method not allowed")
		return
	}
	switch r.URL.Path {
	case "/healthz":
		w.WriteHeader(200)
		fmt.Fprintln(w, "ok")
	case "/readyz":
		if a.draining.Load() {
			fail(w, 503, "shutting down")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if a.check != nil {
			if err := a.check(ctx); err != nil {
				fail(w, 503, "dependencies unavailable")
				return
			}
		}
		w.WriteHeader(200)
		fmt.Fprintln(w, "ready")
	case "/capture":
		a.capture(w, r)
	default:
		fail(w, 404, "not found")
	}
}

func (a *API) capture(w http.ResponseWriter, r *http.Request) {
	if a.draining.Load() {
		fail(w, 503, "shutting down")
		return
	}
	req, err := a.parse(r)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	select {
	case a.requests <- struct{}{}:
		defer func() { <-a.requests }()
	default:
		w.Header().Set("Retry-After", "1")
		fail(w, 503, "too many capture requests")
		return
	}
	if r.Method == http.MethodPost {
		mediaType, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || mediaType != "text/html" || (params["charset"] != "" && !strings.EqualFold(params["charset"], "utf-8")) {
			fail(w, 415, "Content-Type must be text/html with UTF-8 encoding")
			return
		}
		if encoding := r.Header.Get("Content-Encoding"); encoding != "" && !strings.EqualFold(encoding, "identity") {
			fail(w, 415, "Content-Encoding is not supported")
			return
		}
		// Admission precedes body buffering so uploads share the handler bound.
		r.Body = http.MaxBytesReader(w, r.Body, a.cfg.MaxPOSTBytes)
		body, err := io.ReadAll(r.Body)
		if err != nil {
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				fail(w, 413, fmt.Sprintf("HTML must be <=%d bytes", a.cfg.MaxPOSTBytes))
			} else {
				fail(w, 400, "could not read HTML body")
			}
			return
		}
		if !utf8.Valid(body) || len(strings.TrimSpace(string(body))) == 0 {
			fail(w, 400, "HTML must be non-empty UTF-8 text")
			return
		}
		req.HTML = string(body)
	}
	img, err := a.service.Get(r.Context(), req)
	if err != nil {
		switch {
		case errors.Is(err, service.ErrBusy), errors.Is(err, service.ErrClosed):
			w.Header().Set("Retry-After", "1")
			fail(w, 503, err.Error())
		case errors.Is(err, context.DeadlineExceeded):
			fail(w, 504, "capture timed out")
		case errors.Is(err, context.Canceled):
			fail(w, 503, "capture canceled")
		default:
			slog.Warn("capture failed", "profile", req.ProfileName, "error", err)
			fail(w, 502, "page capture failed")
		}
		return
	}
	if r.Method == http.MethodGet {
		w.Header().Set("ETag", img.ETag)
		// Reusing an image never resets its freshness. Rounding age upward avoids
		// granting a partial extra second of freshness on each cache hit.
		if a.cfg.CacheTTLSeconds > 0 {
			w.Header().Set("Cache-Control", fmt.Sprintf("private, max-age=%d, must-revalidate", a.cfg.CacheTTLSeconds))
			age := int(math.Ceil(time.Since(img.Created).Seconds()))
			if age < 0 {
				age = 0
			}
			w.Header().Set("Age", strconv.Itoa(age))
		} else {
			w.Header().Set("Cache-Control", "private, no-cache")
		}
		if matchesETag(r.Header.Values("If-None-Match"), img.ETag) {
			w.WriteHeader(304)
			return
		}
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Length", strconv.Itoa(len(img.PNG)))
	w.WriteHeader(200)
	_, _ = w.Write(img.PNG)
}

func (a *API) parse(r *http.Request) (capture.Request, error) {
	var req capture.Request
	if len(r.URL.RawQuery) > 16*1024 {
		return req, errors.New("query too long")
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return req, errors.New("invalid query encoding")
	}
	allowed := map[string]bool{"url": true, "width": true, "height": true, "profile": true, "ready_selector": true}
	if r.Method == http.MethodPost {
		delete(allowed, "url")
		allowed["html_mode"] = true
	}
	for key, values := range q {
		if !allowed[key] || len(values) != 1 {
			return req, fmt.Errorf("unknown or repeated parameter %q", key)
		}
	}
	if r.Method == http.MethodPost {
		req.HTMLMode = capture.HTMLModeFile
		if values, ok := q["html_mode"]; ok {
			if values[0] != capture.HTMLModeFile && values[0] != capture.HTMLModeLoopback {
				return req, errors.New("html_mode must be file or loopback")
			}
			req.HTMLMode = values[0]
		}
	}
	if r.Method == http.MethodGet {
		req.URL = q.Get("url")
		if len(req.URL) == 0 || len(req.URL) > 8192 {
			return req, errors.New("url is required and must be <=8192 bytes")
		}
		u, err := url.Parse(req.URL)
		if err != nil || u.Host == "" || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || strings.ContainsAny(req.URL, "\x00\r\n") {
			return req, errors.New("url must be an absolute http(s) URL without credentials")
		}
		if port := u.Port(); port != "" {
			n, e := strconv.Atoi(port)
			if e != nil || n < 1 || n > 65535 {
				return req, errors.New("invalid URL port")
			}
		}
	}
	req.Width, req.Height = 800, 480
	for key, dst := range map[string]*int{"width": &req.Width, "height": &req.Height} {
		if values, ok := q[key]; ok {
			n, e := strconv.Atoi(values[0])
			if e != nil || n < 1 {
				return req, fmt.Errorf("%s must be a positive integer", key)
			}
			*dst = n
		}
	}
	if req.Width > a.cfg.MaxWidth || req.Height > a.cfg.MaxHeight || int64(req.Width)*int64(req.Height) > a.cfg.MaxPixels {
		return req, errors.New("viewport exceeds configured limits")
	}
	req.ProfileName = config.DefaultProfileName
	if values, ok := q["profile"]; ok {
		req.ProfileName = values[0]
	}
	p, ok := a.cfg.Profiles[req.ProfileName]
	if !ok {
		return req, errors.New("unknown profile")
	}
	req.Profile = p
	req.ReadySelector = q.Get("ready_selector")
	if len(req.ReadySelector) > 1024 {
		return req, errors.New("ready_selector must be <=1024 bytes")
	}
	return req, nil
}

func matchesETag(headers []string, etag string) bool {
	for _, h := range headers {
		for _, item := range strings.Split(h, ",") {
			item = strings.TrimSpace(item)
			if item == "*" || strings.TrimPrefix(item, "W/") == etag {
				return true
			}
		}
	}
	return false
}
func fail(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
}
