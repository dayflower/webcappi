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
	profile := fs.String("profile", config.DefaultProfileName, "capture profile name")
	url := fs.String("url", "", "page URL")
	width := fs.Int("width", capture.DefaultViewportWidth, "viewport width")
	height := fs.Int("height", capture.DefaultViewportHeight, "viewport height")
	fs.Int("output-width", 0, "PNG width (defaults to viewport width)")
	fs.Int("output-height", 0, "PNG height (defaults to viewport height)")
	out := fs.String("out", "capture.png", "PNG output path")
	selector := fs.String("ready-selector", "", "page readiness CSS selector")
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
	s := service.New(ctx, c, renderer.Capture)
	defer s.Close()
	api := httpapi.New(c, s, checkFiles)
	if args[0] == "capture" {
		// Reuse HTTP input validation so CLI and REST accept the same requests.
		req, err := http.NewRequestWithContext(ctx, "GET", "http://localhost/capture", nil)
		if err != nil {
			return err
		}
		q := req.URL.Query()
		q.Set("url", *url)
		q.Set("profile", *profile)
		q.Set("width", fmt.Sprint(*width))
		q.Set("height", fmt.Sprint(*height))
		fs.Visit(func(f *flag.Flag) {
			switch f.Name {
			case "output-width":
				q.Set("output_width", f.Value.String())
			case "output-height":
				q.Set("output_height", f.Value.String())
			}
		})
		q.Set("ready_selector", *selector)
		req.URL.RawQuery = q.Encode()
		writer := &fileResponse{header: http.Header{}}
		api.ServeHTTP(writer, req)
		if writer.status != 200 {
			return fmt.Errorf("capture HTTP %d: %s", writer.status, writer.body)
		}
		return os.WriteFile(*out, writer.body, 0600)
	}
	if args[0] != "serve" {
		return fmt.Errorf("unknown command %q", args[0])
	}
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

type fileResponse struct {
	header http.Header
	status int
	body   []byte
}

func (f *fileResponse) Header() http.Header    { return f.header }
func (f *fileResponse) WriteHeader(status int) { f.status = status }
func (f *fileResponse) Write(b []byte) (int, error) {
	if f.status == 0 {
		f.status = 200
	}
	f.body = append(f.body, b...)
	return len(b), nil
}
