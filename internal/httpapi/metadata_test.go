package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

type getterFunc func(context.Context, capture.Request) (*service.Image, error)

func (f getterFunc) Get(ctx context.Context, req capture.Request) (*service.Image, error) {
	return f(ctx, req)
}

func TestSourceRevalidationPreservesServiceResult(t *testing.T) {
	cfg := config.Defaults()
	cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
	now := time.Date(2026, 10, 25, 0, 0, 0, 0, time.UTC)
	initial := &service.Image{PNG: []byte("png"), ETag: `"initial"`, Source: &capture.SourceResponse{
		URL: "http://fixture/", RequestStarted: now,
		Headers: http.Header{
			"Cache-Control": {"max-age=10"}, "Etag": {"source-v1"},
			"Last-Modified": {"Wed, 21 Oct 2015 07:28:00 GMT"},
			"Date":          {now.Add(-20 * time.Second).Format(http.TimeFormat)}, "Age": {"20"},
		},
	}}
	validated := &service.Image{NotModified: true, Source: &capture.SourceResponse{
		URL: "http://fixture/", RequestStarted: now.Add(30 * time.Second),
		Headers: http.Header{"Cache-Control": {"max-age=40"}, "Etag": {"source-v2"}},
	}}
	wantSource := *validated.Source
	wantSource.Headers = validated.Source.Headers.Clone()
	var calls int
	api := New(cfg, getterFunc(func(_ context.Context, req capture.Request) (*service.Image, error) {
		calls++
		if calls == 1 {
			return initial, nil
		}
		if req.Revalidate == nil || req.Revalidate.Headers.Get("ETag") != "source-v1" {
			t.Fatal("expired matching request lost source validator")
		}
		return validated, nil
	}), nil)
	api.now = func() time.Time { return now }
	const path = "/capture?url=http://fixture/"
	first := get(api, path, "")
	now = now.Add(30 * time.Second)
	response := get(api, path, first.Header().Get("ETag"))
	if response.Code != 304 || response.Body.Len() != 0 || response.Header().Get("ETag") != initial.ETag || response.Header().Get("Age") != "0" || response.Header().Get("Cache-Control") != "private, max-age=40, must-revalidate" {
		t.Fatal("source validation did not merge policy or renew freshness", response)
	}
	if validated.ETag != "" || !reflect.DeepEqual(*validated.Source, wantSource) {
		t.Fatal("HTTP handling modified the service result", validated)
	}
	if initial.Source.Headers.Get("Age") != "20" || initial.Source.Headers.Get("ETag") != "source-v1" {
		t.Fatal("source validation modified the previous service result")
	}
	req, err := api.parse(httptest.NewRequest(http.MethodGet, path, nil))
	if err != nil {
		t.Fatal(err)
	}
	stored := api.metadata.(*metadataCache).get(service.Key(req))
	if stored.source.Headers.Get("ETag") != "source-v2" || stored.source.Headers.Get("Last-Modified") == "" || stored.source.Headers.Get("Date") != "" || stored.source.Headers.Get("Age") != "" {
		t.Fatal("source 304 header merge lost validators or retained old age", stored.source.Headers)
	}
	now = now.Add(5 * time.Second)
	if fresh := get(api, path, initial.ETag); fresh.Code != 304 || fresh.Header().Get("Age") != "5" || calls != 2 {
		t.Fatal("renewed metadata did not bypass service", fresh)
	}
}

func TestConcurrentCaptureMetadataOrder(t *testing.T) {
	cfg := config.Defaults()
	cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
	started, release := make(chan struct{}), make(chan struct{})
	unblock := sync.OnceFunc(func() { close(release) })
	defer unblock()
	var calls atomic.Int32
	api := New(cfg, getterFunc(func(context.Context, capture.Request) (*service.Image, error) {
		if calls.Add(1) == 1 {
			close(started)
			<-release
			return &service.Image{PNG: []byte("old"), ETag: `"old"`}, nil
		}
		return &service.Image{PNG: []byte("new"), ETag: `"new"`}, nil
	}), nil)
	const path = "/capture?url=http://fixture/"
	completed := make(chan *httptest.ResponseRecorder, 1)
	go func() { completed <- get(api, path, "") }()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("first request did not reach service")
	}
	second := get(api, path, "")
	if second.Code != 200 || second.Body.String() != "new" {
		t.Fatal(second)
	}
	unblock()
	select {
	case first := <-completed:
		if first.Code != 200 || first.Body.String() != "old" {
			t.Fatal(first)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("first request did not finish")
	}
	if fresh := get(api, path, second.Header().Get("ETag")); fresh.Code != 304 || calls.Load() != 2 {
		t.Fatal("older completion replaced newer metadata", fresh)
	}
}

func TestSourceRevalidation(t *testing.T) {
	for _, validator := range []string{"ETag", "Last-Modified"} {
		t.Run(validator, func(t *testing.T) {
			cfg := config.Defaults()
			cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
			now := time.Now()
			var renders, checks atomic.Int32
			changed, broken := false, false
			s := service.New(t.Context(), cfg, func(_ context.Context, req capture.Request) (capture.Result, error) {
				if broken {
					return capture.Result{}, errors.New("source unavailable")
				}
				headers := http.Header{"Cache-Control": {"max-age=10"}}
				headers.Set(validator, "source-validator")
				if req.Revalidate != nil {
					checks.Add(1)
					if req.Revalidate.Headers.Get(validator) != "source-validator" {
						t.Fatal("source validator lost")
					}
					if !changed {
						return capture.Result{NotModified: true, Source: &capture.SourceResponse{URL: req.URL, Headers: http.Header{}, RequestStarted: now}}, nil
					}
				}
				n := renders.Add(1)
				return capture.Result{PNG: []byte(fmt.Sprint(n)), Source: &capture.SourceResponse{URL: req.URL, Headers: headers, RequestStarted: now}}, nil
			})
			defer s.Close()
			api := New(cfg, s, nil)
			api.now = func() time.Time { return now }
			path := "/capture?url=http://fixture/"
			first := get(api, path, "")
			tag := first.Header().Get("ETag")
			if first.Code != 200 || first.Header().Get("Cache-Control") != "private, max-age=10, must-revalidate" {
				t.Fatal(first)
			}
			now = now.Add(5 * time.Second)
			fresh := get(api, path, tag)
			if fresh.Code != 304 || fresh.Header().Get("Age") != "5" || checks.Load() != 0 || renders.Load() != 1 {
				t.Fatal("fresh validation touched source")
			}
			now = now.Add(5 * time.Second)
			validated := get(api, path, tag)
			if validated.Code != 304 || validated.Header().Get("Age") != "0" || renders.Load() != 1 || checks.Load() != 1 {
				t.Fatal("source 304 rendered or freshness was not renewed", validated)
			}
			// Repeated 304s must retain the original source validator and policy.
			now = now.Add(10 * time.Second)
			if response := get(api, path, tag); response.Code != 304 || renders.Load() != 1 || checks.Load() != 2 {
				t.Fatal(response)
			}
			now = now.Add(10 * time.Second)
			broken = true
			if response := get(api, path, tag); response.Code != 502 || response.Header().Get("ETag") != "" {
				t.Fatal("source error reused stale validator", response)
			}
			broken = false
			changed = true
			updated := get(api, path, tag)
			if updated.Code != 200 || updated.Header().Get("ETag") == tag || renders.Load() != 2 {
				t.Fatal("changed source did not render", updated)
			}
			before := checks.Load()
			if response := get(api, path, `"unrelated"`); response.Code != 200 || checks.Load() != before || renders.Load() != 3 {
				t.Fatal("mismatched validator did not receive a PNG", response)
			}
		})
	}
}

func TestMetadataPolicies(t *testing.T) {
	for _, tc := range []struct {
		name, control string
		disable       bool
		wantCalls     int32
	}{
		{"fallback", "", false, 1},
		{"no cache", "no-cache", false, 2},
		{"no store", "no-store", false, 2},
		{"disabled", "", true, 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := config.Defaults()
			cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
			if tc.disable {
				cfg.MetadataMaxEntries = 0
			}
			now := time.Now()
			var calls atomic.Int32
			s := service.New(t.Context(), cfg, func(_ context.Context, req capture.Request) (capture.Result, error) {
				calls.Add(1)
				return capture.Result{PNG: []byte("same PNG"), Source: &capture.SourceResponse{URL: req.URL, RequestStarted: now, Headers: http.Header{"Cache-Control": {tc.control}}}}, nil
			})
			defer s.Close()
			api := New(cfg, s, nil)
			api.now = func() time.Time { return now }
			path := "/capture?url=http://fixture/"
			first := get(api, path, "")
			second := get(api, path, first.Header().Get("ETag"))
			if second.Code != 304 || calls.Load() != tc.wantCalls {
				t.Fatal("conditional capture count", second.Code, calls.Load())
			}
			if tc.control == "no-store" && len(api.metadata.(*metadataCache).entries) != 0 {
				t.Fatal("no-store metadata retained")
			}
			if tc.name == "fallback" {
				now = now.Add(time.Minute)
				expired := get(api, path, first.Header().Get("ETag"))
				if expired.Code != 304 || calls.Load() != 2 {
					t.Fatal("expired fallback must capture again before returning the unchanged PNG tag", expired.Code, calls.Load())
				}
			}
		})
	}
}

func TestNoStoreRemovesPreviousMetadata(t *testing.T) {
	cfg := config.Defaults()
	cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
	var calls int
	s := service.New(t.Context(), cfg, func(_ context.Context, req capture.Request) (capture.Result, error) {
		calls++
		control := "max-age=60"
		if calls == 2 {
			control = "no-store"
		}
		return capture.Result{PNG: []byte("png"), Source: &capture.SourceResponse{
			URL: req.URL, RequestStarted: time.Now(), Headers: http.Header{"Cache-Control": {control}},
		}}, nil
	})
	defer s.Close()
	api := New(cfg, s, nil)
	const path = "/capture?url=http://fixture/"
	first := get(api, path, "")
	if first.Code != 200 || len(api.metadata.(*metadataCache).entries) != 1 {
		t.Fatal("initial GET did not populate metadata", first)
	}
	second := get(api, path, "")
	if second.Code != 200 || second.Header().Get("Cache-Control") != "private, no-store" || second.Header().Get("Age") != "" || len(api.metadata.(*metadataCache).entries) != 0 {
		t.Fatal("no-store GET retained previous metadata", second)
	}
	if response := get(api, path, first.Header().Get("ETag")); response.Code != 304 || calls != 3 {
		t.Fatal("no-store left a fresh metadata shortcut", response, calls)
	}
}

func TestUnexpectedSourceNotModified(t *testing.T) {
	cfg := config.Defaults()
	cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
	api := New(cfg, getterFunc(func(context.Context, capture.Request) (*service.Image, error) {
		return &service.Image{NotModified: true}, nil
	}), nil)
	response := get(api, "/capture?url=http://fixture/", "*")
	if response.Code != 502 || response.Body.String() != "{\"error\":\"unexpected source validation response\"}\n" || response.Header().Get("Cache-Control") != "no-store" || response.Header().Get("ETag") != "" || len(api.metadata.(*metadataCache).entries) != 0 {
		t.Fatal("unexpected source 304 was accepted", response)
	}
}

func TestMetadataBoundsAndIsolation(t *testing.T) {
	cache := newMetadataCache(2, 10000)
	store := func(key string, order uint64) {
		cache.put(&metadata{key: key, etag: key, order: order, source: &capture.SourceResponse{Headers: http.Header{"Etag": {"source"}, "Set-Cookie": {"secret"}}}})
	}
	store("one", 1)
	store("two", 2)
	value := cache.get("one")
	if value.source.Headers.Get("Set-Cookie") != "" {
		t.Fatal("unneeded source headers retained")
	}
	store("three", 3)
	if cache.get("two") != nil || cache.get("one") == nil {
		t.Fatal("LRU entry bound failed")
	}
	cache.put(&metadata{key: "one", etag: "new", order: 10})
	store("one", 4)
	if cache.get("one").etag != "new" {
		t.Fatal("late validation overwrote newer capture")
	}
	cache.maxBytes = 512
	cache.put(&metadata{key: "big", etag: strings.Repeat("x", 600)})
	if cache.get("big") != nil {
		t.Fatal("oversized metadata retained")
	}
	store("small", 12)
	if cache.bytes > cache.maxBytes {
		t.Fatal("metadata byte limit exceeded")
	}
	cache.put(&metadata{key: "small", order: 13, policy: responsePolicy{noStore: true}})
	if cache.get("small") != nil {
		t.Fatal("no-store did not invalidate old metadata")
	}
}
