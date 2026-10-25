package httpapi

import (
	"context"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

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
		var bodyErr *responseError
		req, bodyErr = readHTMLRequest(w, r, req, a.cfg.MaxPOSTBytes)
		if bodyErr != nil {
			fail(w, bodyErr.status, bodyErr.message)
			return
		}
	}

	order := a.next.Add(1)
	tags := r.Header.Values("If-None-Match")
	var validation captureValidation
	if r.Method == http.MethodGet {
		validation = a.lookupValidation(req, tags, order)
		if validation.fresh(a.now()) {
			writeValidationHeaders(w, validation.previous, a.now())
			w.WriteHeader(http.StatusNotModified)
			return
		}
		req.Revalidate = validation.revalidationSource()
	}

	img, err := a.service.Get(r.Context(), req)
	if err != nil {
		writeCaptureError(w, err, req.ProfileName)
		return
	}
	if r.Method == http.MethodGet {
		entry, err := a.recordMetadata(validation, img)
		if err != nil {
			fail(w, 502, err.Error())
			return
		}
		writeValidationHeaders(w, entry, entry.validated)
		if matchesETag(tags, entry.etag) {
			w.WriteHeader(http.StatusNotModified)
			return
		}
	}
	writePNG(w, img.PNG)
}
