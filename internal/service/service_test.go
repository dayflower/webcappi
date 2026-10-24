package service

import (
	"context"
	"errors"
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

func TestIndependentCaptures(t *testing.T) {
	for _, html := range []string{"", "<p>hello</p>"} {
		t.Run(html, func(t *testing.T) {
			cfg := config.Defaults()
			cfg.Concurrency = 4
			var calls atomic.Int32
			entered := make(chan struct{}, 4)
			release := make(chan struct{})
			s := New(t.Context(), cfg, func(ctx context.Context, _ capture.Request) (capture.Result, error) {
				calls.Add(1)
				entered <- struct{}{}
				select {
				case <-release:
				case <-ctx.Done():
					return capture.Result{}, ctx.Err()
				}
				return capture.Result{PNG: []byte("PNG")}, nil
			})
			defer s.Close()
			req := request("http://fixture")
			req.HTML = html
			var wg sync.WaitGroup
			for range 4 {
				wg.Go(func() {
					if _, err := s.Get(t.Context(), req); err != nil {
						t.Error(err)
					}
				})
			}
			for range 4 {
				select {
				case <-entered:
				case <-time.After(2 * time.Second):
					t.Fatal("identical requests were coalesced")
				}
			}
			close(release)
			wg.Wait()
			first, err := s.Get(t.Context(), req)
			if err != nil {
				t.Fatal(err)
			}
			next, err := s.Get(t.Context(), req)
			if err != nil || first == next || calls.Load() != 6 || first.ETag != next.ETag {
				t.Fatal("images were retained or content tag changed", err)
			}
		})
	}
}

func waitActive(t *testing.T, s *Service, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		n := s.active
		s.mu.Unlock()
		if n == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("job count did not reach", want)
}

func TestBoundsCancellationAndFailure(t *testing.T) {
	cfg := config.Defaults()
	cfg.QueueSize = 1
	entered := make(chan struct{}, 4)
	var active, maximum atomic.Int32
	s := New(t.Context(), cfg, func(ctx context.Context, req capture.Request) (capture.Result, error) {
		n := active.Add(1)
		defer active.Add(-1)
		if n > maximum.Load() {
			maximum.Store(n)
		}
		entered <- struct{}{}
		if req.URL == "fail" {
			return capture.Result{}, errors.New("failed")
		}
		if req.URL == "large" {
			return capture.Result{PNG: make([]byte, cfg.MaxPNGBytes+1)}, nil
		}
		<-ctx.Done()
		// Late successful returns must still report cancellation.
		return capture.Result{PNG: []byte("PNG")}, nil
	})
	defer s.Close()
	first, cancel := context.WithCancel(t.Context())
	defer cancel()
	second, cancelSecond := context.WithCancel(t.Context())
	defer cancelSecond()
	one, two := make(chan error, 1), make(chan error, 1)
	go func() { _, err := s.Get(first, request("same")); one <- err }()
	<-entered
	go func() { _, err := s.Get(second, request("same")); two <- err }()
	waitActive(t, s, 2)
	if _, err := s.Get(t.Context(), request("same")); !errors.Is(err, ErrBusy) {
		t.Fatal("capacity exceeded", err)
	}
	cancelSecond()
	if err := <-two; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if active.Load() != 1 {
		t.Fatal("queued cancellation affected active request")
	}
	cancel()
	if err := <-one; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	waitActive(t, s, 0)
	if active.Load() != 0 || maximum.Load() != 1 {
		t.Fatal("concurrency or cleanup failed")
	}
	for _, url := range []string{"fail", "fail", "large"} {
		if _, err := s.Get(t.Context(), request(url)); err == nil {
			t.Fatal("failure ignored", url)
		}
	}
}

func TestJobTimeoutAndShutdown(t *testing.T) {
	cfg := config.Defaults()
	cfg.CaptureTimeoutSeconds = 1
	entered := make(chan struct{}, 2)
	s := New(t.Context(), cfg, func(ctx context.Context, _ capture.Request) (capture.Result, error) {
		entered <- struct{}{}
		<-ctx.Done()
		return capture.Result{}, ctx.Err()
	})
	if _, err := s.Get(t.Context(), request("slow")); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("timeout missing", err)
	}
	<-entered
	done := make(chan error, 1)
	go func() { _, err := s.Get(t.Context(), request("shutdown")); done <- err }()
	<-entered
	s.Close()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal("shutdown did not cancel job", err)
	}
	if _, err := s.Get(t.Context(), request("late")); !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
}

func TestMetadataKey(t *testing.T) {
	base := request("http://fixture")
	explicit := base
	explicit.OutputWidth, explicit.OutputHeight = base.Width, base.Height
	if Key(base) != Key(explicit) {
		t.Fatal("default output dimensions differ")
	}
	changes := []func(*capture.Request){
		func(r *capture.Request) { r.URL += "#fragment" },
		func(r *capture.Request) { r.Width++ },
		func(r *capture.Request) { r.Height++ },
		func(r *capture.Request) { r.OutputWidth = 100 },
		func(r *capture.Request) { r.OutputHeight = 100 },
		func(r *capture.Request) { r.ProfileName = "other" },
		func(r *capture.Request) { r.ReadySelector = "#ready" },
		func(r *capture.Request) { r.DelayMS = 100 },
	}
	for _, change := range changes {
		changed := base
		change(&changed)
		if Key(changed) == Key(base) {
			t.Fatal("capture conditions missing from key")
		}
	}
}
