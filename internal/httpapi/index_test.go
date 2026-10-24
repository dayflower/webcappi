package httpapi

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
)

func TestGuide(t *testing.T) {
	for _, state := range []string{"normal", "busy", "draining"} {
		t.Run(state, func(t *testing.T) {
			api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
				t.Error("guide invoked the renderer")
				return nil, nil
			})
			api.check = func(context.Context) error {
				t.Error("guide checked browser dependencies")
				return nil
			}
			api.requests = make(chan struct{}, 1)
			if state == "busy" {
				api.requests <- struct{}{}
			}
			if state == "draining" {
				api.Drain()
			}
			before := len(api.requests)
			first := get(api, "/", "")
			if first.Code != http.StatusOK || !strings.Contains(first.Body.String(), "<h1>webcappi</h1>") || !strings.Contains(first.Body.String(), "curl --fail") {
				t.Fatalf("guide missing: %d %s", first.Code, first.Body)
			}
			for key, want := range map[string]string{
				"Content-Type":           "text/html; charset=utf-8",
				"Cache-Control":          "no-store",
				"X-Content-Type-Options": "nosniff",
			} {
				if got := first.Header().Get(key); got != want {
					t.Errorf("%s = %q, want %q", key, got, want)
				}
			}
			r := httptest.NewRequest(http.MethodGet, "/?url=QUERY_SENTINEL&unknown=%zz", nil)
			r.Host = "HOST_SENTINEL"
			r.Header.Set("Forwarded", "host=FORWARDED_SENTINEL;proto=https")
			r.Header.Set("X-Forwarded-Host", "X_FORWARDED_SENTINEL")
			r.Header.Set("If-None-Match", "*")
			w := httptest.NewRecorder()
			api.ServeHTTP(w, r)
			if w.Code != http.StatusOK || w.Body.String() != first.Body.String() {
				t.Fatal("query, host or conditional headers changed the guide")
			}
			if len(api.requests) != before {
				t.Fatal("guide changed capture admission")
			}
		})
	}
}

func TestPublicGuideRouting(t *testing.T) {
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		t.Error("routing check invoked the renderer")
		return nil, nil
	})
	api.cfg.Auth = &config.Auth{BearerTokenFile: tokenFile(t, "correct-token")}
	auth, err := NewBearerAuth(api.cfg.Auth)
	if err != nil {
		t.Fatal(err)
	}
	handler := auth.Handler(api)
	for _, token := range []string{"", "Bearer wrong-token", "Bearer correct-token"} {
		for _, tc := range []struct {
			method, path string
			status       int
		}{
			{http.MethodGet, "/", http.StatusOK},
			{http.MethodGet, "/?unknown=input", http.StatusOK},
			{http.MethodHead, "/", http.StatusMethodNotAllowed},
			{http.MethodPost, "/", http.StatusMethodNotAllowed},
			{http.MethodPut, "/", http.StatusMethodNotAllowed},
			{http.MethodDelete, "/", http.StatusMethodNotAllowed},
		} {
			r := httptest.NewRequest(tc.method, tc.path, nil)
			r.Header.Set("Authorization", token)
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, r)
			if w.Code != tc.status || w.Header().Get("WWW-Authenticate") != "" {
				t.Fatalf("%s %s with %q: %d %v", tc.method, tc.path, token, w.Code, w.Header())
			}
			if tc.status == http.StatusMethodNotAllowed && (w.Header().Get("Allow") != "GET" || w.Header().Get("Content-Type") != "application/json") {
				t.Fatalf("incorrect method response: %v", w.Header())
			}
		}
		for _, path := range []string{"/missing", "/capture/", "//", "/index.html"} {
			r := httptest.NewRequest(http.MethodGet, path, nil)
			r.Header.Set("Authorization", token)
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, r)
			want := http.StatusUnauthorized
			if token == "Bearer correct-token" {
				want = http.StatusNotFound
			}
			if w.Code != want {
				t.Fatalf("GET %s with %q: %d, want %d", path, token, w.Code, want)
			}
		}
	}
}

func TestGuideConfiguration(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		for _, customLimits := range []bool{false, true} {
			t.Run(fmt.Sprintf("auth=%t/custom_limits=%t", enabled, customLimits), func(t *testing.T) {
				c := config.Defaults()
				if enabled {
					c.Auth = &config.Auth{BearerTokenFile: "/private/TOKEN_PATH_SENTINEL"}
				}
				c.Profiles = map[string]capture.Profile{
					"PROFILE_NAME_SENTINEL": {UserDataDir: "/private/PROFILE_PATH_SENTINEL"},
				}
				if customLimits {
					c.MaxWidth, c.MaxHeight = 2345, 1987
					c.MaxPixels, c.MaxPOSTBytes = 3456789, 765432
					c.CaptureTimeoutSeconds = 47
				}
				w := get(New(c, nil, nil), "/", "")
				if w.Code != http.StatusOK {
					t.Fatalf("guide failed: %d %s", w.Code, w.Body)
				}
				body := w.Body.String()
				for _, marker := range []string{`id="authentication"`, `href="#authentication"`, "YOUR_TOKEN", "<td>401</td>"} {
					if strings.Contains(body, marker) != enabled {
						t.Errorf("authentication content %q does not match enabled=%t", marker, enabled)
					}
				}
				for _, value := range []string{
					fmt.Sprintf("%d pixels", c.MaxWidth),
					fmt.Sprintf("%d pixels", c.MaxHeight),
					fmt.Sprintf("%d pixels", c.MaxPixels),
					fmt.Sprintf("%d bytes", c.MaxPOSTBytes),
					fmt.Sprintf("%d seconds", c.CaptureTimeoutSeconds),
				} {
					if !strings.Contains(body, "<td>"+value+"</td>") {
						t.Errorf("configured limit %q missing from guide", value)
					}
				}
				if strings.Contains(body, "SENTINEL") {
					t.Error("guide exposed token path or profile details")
				}
			})
		}
	}
}
