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
		name, source  string
		algorithm     ResizeAlgorithm
		width, height int
		want          image.Rectangle
	}{
		{"url", "url", "", 160, 160, image.Rect(0, 0, 160, 160)},
		{HTMLModeFile, HTMLModeFile, "", 160, 160, image.Rect(0, 0, 160, 160)},
		{HTMLModeLoopback, HTMLModeLoopback, "", 160, 160, image.Rect(0, 0, 160, 160)},
		{"catmull-rom", HTMLModeFile, ResizeCatmullRom, 160, 160, image.Rect(0, 0, 160, 160)},
		{"approx-bilinear", HTMLModeFile, ResizeApproxBilinear, 160, 160, image.Rect(0, 0, 160, 160)},
		{"nearest-neighbor", HTMLModeFile, ResizeNearestNeighbor, 160, 160, image.Rect(0, 0, 160, 160)},
		{"width only", "url", "", 160, 0, image.Rect(0, 0, 160, 100)},
		{"height only", HTMLModeFile, "", 0, 100, image.Rect(0, 0, 160, 100)},
		{"rounded width", HTMLModeLoopback, "", 161, 0, image.Rect(0, 0, 161, 101)},
		{"enlarge height", HTMLModeFile, "", 0, 400, image.Rect(0, 0, 640, 400)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			renderer.ResizeAlgorithm = tc.algorithm
			req := Request{Width: 320, Height: 200, OutputWidth: tc.width, OutputHeight: tc.height,
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
			if img.Bounds() != tc.want {
				t.Fatalf("unexpected PNG size: %v", img.Bounds())
			}
			if tc.width == 0 || tc.height == 0 {
				if got := color.RGBAModel.Convert(img.At(tc.want.Dx()-2, tc.want.Dy()-2)); got != (color.RGBA{255, 0, 0, 255}) {
					t.Fatalf("complete-image resize lost bottom-right corner: %v", got)
				}
				if got := color.RGBAModel.Convert(img.At(0, 0)); got != (color.RGBA{20, 180, 60, 255}) {
					t.Fatalf("viewport layout changed: %v", got)
				}
				return
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
