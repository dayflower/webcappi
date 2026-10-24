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
	"testing"
	"time"
)

func TestCaptureOutputSize(t *testing.T) {
	b := fixtureBrowser(t)
	// Green requires the original layout dimensions, regardless of PNG size.
	const html = `<!doctype html><style>
html,body{margin:0;width:100%;height:100%;background:red}
@media (width:320px) and (height:200px){html,body{background:rgb(20,180,60)}}
#center{position:fixed;left:150px;top:90px;width:20px;height:20px;background:blue}
#edge{position:fixed;right:0;bottom:0;width:40px;height:40px;background:red}
</style><div id=center></div><div id=edge></div>`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte(html))
	}))
	defer server.Close()
	template, work := t.TempDir(), t.TempDir()
	before := treeDigest(t, template)
	renderer := Renderer{Browser: b, WorkDir: work, MaxProfileBytes: 512 << 20}
	for _, tc := range []struct {
		name, source string
		algorithm    ResizeAlgorithm
	}{
		{"url", "url", ""},
		{HTMLModeFile, HTMLModeFile, ""},
		{HTMLModeLoopback, HTMLModeLoopback, ""},
		{"catmull-rom", HTMLModeFile, ResizeCatmullRom},
		{"approx-bilinear", HTMLModeFile, ResizeApproxBilinear},
		{"nearest-neighbor", HTMLModeFile, ResizeNearestNeighbor},
	} {
		t.Run(tc.name, func(t *testing.T) {
			renderer.ResizeAlgorithm = tc.algorithm
			req := Request{Width: 320, Height: 200, OutputWidth: 160, OutputHeight: 160,
				Profile: Profile{UserDataDir: template, ProfileDirectory: "Default"}}
			if tc.source == "url" {
				req.URL = server.URL
			} else {
				req.HTML, req.HTMLMode = html, tc.source
			}
			ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
			defer cancel()
			data, err := renderer.Capture(ctx, req)
			if err != nil {
				t.Fatal(err)
			}
			img, err := png.Decode(bytes.NewReader(data))
			if err != nil {
				t.Fatal(err)
			}
			if img.Bounds() != image.Rect(0, 0, 160, 160) {
				t.Fatalf("unexpected PNG size: %v", img.Bounds())
			}
			for _, pixel := range []struct {
				x, y int
				want color.RGBA
			}{
				{0, 0, color.RGBA{20, 180, 60, 255}},
				{159, 159, color.RGBA{20, 180, 60, 255}},
				{80, 80, color.RGBA{0, 0, 255, 255}},
				{64, 80, color.RGBA{20, 180, 60, 255}},
				{96, 80, color.RGBA{20, 180, 60, 255}},
			} {
				if got := color.RGBAModel.Convert(img.At(pixel.x, pixel.y)); got != pixel.want {
					t.Fatalf("pixel %d,%d: got %v, want %v", pixel.x, pixel.y, got, pixel.want)
				}
			}
		})
	}
	if before != treeDigest(t, template) {
		t.Fatal("resized capture changed template")
	}
	if entries, err := os.ReadDir(work); err != nil || len(entries) != 0 {
		t.Fatalf("resized capture leaked working files: %v, %v", entries, err)
	}
}
