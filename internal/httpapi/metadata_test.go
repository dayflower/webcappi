package httpapi

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

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
			if tc.control == "no-store" && len(api.metadata.entries) != 0 {
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
