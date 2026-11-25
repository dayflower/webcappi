package httpapi

import (
	"context"
	"encoding/base64"
	"errors"
	"net/url"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

const captureMetadataHeader = "Webcappi-Capture-Metadata"

func metadataAPI(t *testing.T, render service.RenderFunc) *API {
	t.Helper()
	cfg := config.Defaults()
	cfg.Profiles = map[string]capture.Profile{"default": {ProfileDirectory: "Default"}}
	s := service.New(t.Context(), cfg, render)
	t.Cleanup(s.Close)
	return New(cfg, s, nil)
}

func TestCaptureMetadataResponse(t *testing.T) {
	const textSelector = " #rss-capture-metadata "
	const attributeSelector = `a[href^="/news/%23"]:first-of-type`
	const png = "\x89PNG\r\n\x1a\n"
	text := capture.MetadataRequest{Method: capture.MetadataTextContent, Selector: textSelector}
	attribute := capture.MetadataRequest{Method: capture.MetadataAttributeValue, Attribute: "data-count", Selector: attributeSelector}
	for _, tc := range []struct {
		name, query, value string
		metadata           *capture.MetadataRequest
	}{
		{"text", "textContent:" + textSelector, "3", &text},
		{"empty attribute", "attributeValue:data-count:" + attributeSelector, "", &attribute},
		{"whitespace and UTF-8", "attributeValue:data-count:" + attributeSelector, " 日本語\n\t ", &attribute},
		{"size limit", "textContent:" + textSelector, strings.Repeat("é", 512), &text},
		{"omitted", "", "", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			api := metadataAPI(t, func(_ context.Context, req capture.Request) (capture.Result, error) {
				calls++
				result := capture.Result{PNG: []byte(png)}
				if tc.metadata == nil {
					if req.Metadata != nil {
						t.Fatal("omitted capture metadata was requested")
					}
				} else {
					if req.Metadata == nil || *req.Metadata != *tc.metadata {
						t.Fatalf("metadata = %+v, want %+v", req.Metadata, tc.metadata)
					}
					result.Metadata = &tc.value
				}
				return result, nil
			})
			path, want := "/capture", ""
			if tc.metadata != nil {
				path += "?" + (url.Values{"capture_metadata": {tc.query}}).Encode()
				want = "b64url=" + base64.RawURLEncoding.EncodeToString([]byte(tc.value))
			}
			for _, etag := range []string{"", "*"} {
				w := post(api, path, "<p>example</p>", "text/html", etag)
				if w.Code != 200 || w.Body.String() != png ||
					w.Header().Get("Content-Type") != "image/png" || w.Header().Get("Cache-Control") != "no-store" ||
					w.Header().Get("ETag") != "" || w.Header().Get("Age") != "" {
					t.Fatalf("unexpected response: %d %v %q", w.Code, w.Header(), w.Body)
				}
				if values := w.Header().Values(captureMetadataHeader); (want == "" && len(values) != 0) ||
					(want != "" && (len(values) != 1 || values[0] != want)) {
					t.Fatalf("capture metadata = %q, want %q", values, want)
				}
			}
			if calls != 2 {
				t.Fatalf("render calls = %d, want 2", calls)
			}
		})
	}
}

func TestCaptureMetadataInvalidQuery(t *testing.T) {
	api, _ := setup(t, func(context.Context, capture.Request) ([]byte, error) {
		t.Fatal("render called for an invalid capture metadata query")
		return nil, nil
	})
	for _, values := range [][]string{
		{""}, {"textContent"}, {"textContent:"}, {"attributeValue"}, {"attributeValue:data-count"},
		{"attributeValue::#meta"}, {"attributeValue:data-count:"}, {"innerHTML:#meta"},
		{"TextContent:#meta"}, {"textContent:#meta", "textContent:#meta"},
	} {
		query := (url.Values{"capture_metadata": values}).Encode()
		if w := post(api, "/capture?"+query, "<p>example</p>", "text/html", ""); w.Code != 400 {
			t.Errorf("POST %s: %d %s", query, w.Code, w.Body)
		}
	}
	query := (url.Values{"capture_metadata": {"textContent:#meta"}}).Encode()
	if w := get(api, valid+"&"+query, ""); w.Code != 400 {
		t.Errorf("GET %s: %d %s", query, w.Code, w.Body)
	}
}

func TestCaptureMetadataErrorResponse(t *testing.T) {
	for _, tc := range []struct {
		name, value string
		err         error
		status      int
	}{
		{"attribute missing", "", errors.New("capture metadata attribute is missing"), 502},
		{"invalid UTF-8", "\xff", nil, 502},
		{"oversized value", strings.Repeat("é", 512) + "x", nil, 502},
		{"deadline", "", context.DeadlineExceeded, 504},
		{"canceled", "", context.Canceled, 503},
	} {
		t.Run(tc.name, func(t *testing.T) {
			api := metadataAPI(t, func(context.Context, capture.Request) (capture.Result, error) {
				return capture.Result{PNG: []byte("png"), Metadata: &tc.value}, tc.err
			})
			query := (url.Values{"capture_metadata": {"attributeValue:data-count:#meta"}}).Encode()
			w := post(api, "/capture?"+query, "<p>example</p>", "text/html", "")
			if w.Code != tc.status || len(w.Header().Values(captureMetadataHeader)) != 0 ||
				w.Header().Get("Content-Type") != "application/json" || !strings.Contains(w.Body.String(), `"error":`) {
				t.Fatalf("unexpected response: %d %v %q", w.Code, w.Header(), w.Body)
			}
		})
	}
}
