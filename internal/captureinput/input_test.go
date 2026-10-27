package captureinput

import (
	"reflect"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
)

func ptr[T any](v T) *T { return &v }

func options() Options {
	return Options{MaxWidth: 4096, MaxHeight: 4096, MaxPixels: 8_388_608,
		DefaultProfileName: "default", Profiles: map[string]capture.Profile{
			"default": {ProfileDirectory: "Default"}, "named": {ProfileDirectory: "Profile 1"},
		}}
}

func TestDimensions(t *testing.T) {
	for _, tc := range []struct {
		name string
		in   Input
		want [4]int
		err  string
	}{
		{"defaults", Input{}, [4]int{1280, 720, 1280, 720}, ""},
		{"full page", Input{FullPage: true}, [4]int{1280, 720, 0, 0}, ""},
		{"full page output width", Input{FullPage: true, OutputWidth: ptr(400)}, [4]int{1280, 720, 400, 0}, ""},
		{"full page output height", Input{FullPage: true, OutputHeight: ptr(400)}, [4]int{1280, 720, 0, 400}, ""},
		{"full page output limit", Input{FullPage: true, OutputWidth: ptr(4097)}, [4]int{}, "output exceeds configured limits"},
		{"full page output pixels", Input{FullPage: true, OutputWidth: ptr(4096), OutputHeight: ptr(4096)}, [4]int{}, "output exceeds configured limits"},
		{"viewport width", Input{Width: ptr(800)}, [4]int{800, 720, 800, 720}, ""},
		{"viewport height", Input{Height: ptr(480)}, [4]int{1280, 480, 1280, 480}, ""},
		{"output width", Input{OutputWidth: ptr(400)}, [4]int{1280, 720, 400, 720}, ""},
		{"output height", Input{OutputHeight: ptr(400)}, [4]int{1280, 720, 1280, 400}, ""},
		{"minimum", Input{Width: ptr(1), Height: ptr(1), OutputWidth: ptr(1), OutputHeight: ptr(1)}, [4]int{1, 1, 1, 1}, ""},
		{"viewport pixels", Input{Width: ptr(4096), Height: ptr(2048)}, [4]int{4096, 2048, 4096, 2048}, ""},
		{"viewport height limit", Input{Width: ptr(2048), Height: ptr(4096)}, [4]int{2048, 4096, 2048, 4096}, ""},
		{"output pixels", Input{OutputWidth: ptr(4096), OutputHeight: ptr(2048)}, [4]int{1280, 720, 4096, 2048}, ""},
		{"output height limit", Input{OutputWidth: ptr(2048), OutputHeight: ptr(4096)}, [4]int{1280, 720, 2048, 4096}, ""},
		{"viewport width over", Input{Width: ptr(4097)}, [4]int{}, "viewport exceeds configured limits"},
		{"viewport height over", Input{Height: ptr(4097)}, [4]int{}, "viewport exceeds configured limits"},
		{"viewport pixels over", Input{Width: ptr(4096), Height: ptr(2049)}, [4]int{}, "viewport exceeds configured limits"},
		{"output width over", Input{OutputWidth: ptr(4097)}, [4]int{}, "output exceeds configured limits"},
		{"output height over", Input{OutputHeight: ptr(4097)}, [4]int{}, "output exceeds configured limits"},
		{"output pixels over", Input{OutputWidth: ptr(4096), OutputHeight: ptr(2049)}, [4]int{}, "output exceeds configured limits"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req, err := Normalize(tc.in, options())
			if tc.err != "" {
				if err == nil || err.Error() != tc.err {
					t.Fatalf("error = %v, want %q", err, tc.err)
				}
				return
			}
			if got := [4]int{req.Width, req.Height, req.OutputWidth, req.OutputHeight}; err != nil || got != tc.want {
				t.Fatalf("dimensions = %v, error = %v, want %v", got, err, tc.want)
			}
		})
	}
	for _, name := range []string{"width", "height", "output_width", "output_height"} {
		for _, value := range []int{0, -1} {
			in := Input{}
			fields := map[string]**int{"width": &in.Width, "height": &in.Height, "output_width": &in.OutputWidth, "output_height": &in.OutputHeight}
			*fields[name] = ptr(value)
			if _, err := Normalize(in, options()); err == nil || err.Error() != name+" must be a positive integer" {
				t.Fatalf("%s=%d: %v", name, value, err)
			}
		}
	}
}

func TestURL(t *testing.T) {
	for _, u := range []string{"http://fixture/", "https://service.namespace.svc.cluster.local/", "http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]:65535/", "http://fixture:1/", "http://fixture:65535/", "http://fixture/" + strings.Repeat("a", 8192-len("http://fixture/"))} {
		if req, err := Normalize(Input{URL: &u}, options()); err != nil || req.URL != u {
			t.Fatalf("URL %q: %+v, %v", u, req, err)
		}
	}
	for _, tc := range []struct{ url, message string }{
		{"", "url is required and must be <=8192 bytes"},
		{strings.Repeat("a", 8193), "url is required and must be <=8192 bytes"},
		{"file:///tmp/a", "url must be an absolute http(s) URL without credentials"},
		{"/relative", "url must be an absolute http(s) URL without credentials"},
		{"http:///path", "url must be an absolute http(s) URL without credentials"},
		{"http://:80/", "url must be an absolute http(s) URL without credentials"},
		{"http://user:pass@fixture/", "url must be an absolute http(s) URL without credentials"},
		{"http://%zz/", "url must be an absolute http(s) URL without credentials"},
		{"http://fixture:abc/", "url must be an absolute http(s) URL without credentials"},
		{"http://fixture/\x00", "url must be an absolute http(s) URL without credentials"},
		{"http://fixture/\r", "url must be an absolute http(s) URL without credentials"},
		{"http://fixture/\n", "url must be an absolute http(s) URL without credentials"},
		{"http://fixture:0/", "invalid URL port"},
		{"http://fixture:65536/", "invalid URL port"},
	} {
		if _, err := Normalize(Input{URL: &tc.url}, options()); err == nil || err.Error() != tc.message {
			t.Fatalf("URL %q: %v, want %q", tc.url, err, tc.message)
		}
	}
}

func TestProfileSelectorAndDelay(t *testing.T) {
	for _, name := range []*string{nil, ptr("default"), ptr("named"), ptr(""), ptr("missing")} {
		req, err := Normalize(Input{ProfileName: name}, options())
		if name != nil && (*name == "" || *name == "missing") {
			if err == nil || err.Error() != "unknown profile" {
				t.Fatalf("profile %q: %v", *name, err)
			}
			continue
		}
		want := "default"
		if name != nil {
			want = *name
		}
		if err != nil || req.ProfileName != want || !reflect.DeepEqual(req.Profile, options().Profiles[want]) {
			t.Fatalf("profile = %+v, error = %v", req, err)
		}
	}
	for _, size := range []int{0, 1024, 1025} {
		s := strings.Repeat("x", size)
		req, err := Normalize(Input{ReadySelector: s}, options())
		if size <= 1024 {
			if err != nil || req.ReadySelector != s {
				t.Fatalf("selector %d: %v", size, err)
			}
		} else if err == nil || err.Error() != "ready_selector must be <=1024 bytes" {
			t.Fatal(err)
		}
	}
	for _, delay := range []int{-1, 0, 300_000, 300_001} {
		req, err := Normalize(Input{DelayMS: delay}, options())
		if delay >= 0 && delay <= 300_000 {
			if err != nil || req.DelayMS != delay {
				t.Fatalf("delay %d: %v", delay, err)
			}
		} else if err == nil || err.Error() != "delay_ms must be an integer between 0 and 300000" {
			t.Fatal(err)
		}
	}
	custom := options()
	custom.DefaultProfileName = "named"
	if req, err := Normalize(Input{}, custom); err != nil || req.ProfileName != "named" {
		t.Fatalf("custom default: %+v, %v", req, err)
	}
}

func TestValidationOrder(t *testing.T) {
	for _, tc := range []struct {
		in      Input
		message string
	}{
		{Input{URL: ptr("file:///x"), Width: ptr(0)}, "url must be an absolute http(s) URL without credentials"},
		{Input{Width: ptr(4097), OutputWidth: ptr(0)}, "output_width must be a positive integer"},
		{Input{Width: ptr(4097), OutputWidth: ptr(4097)}, "viewport exceeds configured limits"},
		{Input{OutputWidth: ptr(4097), DelayMS: -1}, "output exceeds configured limits"},
		{Input{DelayMS: -1, ProfileName: ptr("missing")}, "delay_ms must be an integer between 0 and 300000"},
		{Input{ProfileName: ptr("missing"), ReadySelector: strings.Repeat("x", 1025)}, "unknown profile"},
	} {
		if _, err := Normalize(tc.in, options()); err == nil || err.Error() != tc.message {
			t.Fatalf("%+v: %v, want %q", tc.in, err, tc.message)
		}
	}
}
