package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

func redisConfig(t *testing.T) config.Config {
	t.Helper()
	address := os.Getenv("WEBCAPPI_REDIS_TEST_ADDR")
	if address == "" {
		t.Skip("set WEBCAPPI_REDIS_TEST_ADDR or run make redis-test for real Redis integration tests")
	}
	var id [12]byte
	if _, err := rand.Read(id[:]); err != nil {
		t.Fatal(err)
	}
	c := config.Defaults()
	c.Cache = config.Cache{Backend: "redis", Redis: &config.Redis{Address: address, Namespace: "test-" + hex.EncodeToString(id[:]), Generation: "v1"}}
	c.Cache.Redis.PasswordFile = os.Getenv("WEBCAPPI_REDIS_TEST_PASSWORD_FILE")
	c.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
	return c
}

func testRedis(t *testing.T, c config.Config) *Cache {
	t.Helper()
	s, err := OpenCache(t.Context(), c)
	if err != nil {
		t.Fatal(err)
	}
	r := s.store.(*redisMetadata)
	t.Cleanup(func() { _ = r.client.Del(context.Background(), r.keys...).Err(); _ = s.Close() })
	return s
}

func redisAPI(t *testing.T, c config.Config, fn func(context.Context, capture.Request) (capture.Result, error)) (*API, *redisMetadata) {
	t.Helper()
	cache := testRedis(t, c)
	s := service.New(t.Context(), c, fn)
	t.Cleanup(s.Close)
	return New(c, s, nil, cache), cache.store.(*redisMetadata)
}

func TestRedisSharedConditionalGET(t *testing.T) {
	c := redisConfig(t)
	var captures atomic.Int32
	render := func(context.Context, capture.Request) (capture.Result, error) {
		captures.Add(1)
		return capture.Result{PNG: []byte("same PNG")}, nil
	}
	a, store := redisAPI(t, c, render)
	b, _ := redisAPI(t, c, render)
	// Wall-clock differences between Pods must not change shared freshness.
	a.now = func() time.Time { return time.Now().Add(-24 * time.Hour) }
	b.now = func() time.Time { return time.Now().Add(24 * time.Hour) }
	path := "/capture?url=http://fixture/"
	first := get(a, path, "")
	tag := first.Header().Get("ETag")
	if first.Code != 200 || tag == "" {
		t.Fatal(first)
	}
	if next := get(b, path, tag); next.Code != 304 || captures.Load() != 1 {
		t.Fatalf("shared metadata missed: %d, captures=%d", next.Code, captures.Load())
	}
	// Redis persists independently of an application connection/process lifetime.
	if err := store.close(); err != nil {
		t.Fatal(err)
	}
	if next := get(b, path, tag); next.Code != 304 || captures.Load() != 1 {
		t.Fatal("closing the first instance lost shared metadata")
	}
	if next := get(b, path, ""); next.Code != 200 || captures.Load() != 2 {
		t.Fatal("unconditional GET reused a PNG")
	}
	if next := post(b, "/capture", "<p>html</p>", "text/html", tag); next.Code != 200 || captures.Load() != 3 || next.Header().Get("ETag") != "" {
		t.Fatal("POST used metadata")
	}
}

func TestRedisExpiredSourceRevalidation(t *testing.T) {
	c := redisConfig(t)
	c.MaxAgeSeconds = 0
	var captures, checks atomic.Int32
	render := func(_ context.Context, req capture.Request) (capture.Result, error) {
		if req.Revalidate != nil {
			checks.Add(1)
			if req.Revalidate.Headers.Get("ETag") != `"source-v1"` {
				t.Error("source validator lost")
			}
			return capture.Result{NotModified: true, Source: &capture.SourceResponse{URL: req.URL, RequestStarted: time.Now(), Headers: http.Header{"Etag": {`"source-v2"`}}}}, nil
		}
		captures.Add(1)
		return capture.Result{PNG: []byte("png"), Source: &capture.SourceResponse{URL: req.URL, RequestStarted: time.Now(), Headers: http.Header{"Etag": {`"source-v1"`}, "Last-Modified": {"Wed, 21 Oct 2015 07:28:00 GMT"}, "Set-Cookie": {"secret"}}}}, nil
	}
	a, _ := redisAPI(t, c, render)
	b, store := redisAPI(t, c, render)
	path := "/capture?url=http://fixture/"
	first := get(a, path, "")
	if next := get(b, path, first.Header().Get("ETag")); next.Code != 304 || captures.Load() != 1 || checks.Load() != 1 {
		t.Fatalf("expired shared metadata: %d", next.Code)
	}
	snapshot, err := store.begin(t.Context(), service.Key(capture.Request{URL: "http://fixture/", Width: 1280, Height: 720, ProfileName: "default"}), time.Now())
	if err != nil || snapshot.previous == nil {
		t.Fatal("metadata unavailable", err)
	}
	headers := snapshot.previous.source.Headers
	if headers.Get("ETag") != `"source-v2"` || headers.Get("Last-Modified") == "" || headers.Get("Set-Cookie") != "" {
		t.Fatal("merged or filtered headers incorrect", headers)
	}
}

func TestRedisFencesAndCapacity(t *testing.T) {
	c := redisConfig(t)
	c.MetadataMaxEntries = 2
	store := testRedis(t, c).store.(*redisMetadata)
	other := testRedis(t, c).store.(*redisMetadata)
	begin := func(s *redisMetadata, key string) metadataSnapshot {
		t.Helper()
		v, err := s.begin(t.Context(), key, time.Now())
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	commit := func(s *redisMetadata, ticket metadataSnapshot, tag string, noStore bool) {
		t.Helper()
		_, err := s.commit(t.Context(), ticket, &metadata{key: ticket.key, etag: tag, validated: time.Now(), policy: responsePolicy{maxAge: 60, noStore: noStore}})
		if err != nil {
			t.Fatal(err)
		}
	}
	old := begin(store, "one")
	newer := begin(other, "one")
	commit(other, newer, `"new"`, false)
	commit(store, old, `"old"`, false)
	if got := begin(store, "one").previous; got == nil || got.etag != `"new"` {
		t.Fatal("old completion overwrote a newer result", got)
	}
	old = begin(store, "one")
	newer = begin(other, "one")
	commit(other, newer, `"private"`, true)
	commit(store, old, `"old"`, false)
	if got := begin(store, "one").previous; got != nil {
		t.Fatal("no-store fence allowed metadata resurrection")
	}
	commit(store, begin(store, "two"), `"two"`, false)
	_ = begin(store, "one") // Touch one; two must be evicted next.
	commit(store, begin(store, "three"), `"three"`, false)
	if got := begin(store, "two").previous; got != nil {
		t.Fatal("LRU entry bound failed")
	}
	count, err := store.client.ZCard(t.Context(), store.keys[1]).Result()
	if err != nil || count > 2 {
		t.Fatal("entry budget exceeded", count, err)
	}
	budget, err := store.client.HGet(t.Context(), store.keys[0], "_bytes").Int64()
	if err != nil || budget > c.MetadataMaxBytes {
		t.Fatal("byte budget exceeded", budget, err)
	}
	// A fenced request may not recreate a record after namespace loss/Redis restart.
	ticket := begin(store, "lost")
	if err := store.client.Del(t.Context(), store.keys...).Err(); err != nil {
		t.Fatal(err)
	}
	_ = begin(other, "lost") // Recreated records must belong to a different epoch.
	commit(other, ticket, `"lost"`, false)
	if got := begin(store, "lost").previous; got != nil {
		t.Fatal("lost fence was reused")
	}
	// An interrupted script must not expose partially updated indexes or budgets.
	ticket = begin(store, "interrupted")
	commit(store, ticket, `"saved"`, false)
	if err := store.client.HSet(t.Context(), store.keys[0], "_dirty", 1).Err(); err != nil {
		t.Fatal(err)
	}
	if got := begin(other, "interrupted").previous; got != nil {
		t.Fatal("incomplete namespace was reused")
	}
}

func TestRedisRetentionAndAge(t *testing.T) {
	c := redisConfig(t)
	store := testRedis(t, c).store.(*redisMetadata)
	ticket, err := store.begin(t.Context(), "one", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.commit(t.Context(), ticket, &metadata{key: "one", etag: `"tag"`, validated: time.Now(), policy: responsePolicy{maxAge: 60}})
	if err != nil {
		t.Fatal(err)
	}
	// Move Redis metadata into the past without sleeping or changing the host clock.
	raw, err := store.client.HGet(t.Context(), store.keys[0], "one").Result()
	if err != nil {
		t.Fatal(err)
	}
	var record map[string]any
	if err := json.Unmarshal([]byte(raw), &record); err != nil {
		t.Fatal(err)
	}
	record["validated"] = record["validated"].(float64) - 5000
	b, _ := json.Marshal(record)
	if err := store.client.HSet(t.Context(), store.keys[0], "one", string(b)).Err(); err != nil {
		t.Fatal(err)
	}
	first, err := store.begin(t.Context(), "one", time.Now())
	if err != nil || first.previous == nil {
		t.Fatal("metadata lost", err)
	}
	second, err := store.begin(t.Context(), "one", time.Now())
	if err != nil || second.previous == nil || !second.previous.validated.Equal(first.previous.validated) {
		t.Fatal("lookup renewed validation time", err)
	}
	if age := second.currentTime().Sub(second.previous.validated); age < 5*time.Second {
		t.Fatal("Age reset", age)
	}
	record["retain"] = float64(time.Now().Add(-time.Minute).UnixMilli())
	b, _ = json.Marshal(record)
	if err := store.client.HSet(t.Context(), store.keys[0], "one", string(b)).Err(); err != nil {
		t.Fatal(err)
	}
	if next, err := store.begin(t.Context(), "one", time.Now()); err != nil || next.previous != nil {
		t.Fatal("retention expiry reused metadata", err)
	}
}

func TestRedisFailureAndRecovery(t *testing.T) {
	c := redisConfig(t)
	var captures atomic.Int32
	api, store := redisAPI(t, c, func(context.Context, capture.Request) (capture.Result, error) {
		captures.Add(1)
		return capture.Result{PNG: []byte("png")}, nil
	})
	path := "/capture?url=http://fixture/"
	first := get(api, path, "")
	if first.Code != 200 {
		t.Fatal(first)
	}
	if err := store.client.ScriptFlush(t.Context()).Err(); err != nil {
		t.Fatal(err)
	}
	if next := get(api, path, first.Header().Get("ETag")); next.Code != 304 {
		t.Fatal("NOSCRIPT recovery failed", next.Code)
	}
	if err := store.close(); err != nil {
		t.Fatal(err)
	}
	if next := get(api, path, first.Header().Get("ETag")); next.Code != 503 || next.Header().Get("ETag") != "" || captures.Load() != 1 {
		t.Fatal("cache failure fell back to local metadata", next)
	}
	if ready := get(api, "/readyz", ""); ready.Code != 503 {
		t.Fatal("Redis failure did not affect readiness")
	}
	if health := get(api, "/healthz", ""); health.Code != 200 {
		t.Fatal("Redis failure affected liveness")
	}
	if next := post(api, "/capture", "<p>html</p>", "text/html", ""); next.Code != 200 {
		t.Fatal("POST depended on Redis")
	}
	cache := testRedis(t, c)
	recovered := New(c, api.service, nil, cache)
	if next := get(recovered, path, first.Header().Get("ETag")); next.Code != 304 || captures.Load() != 2 {
		t.Fatal("reconnected instance lost persisted metadata", next.Code)
	}
}

func TestRedisScopeIsolation(t *testing.T) {
	c := config.Defaults()
	c.Cache = config.Cache{Backend: "redis", Redis: &config.Redis{Address: "redis:6379", Namespace: "production", Generation: "v1"}}
	base := redisScope(c)
	for _, change := range []func(*config.Config){
		func(c *config.Config) { c.MaxWidth++ },
		func(c *config.Config) { c.MaxHeight++ },
		func(c *config.Config) { c.MaxPixels++ },
	} {
		changed := c
		change(&changed)
		if redisScope(changed) == base {
			t.Fatal("full-page limits did not isolate metadata")
		}
	}
	c.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default", UserDataDir: "/work/random-template"}}
	if redisScope(c) != base {
		t.Fatal("automatic profile path changed scope")
	}
	c.Cache.Redis.Generation = "v2"
	if redisScope(c) == base {
		t.Fatal("generation did not isolate metadata")
	}
	c.Cache.Redis.Generation = "v1"
	c.ResizeAlgorithm = capture.ResizeNearestNeighbor
	if redisScope(c) == base {
		t.Fatal("renderer settings did not isolate metadata")
	}
}

func TestRedisConnectionDeadline(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	stop := make(chan struct{})
	defer close(stop)
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() { defer conn.Close(); <-stop }()
		}
	}()
	c := config.Defaults()
	c.Cache = config.Cache{Backend: "redis", Redis: &config.Redis{Address: listener.Addr().String(), Namespace: "timeout", Generation: "v1", TimeoutMS: 100}}
	started := time.Now()
	if cache, err := OpenCache(t.Context(), c); err == nil {
		_ = cache.Close()
		t.Fatal("unresponsive Redis accepted")
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatal("Redis deadline exceeded", elapsed)
	}
}

func TestRedisByteBoundAndMalformedValue(t *testing.T) {
	c := redisConfig(t)
	c.MetadataMaxBytes = 2048
	cache := testRedis(t, c)
	s := cache.store.(*redisMetadata)
	ticket, err := s.begin(t.Context(), "one", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.commit(t.Context(), ticket, &metadata{key: "one", etag: strings.Repeat("x", 4096), validated: time.Now(), policy: responsePolicy{maxAge: 60}})
	if err != nil {
		t.Fatal(err)
	}
	if next, err := s.begin(t.Context(), "one", time.Now()); err != nil || next.previous != nil {
		t.Fatal("oversized value retained", err)
	}
	// Keep a valid envelope but make its metadata format incompatible.
	raw, err := s.client.HGet(t.Context(), s.keys[0], "one").Result()
	if err != nil {
		t.Fatal(err)
	}
	var record map[string]any
	if err := json.Unmarshal([]byte(raw), &record); err != nil {
		t.Fatal(err)
	}
	record["value"] = `{"version":999,"etag":"tag"}`
	record["validated"] = float64(time.Now().UnixMilli())
	record["retain"] = float64(time.Now().Add(time.Hour).UnixMilli())
	b, _ := json.Marshal(record)
	if err := s.client.HSet(t.Context(), s.keys[0], "one", string(b)).Err(); err != nil {
		t.Fatal(err)
	}
	if _, err := s.begin(t.Context(), "one", time.Now()); err == nil {
		t.Fatal("incompatible metadata accepted")
	}
	// Disabling capacity never retains metadata or fencing records.
	c.MetadataMaxEntries = 0
	disabled := testRedis(t, c).store.(*redisMetadata)
	ticket, err = disabled.begin(t.Context(), "one", time.Now())
	if err != nil || ticket.token != "" {
		t.Fatal("disabled store allocated a fence", err)
	}
	if n, err := disabled.client.Exists(t.Context(), disabled.keys...).Result(); err != nil || n != 0 {
		t.Fatal("disabled store retained keys", n, err)
	}
}

func TestMemoryNoStoreFencesActiveCapture(t *testing.T) {
	c := newMetadataCache(2, 10000)
	older, _ := c.begin(t.Context(), "one", time.Now())
	newer, _ := c.begin(t.Context(), "one", time.Now())
	defer c.finish(older)
	_, _ = c.commit(t.Context(), newer, &metadata{key: "one", order: newer.order, policy: responsePolicy{noStore: true}})
	c.finish(newer)
	_, _ = c.commit(t.Context(), older, &metadata{key: "one", order: older.order, etag: "old"})
	if c.get("one") != nil {
		t.Fatal("older capture resurrected metadata after no-store")
	}
}

func TestRedisConcurrentHTTPUpdates(t *testing.T) {
	for _, noStore := range []bool{false, true} {
		name := "newer result"
		if noStore {
			name = "no-store"
		}
		t.Run(name, func(t *testing.T) {
			c := redisConfig(t)
			started, release := make(chan struct{}), make(chan struct{})
			var calls atomic.Int32
			render := func(ctx context.Context, _ capture.Request) (capture.Result, error) {
				n := calls.Add(1)
				if n == 1 {
					close(started)
					select {
					case <-release:
					case <-ctx.Done():
						return capture.Result{}, ctx.Err()
					}
					return capture.Result{PNG: []byte("old")}, nil
				}
				result := capture.Result{PNG: []byte("new")}
				if n == 2 && noStore {
					result.Source = &capture.SourceResponse{RequestStarted: time.Now(), Headers: http.Header{"Cache-Control": {"no-store"}}}
				}
				return result, nil
			}
			a, _ := redisAPI(t, c, render)
			b, _ := redisAPI(t, c, render)
			path := "/capture?url=http://fixture/"
			complete := make(chan *httptest.ResponseRecorder, 1)
			go func() { complete <- get(a, path, "") }()
			select {
			case <-started:
			case <-time.After(5 * time.Second):
				t.Fatal("old request did not start")
			}
			newer := get(b, path, "")
			close(release)
			select {
			case older := <-complete:
				if older.Code != 200 {
					t.Fatal(older)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("old request did not finish")
			}
			if newer.Code != 200 {
				t.Fatal(newer)
			}
			next := get(b, path, newer.Header().Get("ETag"))
			want := int32(2)
			if noStore {
				want = 3
			}
			if next.Code != 304 || calls.Load() != want {
				t.Fatalf("newer metadata state lost: HTTP %d, captures %d (want %d)", next.Code, calls.Load(), want)
			}
		})
	}
}

func TestRedisCommitFailure(t *testing.T) {
	c := redisConfig(t)
	cache := testRedis(t, c)
	s := service.New(t.Context(), c, func(context.Context, capture.Request) (capture.Result, error) {
		_ = cache.Close()
		return capture.Result{PNG: []byte("png")}, nil
	})
	defer s.Close()
	api := New(c, s, nil, cache)
	if response := get(api, "/capture?url=http://fixture/", ""); response.Code != 503 || response.Header().Get("ETag") != "" || response.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("commit failure sent a successful response", response)
	}
}

func TestRedisByteBudgetEviction(t *testing.T) {
	c := redisConfig(t)
	c.MetadataMaxEntries = 20
	c.MetadataMaxBytes = 1600
	s := testRedis(t, c).store.(*redisMetadata)
	for _, key := range []string{strings.Repeat("a", 64), strings.Repeat("b", 64)} {
		ticket, err := s.begin(t.Context(), key, time.Now())
		if err != nil {
			t.Fatal(err)
		}
		if _, err := s.commit(t.Context(), ticket, &metadata{key: key, etag: strings.Repeat("x", 550), policy: responsePolicy{maxAge: 60}}); err != nil {
			t.Fatal(err)
		}
	}
	count, err := s.client.ZCard(t.Context(), s.keys[1]).Result()
	if err != nil || count != 1 {
		t.Fatal("byte budget did not evict a record", count, err)
	}
	budget, err := s.client.HGet(t.Context(), s.keys[0], "_bytes").Int64()
	if err != nil || budget > c.MetadataMaxBytes {
		t.Fatal("byte budget exceeded", budget, err)
	}
	if ticket, err := s.begin(t.Context(), strings.Repeat("a", 64), time.Now()); err != nil || ticket.previous != nil {
		t.Fatal("oldest metadata survived byte eviction", err)
	}
}

func TestRedisReadersPreserveCaptureUpdates(t *testing.T) {
	c := redisConfig(t)
	s := testRedis(t, c).store
	testReadersPreserveCaptureUpdates(t, s)
}

func TestMemoryReadersPreserveCaptureUpdates(t *testing.T) {
	testReadersPreserveCaptureUpdates(t, newMetadataCache(4, 10000))
}

func testReadersPreserveCaptureUpdates(t *testing.T, store metadataStore) {
	t.Helper()
	begin := func() metadataSnapshot {
		t.Helper()
		v, err := store.begin(t.Context(), "key", time.Now())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { store.finish(v) })
		return v
	}
	commit := func(v metadataSnapshot, noStore bool) {
		t.Helper()
		_, err := store.commit(t.Context(), v, &metadata{key: v.key, order: v.order, etag: `"tag"`, validated: time.Now(), policy: responsePolicy{maxAge: 60, noStore: noStore}})
		if err != nil {
			t.Fatal(err)
		}
	}
	commit(begin(), false)
	capture := begin()
	reader := begin()
	if reader.previous == nil {
		t.Fatal("fresh reader lost metadata")
	}
	commit(capture, true)
	if next := begin(); next.previous != nil {
		t.Fatal("a read suppressed no-store invalidation")
	}
	capture = begin()
	_ = begin() // A newer failed capture does not commit.
	commit(capture, false)
	if next := begin(); next.previous == nil {
		t.Fatal("failed newer request suppressed a successful update")
	}
}
