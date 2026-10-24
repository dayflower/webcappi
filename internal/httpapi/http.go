package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/captureinput"
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
	metadata *metadataCache
	next     atomic.Uint64
	now      func() time.Time
}

func New(c config.Config, s Getter, check func(context.Context) error) *API {
	return &API{cfg: c, service: s, requests: make(chan struct{}, c.MaxRequests), check: check, metadata: newMetadataCache(c.MetadataMaxEntries, c.MetadataMaxBytes), now: time.Now}
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
	case "/":
		a.index(w)
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
		contentType := r.Header.Get("Content-Type")
		mediaType, params, err := mime.ParseMediaType(contentType)
		if err != nil || mediaType != "text/html" {
			fail(w, 415, "Content-Type must be text/html")
			return
		}
		if req.HTMLMode == capture.HTMLModeFile && params["charset"] != "" && !strings.EqualFold(params["charset"], "utf-8") {
			fail(w, 415, "file mode requires UTF-8 encoding")
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
		if len(strings.TrimSpace(string(body))) == 0 {
			fail(w, 400, "HTML must be non-empty")
			return
		}
		if req.HTMLMode == capture.HTMLModeFile && !utf8.Valid(body) {
			fail(w, 400, "HTML must be UTF-8 in file mode")
			return
		}
		req.HTML = string(body)
		req.HTMLContentType = contentType
	}
	var previous *metadata
	order := a.next.Add(1)
	key := service.Key(req)
	if r.Method == http.MethodGet {
		previous = a.metadata.get(key)
		if previous != nil && matchesETag(r.Header.Values("If-None-Match"), previous.etag) {
			if a.now().Before(previous.expires()) {
				writeValidationHeaders(w, previous, a.now())
				w.WriteHeader(http.StatusNotModified)
				return
			}
			req.Revalidate = previous.source
		}
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
		now := a.now()
		source := img.Source
		if img.NotModified {
			if previous == nil {
				fail(w, 502, "unexpected source validation response")
				return
			}
			img.ETag = previous.etag
			source = mergeSource(previous.source, img.Source)
		}
		policy := cachePolicy(a.cfg.MaxAgeSeconds, source, now)
		entry := &metadata{key: key, etag: img.ETag, source: source, validated: now, policy: policy, order: order}
		a.metadata.put(entry)
		writeValidationHeaders(w, entry, now)
		if matchesETag(r.Header.Values("If-None-Match"), img.ETag) {
			w.WriteHeader(http.StatusNotModified)
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
	allowed := map[string]bool{"url": true, "width": true, "height": true, "output_width": true, "output_height": true, "profile": true, "ready_selector": true, "delay_ms": true}
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
	in := captureinput.Input{ReadySelector: q.Get("ready_selector")}
	if r.Method == http.MethodGet {
		u := q.Get("url")
		in.URL = &u
	}
	for key, dst := range map[string]**int{"width": &in.Width, "height": &in.Height, "output_width": &in.OutputWidth, "output_height": &in.OutputHeight} {
		if values, ok := q[key]; ok {
			n, err := strconv.Atoi(values[0])
			if err != nil {
				// Defer rejection to the shared validator so URL errors still win.
				n = 0
			}
			*dst = &n
		}
	}
	if values, ok := q["delay_ms"]; ok {
		n, err := strconv.Atoi(values[0])
		if err != nil {
			n = -1
		}
		in.DelayMS = n
	}
	if values, ok := q["profile"]; ok {
		in.ProfileName = &values[0]
	}
	validated, err := captureinput.Normalize(in, captureinput.Options{
		MaxWidth: a.cfg.MaxWidth, MaxHeight: a.cfg.MaxHeight, MaxPixels: a.cfg.MaxPixels,
		DefaultProfileName: config.DefaultProfileName, Profiles: a.cfg.Profiles,
	})
	validated.HTMLMode = req.HTMLMode
	return validated, err
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
