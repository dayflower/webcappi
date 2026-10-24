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
	c.Profiles = map[string]capture.Profile{
		"default":   {ProfileDirectory: "Default"},
		"alternate": {ProfileDirectory: "Alternate"},
	}
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

const valid = "/capture?profile=alternate&url=http%3A%2F%2Ffixture%2F"

func TestDefaultProfileSelection(t *testing.T) {
	var calls atomic.Int32
	api, _ := setup(t, func(_ context.Context, req capture.Request) ([]byte, error) {
		calls.Add(1)
		wantDirectory := "Default"
		if req.ProfileName == "alternate" {
			wantDirectory = "Alternate"
		} else if req.ProfileName != "default" {
			t.Errorf("unexpected profile name: %q", req.ProfileName)
		}
		if req.Profile.ProfileDirectory != wantDirectory || req.Width != 800 || req.Height != 480 {
			t.Errorf("unexpected profile or viewport: %+v", req)
		}
		return []byte(req.ProfileName), nil
	})
	const minimal = "/capture?url=http%3A%2F%2Ffixture%2F"
	for _, tc := range []struct {
		path, want string
	}{
		{minimal, "default"},
		{minimal + "&profile=default", "default"},
		{minimal + "&profile=alternate", "alternate"},
	} {
		if w := get(api, tc.path, ""); w.Code != 200 || w.Body.String() != tc.want {
			t.Fatalf("%s: status=%d body=%s", tc.path, w.Code, w.Body)
		}
	}
	if calls.Load() != 2 {
		t.Fatal("omitted and explicit default profiles did not share the cache")
	}
	for _, tc := range []struct {
		path, want string
	}{
		{"/capture", "default"},
		{"/capture?profile=default", "default"},
		{"/capture?profile=alternate", "alternate"},
	} {
		if w := post(api, tc.path, "<p>hello</p>", "text/html", ""); w.Code != 200 || w.Body.String() != tc.want {
			t.Fatalf("%s: status=%d body=%s", tc.path, w.Code, w.Body)
		}
	}
	for _, query := range []string{"&profile=", "&profile=missing", "&profile=default&profile=alternate"} {
		if w := get(api, minimal+query, ""); w.Code != 400 {
			t.Fatalf("%s: status=%d body=%s", query, w.Code, w.Body)
		}
	}
	if calls.Load() != 5 {
		t.Fatal("invalid profiles rendered or HTML requests reused the cache")
	}
}

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
	api.ServeHTTP(w, httptest.NewRequest(http.MethodPut, valid, nil))
	if w.Code != 405 || w.Header().Get("Allow") != "GET, POST" {
		t.Fatal(w)
	}
}

func post(handler http.Handler, path, html, contentType, etag string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(html))
	r.Header.Set("Content-Type", contentType)
	r.Header.Set("If-None-Match", etag)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	return w
}

func TestHTMLCaptureAlwaysRenders(t *testing.T) {
	var calls atomic.Int32
	api, _ := setup(t, func(_ context.Context, req capture.Request) ([]byte, error) {
		calls.Add(1)
		if req.URL != "" || req.HTMLMode != capture.HTMLModeFile || req.Width != 640 || req.Height != 360 || req.ProfileName != "alternate" || req.Profile.ProfileDirectory != "Alternate" || req.ReadySelector != "#ready" {
			t.Errorf("unexpected HTML request: %+v", req)
		}
		return []byte(req.HTML), nil
	})
	const path = "/capture?profile=alternate&width=640&height=360&ready_selector=%23ready"
	const html = "<!doctype html><p id=ready>こんにちは</p>"
	first := post(api, path, html, "text/html; charset=UTF-8", "")
	if first.Code != 200 || first.Body.String() != html || first.Header().Get("Content-Type") != "image/png" || first.Header().Get("Cache-Control") != "no-store" {
		t.Fatal(first)
	}
	for i, etag := range []string{"", "*", `"previous-image"`} {
		fresh := post(api, path, html, "text/html", etag)
		if fresh.Code != 200 || fresh.Body.String() != html || calls.Load() != int32(i+2) || fresh.Header().Get("ETag") != "" || fresh.Header().Get("Age") != "" || fresh.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("POST did not render a fresh image: %v", fresh)
		}
	}
}

func TestHTMLModeQuery(t *testing.T) {
	for _, mode := range []string{capture.HTMLModeFile, capture.HTMLModeLoopback} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			api, _ := setup(t, func(_ context.Context, req capture.Request) ([]byte, error) {
				calls.Add(1)
				if req.HTMLMode != mode || req.HTML != "<p>hello</p>" {
					t.Errorf("HTML mode or body not forwarded: %+v", req)
				}
				return []byte("png"), nil
			})
			for range 2 {
				w := post(api, "/capture?profile=alternate&html_mode="+mode, "<p>hello</p>", "text/html", "*")
				if w.Code != 200 || w.Body.String() != "png" || w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("ETag") != "" {
					t.Fatal(w)
				}
			}
			if calls.Load() != 2 {
				t.Fatal("HTML mode used the cache")
			}
		})
	}
}

func TestHTMLInputValidation(t *testing.T) {
	var calls atomic.Int32
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		calls.Add(1)
		return []byte("png"), nil
	})
	const path = "/capture?profile=alternate"
	for _, tc := range []struct {
		name, path, html, contentType string
		status                        int
	}{
		{"empty", path, "", "text/html", 400},
		{"whitespace", path, " \n\t", "text/html", 400},
		{"invalid UTF-8", path, "\xff", "text/html", 400},
		{"oversized", path, strings.Repeat("x", int(api.cfg.MaxPOSTBytes)+1), "text/html", 413},
		{"missing media type", path, "<p>hello</p>", "", 415},
		{"JSON", path, "{}", "application/json", 415},
		{"charset", path, "<p>hello</p>", "text/html; charset=shift_jis", 415},
		{"malformed media type", path, "hello", "text/html; charset", 415},
		{"URL supplied", path + "&url=http://fixture/", "hello", "text/html", 400},
		{"unknown profile", "/capture?profile=missing", "hello", "text/html", 400},
		{"empty profile", "/capture?profile=", "hello", "text/html", 400},
		{"viewport", path + "&width=0", "hello", "text/html", 400},
		{"repeated", path + "&width=1&width=2", "hello", "text/html", 400},
		{"unknown", path + "&html=x", "hello", "text/html", 400},
		{"empty mode", path + "&html_mode=", "hello", "text/html", 400},
		{"unknown mode", path + "&html_mode=data", "hello", "text/html", 400},
		{"mode casing", path + "&html_mode=FILE", "hello", "text/html", 400},
		{"repeated mode", path + "&html_mode=file&html_mode=loopback", "hello", "text/html", 400},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if w := post(api, tc.path, tc.html, tc.contentType, ""); w.Code != tc.status || (tc.status != 200 && w.Header().Get("Cache-Control") != "no-store") {
				t.Fatalf("status=%d body=%s", w.Code, w.Body)
			}
		})
	}
	if calls.Load() != 0 {
		t.Fatal("invalid HTML request rendered")
	}
	for _, mode := range []string{capture.HTMLModeFile, capture.HTMLModeLoopback} {
		if w := get(api, valid+"&html_mode="+mode, ""); w.Code != 400 {
			t.Fatal("GET accepted HTML-only mode", w)
		}
	}
	if w := post(api, path, strings.Repeat("x", int(api.cfg.MaxPOSTBytes)), "text/html", ""); w.Code != 200 {
		t.Fatal("maximum allowed HTML size rejected", w.Code)
	}
	for _, path := range []string{"/healthz", "/readyz"} {
		if w := post(api, path, "hello", "text/html", ""); w.Code != 405 || w.Header().Get("Allow") != "GET" {
			t.Fatal("probe accepted POST", w)
		}
	}
}

func TestConfiguredPOSTSize(t *testing.T) {
	api, _ := setup(t, func(_ context.Context, req capture.Request) ([]byte, error) {
		return []byte(req.HTML), nil
	})
	for _, limit := range []int64{16, 2 << 20} {
		api.cfg.MaxPOSTBytes = limit
		for _, streamed := range []bool{false, true} {
			for _, size := range []int64{limit, limit + 1} {
				r := httptest.NewRequest(http.MethodPost, "/capture?profile=alternate", strings.NewReader(strings.Repeat("x", int(size))))
				r.Header.Set("Content-Type", "text/html")
				if streamed {
					r.ContentLength = -1
				}
				w := httptest.NewRecorder()
				api.ServeHTTP(w, r)
				want := 200
				if size > limit {
					want = 413
				}
				if w.Code != want {
					t.Fatalf("limit=%d size=%d streamed=%v: %d", limit, size, streamed, w.Code)
				}
			}
		}
	}
}

type failingBody struct{ reads int }

func (b *failingBody) Read([]byte) (int, error) {
	b.reads++
	return 0, errors.New("read failed")
}
func (*failingBody) Close() error { return nil }

func TestHTMLAdmissionAndAuthenticationBeforeBody(t *testing.T) {
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		t.Error("rejected request rendered")
		return nil, nil
	})
	auth, err := NewBearerAuth(&config.Auth{BearerTokenFile: tokenFile(t, "secret")})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name      string
		status    int
		configure func()
	}{
		{"unauthorized", 401, func() {}},
		{"busy", 503, func() { api.requests = make(chan struct{}, 1); api.requests <- struct{}{} }},
		{"draining", 503, func() { api.Drain() }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.configure()
			body := &failingBody{}
			r := httptest.NewRequest(http.MethodPost, "/capture?profile=alternate", body)
			r.Header.Set("Content-Type", "text/html")
			if tc.status != 401 {
				r.Header.Set("Authorization", "Bearer secret")
			}
			w := httptest.NewRecorder()
			auth.Handler(api).ServeHTTP(w, r)
			if w.Code != tc.status || body.reads != 0 {
				t.Fatalf("status=%d body reads=%d", w.Code, body.reads)
			}
		})
	}
}

func TestHTMLBodyReadFailureAndEncoding(t *testing.T) {
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		t.Error("invalid body rendered")
		return nil, nil
	})
	for _, encoding := range []string{"", "gzip"} {
		r := httptest.NewRequest(http.MethodPost, "/capture?profile=alternate", &failingBody{})
		r.Header.Set("Content-Type", "text/html")
		r.Header.Set("Content-Encoding", encoding)
		// Exercise streaming bodies without a known Content-Length.
		r.ContentLength = -1
		w := httptest.NewRecorder()
		api.ServeHTTP(w, r)
		want := 400
		if encoding != "" {
			want = 415
		}
		if w.Code != want {
			t.Fatal(w)
		}
	}
}
