package httpapi

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/captureinput"
	"github.com/dayflower/webcappi/internal/config"
)

func (a *API) parse(r *http.Request) (capture.Request, error) {
	var req capture.Request
	if len(r.URL.RawQuery) > 16*1024 {
		return req, errors.New("query too long")
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return req, errors.New("invalid query encoding")
	}
	allowed := map[string]bool{"url": true, "width": true, "height": true, "output_width": true, "output_height": true, "profile": true, "ready_selector": true, "delay_ms": true, "full_page": true}
	if r.Method == http.MethodPost {
		delete(allowed, "url")
		allowed["html_mode"] = true
		allowed["capture_metadata"] = true
	}
	for key, values := range q {
		if !allowed[key] || len(values) != 1 {
			return req, fmt.Errorf("unknown or repeated parameter %q", key)
		}
	}
	if r.Method == http.MethodPost {
		req.HTMLMode = capture.HTMLModeFile
		if values, ok := q["html_mode"]; ok {
			if values[0] != capture.HTMLModeFile && values[0] != capture.HTMLModeLoopback {
				return req, errors.New("html_mode must be file or loopback")
			}
			req.HTMLMode = values[0]
		}
		if values, ok := q["capture_metadata"]; ok {
			req.Metadata, err = parseCaptureMetadata(values[0])
			if err != nil {
				return req, err
			}
		}
	}
	in := captureinput.Input{ReadySelector: q.Get("ready_selector")}
	switch q.Get("full_page") {
	case "", "false":
	case "true":
		in.FullPage = true
	default:
		return req, errors.New("full_page must be true, false or empty")
	}
	if r.Method == http.MethodGet {
		u := q.Get("url")
		in.URL = &u
	}
	for key, dst := range map[string]**int{"width": &in.Width, "height": &in.Height, "output_width": &in.OutputWidth, "output_height": &in.OutputHeight} {
		if values, ok := q[key]; ok {
			n, err := strconv.Atoi(values[0])
			if err != nil {
				// Defer rejection to the shared validator so URL errors still win.
				n = 0
			}
			*dst = &n
		}
	}
	if values, ok := q["delay_ms"]; ok {
		n, err := strconv.Atoi(values[0])
		if err != nil {
			n = -1
		}
		in.DelayMS = n
	}
	if values, ok := q["profile"]; ok {
		in.ProfileName = &values[0]
	}
	validated, err := captureinput.Normalize(in, captureinput.Options{
		MaxWidth: a.cfg.MaxWidth, MaxHeight: a.cfg.MaxHeight, MaxPixels: a.cfg.MaxPixels,
		DefaultProfileName: config.DefaultProfileName, Profiles: a.cfg.Profiles,
	})
	validated.HTMLMode = req.HTMLMode
	validated.Metadata = req.Metadata
	return validated, err
}

func parseCaptureMetadata(value string) (*capture.MetadataRequest, error) {
	method, selector, _ := strings.Cut(value, ":")
	var attribute string
	switch method {
	case capture.MetadataTextContent:
	case capture.MetadataAttributeValue:
		attribute, selector, _ = strings.Cut(selector, ":")
		if attribute == "" {
			return nil, errors.New("capture_metadata requires an attribute name")
		}
	default:
		return nil, errors.New("invalid capture_metadata method")
	}
	if selector == "" {
		return nil, errors.New("capture_metadata requires a selector")
	}
	return &capture.MetadataRequest{Method: method, Selector: selector, Attribute: attribute}, nil
}
