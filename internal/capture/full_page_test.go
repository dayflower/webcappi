package capture

import (
	"bytes"
	"context"
	"image"
	"image/color"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func TestCaptureFullPage(t *testing.T) {
	b := fixtureBrowser(t)
	const html = `<!doctype html><style>
html,body{margin:0;background:red}body{width:400px;height:1200px}
@media (width:320px) and (height:200px){body{background:rgb(20,180,60)}}
#end{position:absolute;left:0;top:1160px;width:400px;height:40px;background:blue}
</style><div id=end></div>`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte(html))
	}))
	defer server.Close()
	template, work := t.TempDir(), t.TempDir()
	before := treeDigest(t, template)
	renderer := Renderer{Browser: b, WorkDir: work, MaxProfileBytes: 512 << 20}
	for _, tc := range []struct {
		name, mode          string
		width, height       int
		maxWidth, maxHeight int
		maxPixels           int64
		want                image.Rectangle
		err                 bool
	}{
		{name: "url", want: image.Rect(0, 0, 400, 1200)},
		{name: "file", mode: HTMLModeFile, want: image.Rect(0, 0, 400, 1200)},
		{name: "loopback", mode: HTMLModeLoopback, want: image.Rect(0, 0, 400, 1200)},
		{name: "resize", mode: HTMLModeFile, width: 200, height: 600, want: image.Rect(0, 0, 200, 600)},
		{name: "output width", mode: HTMLModeFile, width: 200, want: image.Rect(0, 0, 200, 1200)},
		{name: "output height", mode: HTMLModeFile, height: 600, want: image.Rect(0, 0, 400, 600)},
		{name: "source height limit", height: 100, maxHeight: 1000, err: true},
		{name: "source width limit", width: 100, maxWidth: 350, err: true},
		{name: "source pixel limit", width: 100, height: 100, maxPixels: 400_000, err: true},
		{name: "output pixel limit", width: 1000, maxPixels: 500_000, err: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := renderer
			r.MaxWidth, r.MaxHeight, r.MaxPixels = tc.maxWidth, tc.maxHeight, tc.maxPixels
			req := Request{URL: server.URL, Width: 320, Height: 200, FullPage: true,
				OutputWidth: tc.width, OutputHeight: tc.height,
				Profile: Profile{UserDataDir: template, ProfileDirectory: "Default"}}
			if tc.mode != "" {
				req.URL, req.HTML, req.HTMLMode = "", html, tc.mode
			}
			ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
			defer cancel()
			data, err := r.Capture(ctx, req)
			if tc.err {
				if err == nil || !strings.Contains(err.Error(), "configured") {
					t.Fatalf("expected configured limit failure, got %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			img, err := png.Decode(bytes.NewReader(data))
			if err != nil || img.Bounds() != tc.want {
				t.Fatalf("PNG dimensions: %v, %v", img, err)
			}
			// These aspect ratios retain both ends; other cases exercise cropping.
			if tc.width == 0 && tc.height == 0 || tc.name == "resize" || tc.name == "output width" {
				if got := color.RGBAModel.Convert(img.At(10, 10)); got != (color.RGBA{20, 180, 60, 255}) {
					t.Fatalf("viewport layout changed: %v", got)
				}
				if got := color.RGBAModel.Convert(img.At(10, tc.want.Dy()-5)); got != (color.RGBA{0, 0, 255, 255}) {
					t.Fatalf("document bottom missing: %v", got)
				}
			}
		})
	}
	if before != treeDigest(t, template) {
		t.Fatal("full-page capture changed template")
	}
	if entries, err := os.ReadDir(work); err != nil || len(entries) != 0 {
		t.Fatalf("full-page capture leaked files: %v, %v", entries, err)
	}
}
