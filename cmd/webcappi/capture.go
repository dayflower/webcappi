package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"strconv"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/captureinput"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

func runCapture(args []string) (err error) {
	fs, path, flags, err := parseCommandFlags("capture", args)
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
	// Jobs use the process context, not the completed startup deadline.
	s := service.New(ctx, r.config, r.renderer.CaptureWithMetadata)
	defer s.Close()
	return captureToFile(ctx, r.config, s, flags.input(fs), *flags.out)
}

type captureFlags struct {
	values                    captureinput.Input
	outputWidth, outputHeight *int
	out                       *string
}

func registerCaptureFlags(fs *flag.FlagSet) *captureFlags {
	var f captureFlags
	f.values.ProfileName = fs.String("profile", config.DefaultProfileName, "capture profile name")
	f.values.URL = fs.String("url", "", "page URL")
	f.values.Width = fs.Int("width", capture.DefaultViewportWidth, "viewport width")
	f.values.Height = fs.Int("height", capture.DefaultViewportHeight, "viewport height")
	f.outputWidth = fs.Int("output-width", 0, "PNG width (defaults to captured width)")
	f.outputHeight = fs.Int("output-height", 0, "PNG height (defaults to captured height)")
	f.out = fs.String("out", "capture.png", "PNG output path")
	fs.StringVar(&f.values.ReadySelector, "ready-selector", "", "page readiness CSS selector")
	fs.BoolVar(&f.values.FullPage, "full-page", false, "capture the full scrollable page")
	return &f
}

func (f captureFlags) input(fs *flag.FlagSet) captureinput.Input {
	in := f.values
	fs.Visit(func(flag *flag.Flag) {
		switch flag.Name {
		case "output-width":
			in.OutputWidth = f.outputWidth
		case "output-height":
			in.OutputHeight = f.outputHeight
		}
	})
	return in
}

type captureGetter interface {
	Get(context.Context, capture.Request) (*service.Image, error)
}

func captureToFile(ctx context.Context, c config.Config, s captureGetter, in captureinput.Input, out string) error {
	// Retain the old CLI's encoded-size limit without constructing an HTTP request.
	q := url.Values{"url": {*in.URL}, "profile": {*in.ProfileName}, "ready_selector": {in.ReadySelector}, "full_page": {strconv.FormatBool(in.FullPage)}}
	for key, value := range map[string]*int{"width": in.Width, "height": in.Height, "output_width": in.OutputWidth, "output_height": in.OutputHeight} {
		if value != nil {
			q.Set(key, strconv.Itoa(*value))
		}
	}
	if len(q.Encode()) > 16*1024 {
		return captureError(400, "query too long")
	}
	req, err := captureinput.Normalize(in, captureinput.Options{
		MaxWidth: c.MaxWidth, MaxHeight: c.MaxHeight, MaxPixels: c.MaxPixels,
		DefaultProfileName: config.DefaultProfileName, Profiles: c.Profiles,
	})
	if err != nil {
		return captureError(400, err.Error())
	}
	img, err := s.Get(ctx, req)
	if err != nil {
		switch {
		case errors.Is(err, service.ErrBusy), errors.Is(err, service.ErrClosed):
			return captureError(503, err.Error())
		case errors.Is(err, context.DeadlineExceeded):
			return captureError(504, "capture timed out")
		case errors.Is(err, context.Canceled):
			return captureError(503, "capture canceled")
		default:
			slog.Warn("capture failed", "profile", req.ProfileName, "error", err)
			return captureError(502, "page capture failed")
		}
	}
	return os.WriteFile(out, img.PNG, 0600)
}

func captureError(status int, message string) error {
	body, _ := json.Marshal(map[string]string{"error": message})
	return fmt.Errorf("capture HTTP %d: %s", status, body)
}
