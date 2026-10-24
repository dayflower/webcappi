package service

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
)

func request(url string) capture.Request {
	return capture.Request{URL: url, Width: 800, Height: 480, ProfileName: "default", Profile: capture.Profile{ProfileDirectory: "Default"}}
}

func TestCacheAndCoalescing(t *testing.T) {
	cfg := config.Defaults()
	var calls atomic.Int32
	entered := make(chan struct{})
	release := make(chan struct{})
	s := New(t.Context(), cfg, func(context.Context, capture.Request) ([]byte, error) {
		if calls.Add(1) == 1 {
			close(entered)
			<-release
		}
		return []byte("PNG"), nil
	})
	defer s.Close()
	const n = 16
	var wg sync.WaitGroup
	results := make(chan *Image, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			img, err := s.Get(t.Context(), request("http://fixture"))
			if err != nil {
				t.Error(err)
			}
			results <- img
		}()
	}
	<-entered
	deadline := time.After(2 * time.Second)
	for {
		s.mu.Lock()
		waiters := s.flights[Key(request("http://fixture"))].waiters
		s.mu.Unlock()
		if waiters == n {
			break
		}
		select {
		case <-deadline:
			t.Fatal("waiters did not join")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	close(release)
	wg.Wait()
	close(results)
	var first *Image
	for image := range results {
		if first == nil {
			first = image
		}
		if image != first {
			t.Fatal("coalesced results differ")
		}
	}
	hit, err := s.Get(t.Context(), request("http://fixture"))
	if err != nil || hit != first || calls.Load() != 1 {
		t.Fatalf("cache missed: %v %d", err, calls.Load())
	}
	// Simulate expiry without sleeping for the public configuration's one-second minimum.
	s.mu.Lock()
	first.Expires = time.Now().Add(-time.Second)
	s.mu.Unlock()
	next, err := s.Get(t.Context(), request("http://fixture"))
	if err != nil || next == first || calls.Load() != 2 {
		t.Fatal("expired entry did not render again")
	}
	if next.ETag != first.ETag {
		t.Fatal("ETag must depend on content")
	}
	changed := request("http://fixture")
	changed.Profile = capture.Profile{
		UserDataDir: "/other/user-data", ProfileDirectory: "Other",
		Extensions: []string{"/other/extension"}, ReadySelectors: []string{"#extension-ready"},
	}
	sameProfile, err := s.Get(t.Context(), changed)
	if err != nil || sameProfile != next || calls.Load() != 2 {
		t.Fatalf("profile settings bypassed the named profile cache: %v %d", err, calls.Load())
	}
	changed = request("http://fixture")
	changed.ProfileName = "other"
	_, _ = s.Get(t.Context(), changed)
	changed = request("http://fixture")
	changed.ReadySelector = "#ready"
	_, _ = s.Get(t.Context(), changed)
	changed = request("http://fixture")
	changed.Width = 801
	_, _ = s.Get(t.Context(), changed)
	changed = request("http://fixture")
	changed.Height = 481
	_, _ = s.Get(t.Context(), changed)
	changed = request("http://fixture")
	changed.OutputWidth = 400
	_, _ = s.Get(t.Context(), changed)
	changed = request("http://fixture")
	changed.OutputHeight = 240
	_, _ = s.Get(t.Context(), changed)
	_, _ = s.Get(t.Context(), request("http://other-fixture"))
	if calls.Load() != 9 {
		t.Fatal("request parameters missing from key")
	}
	explicit := request("http://fixture")
	explicit.OutputWidth, explicit.OutputHeight = explicit.Width, explicit.Height
	if Key(explicit) != Key(request("http://fixture")) {
		t.Fatal("explicit default output size must reuse the same cache key")
	}
}

func TestHTMLRequestsAreIndependent(t *testing.T) {
	cfg := config.Defaults()
	cfg.Concurrency, cfg.QueueSize = 1, 1
	var calls atomic.Int32
	entered := make(chan struct{}, 4)
	release := make(chan struct{})
	s := New(t.Context(), cfg, func(ctx context.Context, _ capture.Request) ([]byte, error) {
		calls.Add(1)
		entered <- struct{}{}
		select {
		case <-release:
			return []byte("png"), nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	})
	defer s.Close()
	req := request("")
	req.HTML = "<p>identical HTML</p>"
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	first := make(chan error, 1)
	go func() { _, err := s.Get(ctx, req); first <- err }()
	<-entered
	second := make(chan error, 1)
	go func() { _, err := s.Get(t.Context(), req); second <- err }()
	deadline := time.Now().Add(2 * time.Second)
	for {
		s.mu.Lock()
		n := len(s.flights)
		s.mu.Unlock()
		if n == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("identical HTML requests were coalesced")
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := s.Get(t.Context(), req); !errors.Is(err, ErrBusy) {
		t.Fatal("HTML jobs did not respect queue capacity", err)
	}
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatal("first HTML request was not canceled", err)
	}
	<-entered
	close(release)
	if err := <-second; err != nil {
		t.Fatal("canceling one HTML request affected another", err)
	}
	if _, err := s.Get(t.Context(), req); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 3 || len(s.cache) != 0 || s.bytes != 0 {
		t.Fatal("HTML results were cached")
	}
}

func TestBoundsCancellationAndFailure(t *testing.T) {
	cfg := config.Defaults()
	cfg.Concurrency = 1
	cfg.QueueSize = 1
	cfg.CacheMaxBytes = 5
	cfg.CacheMaxEntries = 1
	entered := make(chan struct{}, 32)
	var active, maxActive atomic.Int32
	var blocked atomic.Bool
	blocked.Store(true)
	render := func(ctx context.Context, r capture.Request) ([]byte, error) {
		n := active.Add(1)
		defer active.Add(-1)
		if n > maxActive.Load() {
			maxActive.Store(n)
		}
		entered <- struct{}{}
		if blocked.Load() {
			<-ctx.Done()
			return nil, ctx.Err()
		}
		if r.URL == "fail" {
			return nil, errors.New("failed")
		}
		return []byte(r.URL), nil
	}
	s := New(t.Context(), cfg, render)
	defer s.Close()
	ctx, stop := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { _, e := s.Get(ctx, request("one")); done <- e }()
	<-entered
	queued, stopQueued := context.WithCancel(t.Context())
	doneQ := make(chan error, 1)
	go func() { _, e := s.Get(queued, request("two")); doneQ <- e }()
	for {
		s.mu.Lock()
		n := len(s.flights)
		s.mu.Unlock()
		if n == 2 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := s.Get(t.Context(), request("three")); !errors.Is(err, ErrBusy) {
		t.Fatal("unique queue not bounded", err)
	}
	stop()
	stopQueued()
	<-done
	<-doneQ
	for {
		s.mu.Lock()
		n := len(s.flights)
		s.mu.Unlock()
		if n == 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if active.Load() != 0 || maxActive.Load() != 1 {
		t.Fatal("render not canceled or concurrency exceeded")
	}
	blocked.Store(false)
	for _, url := range []string{"abc", "def", "oversized"} {
		if _, err := s.Get(t.Context(), request(url)); err != nil {
			t.Fatal(err)
		}
	}
	s.mu.Lock()
	if s.bytes > cfg.CacheMaxBytes || s.lru.Len() > 1 {
		t.Fatal("cache exceeded bounds")
	}
	s.mu.Unlock()
	if _, err := s.Get(t.Context(), request("fail")); err == nil {
		t.Fatal("expected failure")
	}
	if _, ok := s.cache[Key(request("fail"))]; ok {
		t.Fatal("failure cached")
	}
}

func TestOneWaiterCancellationDoesNotCancelOthers(t *testing.T) {
	cfg := config.Defaults()
	entered := make(chan struct{})
	release := make(chan struct{})
	s := New(t.Context(), cfg, func(ctx context.Context, _ capture.Request) ([]byte, error) {
		close(entered)
		select {
		case <-release:
			return []byte("png"), nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	})
	defer s.Close()
	ctx, cancel := context.WithCancel(t.Context())
	one := make(chan error, 1)
	two := make(chan error, 1)
	go func() { _, err := s.Get(ctx, request("x")); one <- err }()
	<-entered
	go func() { _, err := s.Get(t.Context(), request("x")); two <- err }()
	for {
		s.mu.Lock()
		n := s.flights[Key(request("x"))].waiters
		s.mu.Unlock()
		if n == 2 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	if err := <-one; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	close(release)
	if err := <-two; err != nil {
		t.Fatal(err)
	}
}

func TestJobTimeoutAndShutdown(t *testing.T) {
	cfg := config.Defaults()
	cfg.CaptureTimeoutSeconds = 1
	entered := make(chan struct{}, 2)
	s := New(t.Context(), cfg, func(ctx context.Context, _ capture.Request) ([]byte, error) {
		entered <- struct{}{}
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if _, err := s.Get(t.Context(), request("slow")); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("timeout missing", err)
	}
	done := make(chan error, 1)
	go func() { _, err := s.Get(t.Context(), request("shutdown")); done <- err }()
	<-entered
	s.Close()
	if err := <-done; err == nil {
		t.Fatal("shutdown did not cancel job")
	}
	if _, err := s.Get(t.Context(), request("late")); !errors.Is(err, ErrClosed) {
		t.Fatal(fmt.Sprint(err))
	}
}
