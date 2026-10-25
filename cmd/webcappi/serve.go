package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"time"

	"github.com/dayflower/webcappi/internal/buildinfo"
	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/httpapi"
	"github.com/dayflower/webcappi/internal/service"
)

func runServe(args []string) (err error) {
	_, path, _, err := parseCommandFlags("serve", args)
	if err != nil {
		return err
	}
	ctx, stop := commandContext()
	defer stop()
	r, err := prepareRuntime(ctx, path)
	if err != nil {
		return err
	}
	defer r.close(&err)
	s := service.New(ctx, r.config, r.renderer.CaptureWithMetadata)
	return serveHTTP(ctx, r, s)
}

// Own the service until jobs and HTTP handlers finish, including listen failures.
func serveHTTP(ctx context.Context, r *runtimeResources, s *service.Service) (err error) {
	defer s.Close()
	c := r.config
	cache, err := httpapi.OpenCache(ctx, c)
	if err != nil {
		return err
	}
	defer func() {
		if e := cache.Close(); err == nil {
			err = e
		}
	}()
	api := httpapi.New(c, s, r.ready, cache)
	// The job timeout covers queueing, copying, browser startup and rendering.
	server := &http.Server{Addr: c.Listen, Handler: r.auth.Handler(api), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: c.Timeout() + 10*time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32 << 10, BaseContext: func(net.Listener) context.Context { return ctx }}
	done := make(chan error, 1)
	go func() {
		version, revision, modified := buildinfo.Current()
		slog.Info("listening", "address", c.Listen, "version", version, "revision", revision, "modified", modified, "browser_version", capture.BrowserVersion)
		done <- server.ListenAndServe()
	}()
	serverDone := false
	select {
	case err = <-done:
		serverDone = true
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
	case <-ctx.Done():
	}
	api.Drain()
	// Wait for browser exit and disk cleanup before shutting down HTTP handlers.
	s.Close()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if e := server.Shutdown(shutdown); e != nil {
		_ = server.Close()
		if err == nil {
			err = e
		}
	}
	if !serverDone {
		<-done
	}
	return err
}
