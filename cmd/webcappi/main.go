package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/httpapi"
	"github.com/dayflower/webcappi/internal/service"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		slog.Error("webcappi", "error", err)
		os.Exit(1)
	}
}
func run(args []string) (err error) {
	if len(args) == 0 {
		return errors.New("usage: webcappi serve|capture|check|init-profile|version [flags]")
	}
	if args[0] == "version" {
		fmt.Println("webcappi; Chrome for Testing " + capture.BrowserVersion)
		return nil
	}
	if args[0] == "init-profile" {
		return runInitProfile(args[1:])
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	path := fs.String("config", "config.json", "server configuration")
	flags := registerCaptureFlags(fs)
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	c, err := config.Load(*path)
	if err != nil {
		return err
	}
	auth, err := httpapi.NewBearerAuth(c.Auth)
	if err != nil {
		return err
	}
	b := capture.Browser{Path: c.BrowserPath, NoSandbox: c.NoSandbox}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err = os.MkdirAll(c.WorkDir, 0700); err != nil {
		return err
	}
	startup, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	// Keep validation of configured bundles separate from the generated template.
	checkFiles := c.CheckFiles
	err = b.Check(startup)
	if err == nil {
		err = checkFiles(startup)
	}
	renderer := capture.Renderer{Browser: b, WorkDir: c.WorkDir, MaxProfileBytes: c.MaxProfileBytes, ResizeAlgorithm: c.ResizeAlgorithm}
	if _, provided := c.Profiles[config.DefaultProfileName]; err == nil && !provided {
		var temporary *capture.TemporaryProfile
		temporary, err = renderer.NewProfile(startup)
		if err == nil {
			defer func() {
				if e := temporary.Close(); err == nil {
					err = e
				}
			}()
			providedFiles := checkFiles
			checkFiles = func(ctx context.Context) error {
				if err := providedFiles(ctx); err != nil {
					return err
				}
				return temporary.CheckFiles(ctx)
			}
			c.Profiles = maps.Clone(c.Profiles)
			if c.Profiles == nil {
				c.Profiles = make(map[string]capture.Profile)
			}
			c.Profiles[config.DefaultProfileName] = temporary.Profile
			slog.Info("temporary default profile created", "user_data_dir", temporary.Profile.UserDataDir)
		}
	}
	if err == nil {
		for _, p := range c.Profiles {
			if err = renderer.Check(startup, p); err != nil {
				break
			}
		}
	}
	cancel()
	if err != nil {
		return err
	}
	if args[0] == "check" {
		fmt.Println("configuration, bundle and browser version verified")
		return nil
	}
	s := service.New(ctx, c, renderer.CaptureWithMetadata)
	defer s.Close()
	if args[0] == "capture" {
		return captureToFile(ctx, c, s, flags.input(fs), *flags.out)
	}
	if args[0] != "serve" {
		return fmt.Errorf("unknown command %q", args[0])
	}
	api := httpapi.New(c, s, checkFiles)
	// The job timeout covers queueing, copying, browser startup and rendering.
	server := &http.Server{Addr: c.Listen, Handler: auth.Handler(api), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: c.Timeout() + 10*time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32 << 10, BaseContext: func(net.Listener) context.Context { return ctx }}
	done := make(chan error, 1)
	go func() {
		slog.Info("listening", "address", c.Listen, "browser_version", capture.BrowserVersion)
		done <- server.ListenAndServe()
	}()
	select {
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
	}
	api.Drain()
	// Cancel active jobs first; the worker waits for browser exit and disk cleanup.
	s.Close()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err = server.Shutdown(shutdown); err != nil {
		_ = server.Close()
		return err
	}
	return nil
}
