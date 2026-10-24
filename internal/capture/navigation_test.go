package capture

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestConditionalDocumentNavigation(t *testing.T) {
	b := fixtureBrowser(t)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	var documents, assets atomic.Int32
	var changed atomic.Bool
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/redirect":
			if r.Header.Get("If-None-Match") != "" || r.Header.Get("If-Modified-Since") != "" {
				t.Error("validator sent to redirect URL")
			}
			http.Redirect(w, r, "/page", http.StatusFound)
		case "/asset.css":
			assets.Add(1)
			if r.Header.Get("If-None-Match") != "" || r.Header.Get("If-Modified-Since") != "" {
				t.Error("document validator sent to subresource")
			}
			w.Header().Set("Content-Type", "text/css")
			_, _ = w.Write([]byte("body{background:#fedcba}"))
		case "/page":
			documents.Add(1)
			w.Header().Set("Cache-Control", "max-age=20")
			w.Header().Set("ETag", `"source-v1"`)
			if r.Header.Get("If-None-Match") == `"source-v1"` && !changed.Load() {
				w.WriteHeader(http.StatusNotModified)
				return
			}
			w.Header().Set("Content-Type", "text/html")
			_, _ = w.Write([]byte(`<link rel="stylesheet" href="/asset.css"><p id="ready">Source document</p>`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer fixture.Close()
	r := Renderer{Browser: b, WorkDir: t.TempDir(), MaxProfileBytes: 32 << 20}
	profile, err := r.NewProfile(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer profile.Close()
	req := Request{URL: fixture.URL + "/redirect", Profile: profile.Profile, Width: 320, Height: 200, ReadySelector: "#ready"}
	first, err := r.CaptureWithMetadata(ctx, req)
	if err != nil || len(first.PNG) == 0 || first.Source == nil || first.Source.URL != fixture.URL+"/page" || first.Source.Headers.Get("ETag") != `"source-v1"` {
		t.Fatalf("first capture: %+v, %v", first.Source, err)
	}
	req.Revalidate = first.Source
	req.ReadySelector = "#must-not-be-waited-for"
	req.DelayMS = 20000
	validated, err := r.CaptureWithMetadata(ctx, req)
	if err != nil || !validated.NotModified || len(validated.PNG) != 0 || documents.Load() != 2 || assets.Load() != 1 {
		t.Fatalf("304 did not skip PNG and assets: unchanged=%v docs=%d assets=%d error=%v", validated.NotModified, documents.Load(), assets.Load(), err)
	}
	changed.Store(true)
	req.ReadySelector = "#ready"
	req.DelayMS = 0
	updated, err := r.CaptureWithMetadata(ctx, req)
	if err != nil || updated.NotModified || len(updated.PNG) == 0 || documents.Load() != 3 || assets.Load() != 2 {
		t.Fatalf("changed document did not render: docs=%d assets=%d error=%v", documents.Load(), assets.Load(), err)
	}
}
