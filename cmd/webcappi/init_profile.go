package main

import (
	"context"
	"errors"
	"flag"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
)

func runInitProfile(args []string) (err error) {
	defaults := config.Defaults()
	fs := flag.NewFlagSet("init-profile", flag.ContinueOnError)
	output := fs.String("out", "-", "tar output path, or - for stdout")
	browser := fs.String("browser-path", defaults.BrowserPath, "Chrome for Testing executable")
	work := fs.String("work-dir", defaults.WorkDir, "temporary working directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	if *output == "" {
		return errors.New("out must be a path or -")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if err := os.MkdirAll(*work, 0700); err != nil {
		return err
	}
	b := capture.Browser{Path: *browser}
	if err := b.Check(ctx); err != nil {
		return err
	}
	dst := os.Stdout
	if *output != "-" {
		dst, err = os.OpenFile(*output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		defer func() {
			if e := dst.Close(); err == nil {
				err = e
			}
			if err != nil {
				_ = os.Remove(*output)
			}
		}()
	}
	stopOutput := context.AfterFunc(ctx, func() { _ = dst.Close() })
	defer stopOutput()
	renderer := capture.Renderer{Browser: b, WorkDir: *work, MaxProfileBytes: defaults.MaxProfileBytes}
	err = renderer.InitProfile(ctx, dst)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return err
}
