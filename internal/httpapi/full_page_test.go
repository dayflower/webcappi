package httpapi

import (
	"context"
	"fmt"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
)

func TestFullPageQuery(t *testing.T) {
	api, _ := setup(t, func(_ context.Context, req capture.Request) ([]byte, error) {
		w, h := req.OutputWidth, req.OutputHeight
		if !req.FullPage {
			var err error
			w, h, err = req.OutputSize(req.Width, req.Height)
			if err != nil {
				return nil, err
			}
		}
		return fmt.Appendf(nil, "%t/%dx%d", req.FullPage, w, h), nil
	})
	for _, tc := range []struct{ query, want string }{
		{"", "false/1280x720"},
		{"full_page", "false/1280x720"},
		{"full_page=", "false/1280x720"},
		{"full_page=false", "false/1280x720"},
		{"full_page=true", "true/0x0"},
		{"full_page=true&output_width=400", "true/400x0"},
		{"full_page=true&output_height=400", "true/0x400"},
	} {
		w := get(api, valid+"&"+tc.query, "")
		if w.Code != 200 || w.Body.String() != tc.want {
			t.Fatalf("GET %s: %d %s", tc.query, w.Code, w.Body)
		}
		w = post(api, "/capture?"+tc.query, "<p>hello</p>", "text/html", "")
		if w.Code != 200 || w.Body.String() != tc.want {
			t.Fatalf("POST %s: %d %s", tc.query, w.Code, w.Body)
		}
	}
	for _, query := range []string{"full_page=1", "full_page=TRUE", "full_page=no", "full_page=+", "full_page=true&full_page=", "full_page=&full_page="} {
		if w := get(api, valid+"&"+query, ""); w.Code != 400 {
			t.Fatalf("GET %s: %d %s", query, w.Code, w.Body)
		}
		if w := post(api, "/capture?"+query, "<p>hello</p>", "text/html", ""); w.Code != 400 {
			t.Fatalf("POST %s: %d %s", query, w.Code, w.Body)
		}
	}
	base := get(api, valid, "")
	for _, query := range []string{"full_page", "full_page=", "full_page=false"} {
		if w := get(api, valid+"&"+query, base.Header().Get("ETag")); w.Code != 304 {
			t.Fatalf("false equivalent %s: %d %s", query, w.Code, w.Body)
		}
	}
	if w := get(api, valid+"&full_page=true", base.Header().Get("ETag")); w.Code != 200 {
		t.Fatalf("full page reused viewport metadata: %d %s", w.Code, w.Body)
	}
}
