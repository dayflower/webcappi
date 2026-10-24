package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

func setup(t *testing.T, render service.RenderFunc) (*API, *service.Service) {
	t.Helper()
	c := config.Defaults()
	c.Profiles = map[string]capture.Profile{"reader": {Revision: "v1"}}
	s := service.New(t.Context(), c, render)
	t.Cleanup(s.Close)
	return New(c, s, nil), s
}
func get(api *API, path, etag string) *httptest.ResponseRecorder {
	r := httptest.NewRequest("GET", path, nil)
	if etag != "" {
		r.Header.Set("If-None-Match", etag)
	}
	w := httptest.NewRecorder()
	api.ServeHTTP(w, r)
	return w
}

const valid = "/capture?profile=reader&url=http%3A%2F%2Ffixture%2F"

func TestPNGCacheAndConditionalFreshness(t *testing.T) {
	var calls atomic.Int32
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		n := calls.Add(1)
		return []byte{137, 'P', 'N', 'G', byte(n)}, nil
	})
	first := get(api, valid, "")
	if first.Code != 200 || first.Header().Get("Content-Type") != "image/png" {
		t.Fatal(first)
	}
	tag := first.Header().Get("ETag")
	time.Sleep(1100 * time.Millisecond)
	cached := get(api, valid, "W/\"unrelated\", W/"+tag)
	if cached.Code != 304 || calls.Load() != 1 {
		t.Fatal("conditional cache failed")
	}
	age, _ := strconv.Atoi(cached.Header().Get("Age"))
	if age < 1 {
		t.Fatal("freshness extended")
	}
	// Expire by waiting with a deliberately short TTL in a separate service.
	c := api.cfg
	c.CacheTTLSeconds = 1
	s := service.New(t.Context(), c, func(context.Context, capture.Request) ([]byte, error) { return []byte{byte(calls.Add(1))}, nil })
	defer s.Close()
	short := New(c, s, nil)
	old := get(short, valid, "")
	time.Sleep(1100 * time.Millisecond)
	fresh := get(short, valid, old.Header().Get("ETag"))
	if fresh.Code != 200 || fresh.Header().Get("ETag") == old.Header().Get("ETag") {
		t.Fatal("expired PNG incorrectly returned 304")
	}
}

func TestInvalidInputAndErrors(t *testing.T) {
	var calls atomic.Int32
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		calls.Add(1)
		return nil, context.DeadlineExceeded
	})
	for _, suffix := range []string{"&width=0", "&width=-1", "&height=90000", "&width=4096&height=4096", "&width=no", "&width=1&width=2", "&unknown=x", "&profile=other", "&ready_selector=" + strings.Repeat("a", 1025)} {
		if w := get(api, valid+suffix, ""); w.Code != 400 {
			t.Fatalf("%s: %d", suffix, w.Code)
		}
	}
	for _, url := range []string{"file:///etc/passwd", "javascript:alert(1)", "http://user:pass@fixture/", "http://fixture:99999/", "http://%zz"} {
		r := httptest.NewRequest("GET", valid, nil)
		q := r.URL.Query()
		q.Set("url", url)
		r.URL.RawQuery = q.Encode()
		w := httptest.NewRecorder()
		api.ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatalf("URL %s: %d", url, w.Code)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid request rendered")
	}
	if w := get(api, valid, ""); w.Code != 504 || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal(w)
	}
	api.service = errorGetter{errors.New("navigation failed")}
	if w := get(api, valid, ""); w.Code != 502 {
		t.Fatal(w)
	}
	for _, err := range []error{service.ErrBusy, service.ErrClosed, context.Canceled} {
		api.service = errorGetter{err}
		if w := get(api, valid, ""); w.Code != 503 {
			t.Fatal(w)
		}
	}
}

type errorGetter struct{ err error }

func (e errorGetter) Get(context.Context, capture.Request) (*service.Image, error) { return nil, e.err }

func TestHealthReadinessAndAdmission(t *testing.T) {
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) { return []byte("png"), nil })
	if get(api, "/healthz", "").Code != 200 || get(api, "/readyz", "").Code != 200 {
		t.Fatal("health failed")
	}
	api.check = func(context.Context) error { return errors.New("missing template") }
	if get(api, "/readyz", "").Code != 503 {
		t.Fatal("unready dependency")
	}
	api.requests = make(chan struct{}, 1)
	api.requests <- struct{}{}
	if get(api, valid, "").Code != 503 {
		t.Fatal("request bound missing")
	}
	<-api.requests
	api.Drain()
	if get(api, "/readyz", "").Code != 503 || get(api, "/healthz", "").Code != 200 || get(api, valid, "").Code != 503 {
		t.Fatal("drain failed")
	}
	w := httptest.NewRecorder()
	api.ServeHTTP(w, httptest.NewRequest(http.MethodPost, valid, nil))
	if w.Code != 405 || w.Header().Get("Allow") != "GET" {
		t.Fatal(w)
	}
}
