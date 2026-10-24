package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
)

func tokenFile(t *testing.T, token string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(token), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestBearerTokenFile(t *testing.T) {
	for _, token := range []string{"aB09-._~+/==", "secret\n", "secret\r\n", strings.Repeat("a", maxBearerTokenBytes) + "\r\n"} {
		if _, err := NewBearerAuth(&config.Auth{BearerTokenFile: tokenFile(t, token)}); err != nil {
			t.Fatalf("valid token file rejected: %v", err)
		}
	}
	for _, token := range []string{"", "\n", "\r\n", " secret", "secret ", "secret\r", "secret\n\n", "secret\nother", "secret\x00", "secrét", "=", "a=b", strings.Repeat("a", maxBearerTokenBytes+1)} {
		if _, err := NewBearerAuth(&config.Auth{BearerTokenFile: tokenFile(t, token)}); err == nil {
			t.Fatal("invalid token file accepted")
		} else if strings.Contains(err.Error(), "secret") {
			t.Fatal("error includes token contents")
		}
	}
	for _, path := range []string{filepath.Join(t.TempDir(), "missing"), t.TempDir()} {
		if _, err := NewBearerAuth(&config.Auth{BearerTokenFile: path}); err == nil {
			t.Fatal("unreadable token file accepted")
		}
	}
}

func TestBearerAuthentication(t *testing.T) {
	var renders atomic.Int32
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		renders.Add(1)
		return []byte("png"), nil
	})
	path := tokenFile(t, "correct-token\n")
	auth, err := NewBearerAuth(&config.Auth{BearerTokenFile: path})
	if err != nil {
		t.Fatal(err)
	}
	handler := auth.Handler(api)
	request := func(path string, headers []string, etag string) *httptest.ResponseRecorder {
		t.Helper()
		r := httptest.NewRequest(http.MethodGet, path, nil)
		for _, h := range headers {
			r.Header.Add("Authorization", h)
		}
		if etag != "" {
			r.Header.Set("If-None-Match", etag)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	invalid := [][]string{
		nil, {""}, {"Bearer"}, {"Bearer "}, {"Bearer wrong-token"},
		{"Basic correct-token"}, {"Bearer\tcorrect-token"}, {"Bearer correct-token "},
		{"Bearer correct-token, Bearer correct-token"}, {"Bearer correct-token", "Bearer correct-token"},
		{"Bearer correct-token", "Bearer wrong-token"}, {"Bearer " + strings.Repeat("a", maxBearerTokenBytes+1)},
	}
	checkUnauthorized := func(path, etag string) {
		t.Helper()
		before := renders.Load()
		for _, headers := range invalid {
			w := request(path, headers, etag)
			if w.Code != http.StatusUnauthorized || w.Header().Get("WWW-Authenticate") != "Bearer" || w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("Content-Type") != "application/json" || w.Header().Get("ETag") != "" || w.Body.String() != "{\"error\":\"unauthorized\"}\n" {
				t.Fatalf("invalid credentials: status=%d headers=%v body=%s", w.Code, w.Header(), w.Body)
			}
		}
		if renders.Load() != before {
			t.Fatal("unauthorized request rendered")
		}
	}
	checkUnauthorized(valid, "")
	checkUnauthorized("/capture?unknown=input", "")
	checkUnauthorized(valid+"&access_token=correct-token", "")
	first := request(valid, []string{"Bearer correct-token"}, "")
	if first.Code != http.StatusOK || renders.Load() != 1 {
		t.Fatalf("authorized capture failed: %d", first.Code)
	}
	checkUnauthorized(valid, first.Header().Get("ETag"))
	for _, header := range []string{"Bearer correct-token", "bearer correct-token", "BEARER   correct-token"} {
		if w := request(valid, []string{header}, first.Header().Get("ETag")); w.Code != http.StatusNotModified || renders.Load() != 1 {
			t.Fatalf("authorized cached request failed: %d", w.Code)
		}
	}
	for _, path := range []string{"/healthz", "/readyz"} {
		if request(path, nil, "").Code != http.StatusOK || request(path, []string{"Bearer wrong"}, "").Code != http.StatusOK {
			t.Fatal("probe requires authentication")
		}
	}
	// Secret changes take effect only after a restart, including after deletion.
	if err := os.WriteFile(path, []byte("replacement-token"), 0600); err != nil {
		t.Fatal(err)
	}
	if request(valid, []string{"Bearer replacement-token"}, "").Code != http.StatusUnauthorized {
		t.Fatal("token unexpectedly reloaded")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if request(valid, []string{"Bearer correct-token"}, "").Code != http.StatusOK {
		t.Fatal("startup token not retained")
	}
}

func TestBearerAuthenticationDisabled(t *testing.T) {
	auth, err := NewBearerAuth(nil)
	if err != nil {
		t.Fatal(err)
	}
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) { return []byte("png"), nil })
	w := httptest.NewRecorder()
	auth.Handler(api).ServeHTTP(w, httptest.NewRequest(http.MethodGet, valid, nil))
	if w.Code != http.StatusOK {
		t.Fatalf("authentication required without auth configuration: %d", w.Code)
	}
}
