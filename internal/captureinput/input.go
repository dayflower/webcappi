// Package captureinput validates capture parameters independently of their transport.
package captureinput

import (
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"

	"github.com/dayflower/webcappi/internal/capture"
)

// Input preserves omission separately from explicitly supplied zero or empty values.
type Input struct {
	// A nil URL selects HTML input; the caller validates and supplies the HTML body.
	URL                       *string
	Width, Height             *int
	OutputWidth, OutputHeight *int
	ProfileName               *string
	ReadySelector             string
	DelayMS                   int
}

type Options struct {
	MaxWidth, MaxHeight int
	MaxPixels           int64
	DefaultProfileName  string
	Profiles            map[string]capture.Profile
}

// Normalize resolves defaults and profiles after validating the input. Validation
// groups retain their historical order: URL, dimensions, delay, profile, selector.
func Normalize(in Input, opts Options) (capture.Request, error) {
	var req capture.Request
	if in.URL != nil {
		req.URL = *in.URL
		if len(req.URL) == 0 || len(req.URL) > 8192 {
			return req, errors.New("url is required and must be <=8192 bytes")
		}
		u, err := url.Parse(req.URL)
		if err != nil || u.Host == "" || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || strings.ContainsAny(req.URL, "\x00\r\n") {
			return req, errors.New("url must be an absolute http(s) URL without credentials")
		}
		if port := u.Port(); port != "" {
			n, err := strconv.Atoi(port)
			if err != nil || n < 1 || n > 65535 {
				return req, errors.New("invalid URL port")
			}
		}
	}
	req.Width, req.Height = capture.DefaultViewportWidth, capture.DefaultViewportHeight
	for key, dimension := range map[string]struct{ input, output *int }{
		"width":         {in.Width, &req.Width},
		"height":        {in.Height, &req.Height},
		"output_width":  {in.OutputWidth, &req.OutputWidth},
		"output_height": {in.OutputHeight, &req.OutputHeight},
	} {
		if dimension.input != nil {
			if *dimension.input < 1 {
				return req, fmt.Errorf("%s must be a positive integer", key)
			}
			*dimension.output = *dimension.input
		}
	}
	if exceedsLimits(req.Width, req.Height, opts) {
		return req, errors.New("viewport exceeds configured limits")
	}
	req.OutputWidth, req.OutputHeight = req.OutputSize()
	if exceedsLimits(req.OutputWidth, req.OutputHeight, opts) {
		return req, errors.New("output exceeds configured limits")
	}
	if in.DelayMS < 0 || in.DelayMS > 300_000 {
		return req, errors.New("delay_ms must be an integer between 0 and 300000")
	}
	req.DelayMS = in.DelayMS
	req.ProfileName = opts.DefaultProfileName
	if in.ProfileName != nil {
		req.ProfileName = *in.ProfileName
	}
	p, ok := opts.Profiles[req.ProfileName]
	if !ok {
		return req, errors.New("unknown profile")
	}
	req.Profile = p
	req.ReadySelector = in.ReadySelector
	if len(req.ReadySelector) > 1024 {
		return req, errors.New("ready_selector must be <=1024 bytes")
	}
	return req, nil
}

func exceedsLimits(width, height int, opts Options) bool {
	// Division avoids overflow even when an adapter supplies the largest int.
	return width > opts.MaxWidth || height > opts.MaxHeight || int64(width) > opts.MaxPixels/int64(height)
}
