package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/captureinput"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/httpapi"
	"github.com/dayflower/webcappi/internal/service"
)

type recordingGetter struct {
	requests []capture.Request
	contexts []context.Context
	err      error
}

func (s *recordingGetter) Get(ctx context.Context, req capture.Request) (*service.Image, error) {
	s.requests = append(s.requests, req)
	s.contexts = append(s.contexts, ctx)
	return &service.Image{PNG: []byte("png")}, s.err
}

func parseCaptureFlags(t *testing.T, args []string) captureinput.Input {
	t.Helper()
	fs := flag.NewFlagSet("capture", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	f := registerCaptureFlags(fs)
	if err := fs.Parse(args); err != nil {
		t.Fatal(err)
	}
	return f.input(fs)
}

func captureConfig() config.Config {
	c := config.Defaults()
	c.Profiles = map[string]capture.Profile{
		"default": {ProfileDirectory: "Default"}, "named": {ProfileDirectory: "Profile 1"},
	}
	return c
}

func TestCaptureCLIAndHTTPParity(t *testing.T) {
	for _, tc := range []struct {
		name   string
		flags  []string
		status int
	}{
		{"defaults", nil, 200},
		{"viewport", []string{"-width", "1200", "-height", "800"}, 200},
		{"output width", []string{"-output-width", "400"}, 200},
		{"output height", []string{"-output-height", "400"}, 200},
		{"resize", []string{"-output-width", "400", "-output-height", "300"}, 200},
		{"named profile", []string{"-profile", "named", "-ready-selector", "#日本語"}, 200},
		{"repeated flag", []string{"-width", "0", "-width", "640"}, 200},
		{"integer flag syntax", []string{"-width", "0x280"}, 200},
		{"zero width", []string{"-width", "0"}, 400},
		{"zero height", []string{"-height", "0"}, 400},
		{"zero output width", []string{"-output-width", "0"}, 400},
		{"zero output height", []string{"-output-height", "0"}, 400},
		{"negative output", []string{"-output-width", "-1"}, 400},
		{"width limit", []string{"-width", "4097"}, 400},
		{"pixel limit", []string{"-width", "4096", "-height", "2049"}, 400},
		{"output limit", []string{"-output-width", "4097"}, 400},
		{"empty profile", []string{"-profile", ""}, 400},
		{"unknown profile", []string{"-profile", "missing"}, 400},
		{"selector limit", []string{"-ready-selector", strings.Repeat("x", 1025)}, 400},
		{"invalid URL first", []string{"-url", "file:///tmp/x", "-width", "0"}, 400},
		{"missing URL first", []string{"-url", "", "-profile", "missing"}, 400},
		{"encoded query limit", []string{"-url", "http://fixture/" + strings.Repeat("あ", 2000)}, 400},
	} {
		t.Run(tc.name, func(t *testing.T) {
			args := append([]string{"-url", "http://fixture/"}, tc.flags...)
			in := parseCaptureFlags(t, args)
			c := captureConfig()
			cli := &recordingGetter{}
			out := filepath.Join(t.TempDir(), "capture.png")
			err := captureToFile(t.Context(), c, cli, in, out)

			// Encode the equivalent parsed flag values as an actual HTTP request.
			q := url.Values{"url": {*in.URL}, "profile": {*in.ProfileName}, "ready_selector": {in.ReadySelector}}
			for key, value := range map[string]*int{"width": in.Width, "height": in.Height, "output_width": in.OutputWidth, "output_height": in.OutputHeight} {
				if value != nil {
					q.Set(key, strconv.Itoa(*value))
				}
			}
			httpService := &recordingGetter{}
			w := httptest.NewRecorder()
			httpapi.New(c, httpService, nil).ServeHTTP(w, httptest.NewRequest("GET", "/capture?"+q.Encode(), nil))
			if w.Code != tc.status {
				t.Fatalf("HTTP status=%d, want %d: %s", w.Code, tc.status, w.Body)
			}
			if w.Code != 200 {
				want := "capture HTTP 400: " + w.Body.String()
				if w.Code != 400 || err == nil || err.Error() != want || len(cli.requests) != 0 || len(httpService.requests) != 0 {
					t.Fatalf("CLI error=%v, HTTP=%d %s, calls=%d/%d", err, w.Code, w.Body, len(cli.requests), len(httpService.requests))
				}
				if _, e := os.Stat(out); !errors.Is(e, os.ErrNotExist) {
					t.Fatalf("invalid input wrote output: %v", e)
				}
				return
			}
			if err != nil || len(cli.requests) != 1 || !reflect.DeepEqual(cli.requests, httpService.requests) {
				t.Fatalf("CLI=%+v, HTTP=%+v, error=%v", cli.requests, httpService.requests, err)
			}
			if cli.contexts[0] != t.Context() {
				t.Fatal("capture context was replaced")
			}
			data, e := os.ReadFile(out)
			if e != nil || string(data) != "png" {
				t.Fatalf("output=%q, error=%v", data, e)
			}
			info, e := os.Stat(out)
			if e != nil || info.Mode().Perm() != 0600 {
				t.Fatalf("output permissions: %v, %v", info, e)
			}
		})
	}
}

func TestCaptureCLIErrorCompatibility(t *testing.T) {
	for _, tc := range []struct {
		err  error
		want string
	}{
		{service.ErrBusy, "capture HTTP 503: {\"error\":\"capture capacity exhausted\"}\n"},
		{service.ErrClosed, "capture HTTP 503: {\"error\":\"service is shutting down\"}\n"},
		{context.DeadlineExceeded, "capture HTTP 504: {\"error\":\"capture timed out\"}\n"},
		{context.Canceled, "capture HTTP 503: {\"error\":\"capture canceled\"}\n"},
		{errors.Join(errors.New("wrapped"), context.Canceled), "capture HTTP 503: {\"error\":\"capture canceled\"}\n"},
		{errors.New("navigation failure"), "capture HTTP 502: {\"error\":\"page capture failed\"}\n"},
	} {
		out := filepath.Join(t.TempDir(), "capture.png")
		s := &recordingGetter{err: tc.err}
		err := captureToFile(t.Context(), captureConfig(), s, parseCaptureFlags(t, []string{"-url", "http://fixture/"}), out)
		if err == nil || err.Error() != tc.want {
			t.Fatalf("%v: got %v, want %q", tc.err, err, tc.want)
		}
		if _, err := os.Stat(out); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("service failure wrote output: %v", err)
		}
	}
	if got := captureError(400, "ready_selector must be <=1024 bytes").Error(); got != "capture HTTP 400: {\"error\":\"ready_selector must be \\u003c=1024 bytes\"}\n" {
		t.Fatalf("JSON escaping changed: %q", got)
	}
}

func TestCaptureCLIFlagErrors(t *testing.T) {
	for _, args := range [][]string{{"-width", "NaN"}, {"-output-width", "99999999999999999999999"}, {"-unknown"}} {
		fs := flag.NewFlagSet("capture", flag.ContinueOnError)
		fs.SetOutput(io.Discard)
		registerCaptureFlags(fs)
		if err := fs.Parse(args); err == nil {
			t.Fatalf("accepted %v", args)
		}
	}
	if err := run([]string{"capture", "positional"}); err == nil || err.Error() != "unexpected positional arguments" {
		t.Fatalf("positional arguments: %v", err)
	}
}

func TestCaptureCLIWriteFailure(t *testing.T) {
	s := &recordingGetter{}
	err := captureToFile(t.Context(), captureConfig(), s, parseCaptureFlags(t, []string{"-url", "http://fixture/"}), t.TempDir())
	var pathError *os.PathError
	if !errors.As(err, &pathError) || len(s.requests) != 1 {
		t.Fatalf("write failure: %v", err)
	}
}
