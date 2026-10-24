package capture

import (
	"bytes"
	"context"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestHTMLCapture(t *testing.T) {
	b := fixtureBrowser(t)
	for _, mode := range []string{"", HTMLModeLoopback} {
		name := mode
		if name == "" {
			name = "default-file"
		}
		t.Run(name, func(t *testing.T) { testHTMLCapture(t, b, mode) })
	}
}

func testHTMLCapture(t *testing.T, b Browser, mode string) {
	t.Helper()
	pixel := image.NewRGBA(image.Rect(0, 0, 1, 1))
	pixel.Set(0, 0, color.RGBA{20, 180, 60, 255})
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, pixel); err != nil {
		t.Fatal(err)
	}
	sources := make(chan string, 4)
	assets := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/pixel.png" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		if r.URL.Query().Get("secure") != "true" {
			t.Error("HTML document is not a secure context")
		}
		sources <- r.URL.Query().Get("source")
		_, _ = w.Write(encoded.Bytes())
	}))
	defer assets.Close()
	template := t.TempDir()
	work := filepath.Join(t.TempDir(), "HTML #%")
	if err := os.Mkdir(work, 0700); err != nil {
		t.Fatal(err)
	}
	before := treeDigest(t, template)
	renderer := Renderer{Browser: b, WorkDir: work, MaxProfileBytes: 512 << 20}
	req := Request{
		HTML: fmt.Sprintf(`<!doctype html><base href="%s/">
<style>body{margin:0;background:rgb(12,34,56)}img{position:absolute;top:0;left:0;width:20px;height:20px}</style>
<p>日本語</p><script>
setTimeout(() => {
 if (!document.querySelector('p').textContent.includes('日本語')) return;
 const img = document.createElement('img'); img.src = 'pixel.png?source=' + encodeURIComponent(location.href) + '&secure=' + isSecureContext;
 document.body.append(img); document.body.id = 'ready';
}, 50);
</script>`, assets.URL),
		Width: 320, Height: 200, ProfileName: "html",
		HTMLMode:      mode,
		Profile:       Profile{UserDataDir: template, ProfileDirectory: "Default"},
		ReadySelector: "#ready",
	}
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	data, err := renderer.Capture(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	img, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if img.Bounds().Dx() != req.Width || img.Bounds().Dy() != req.Height {
		t.Fatalf("wrong viewport: %v", img.Bounds())
	}
	assertSourceRemoved := func() {
		t.Helper()
		select {
		case source := <-sources:
			u, err := url.Parse(source)
			if err != nil {
				t.Fatal(err)
			}
			if mode == HTMLModeLoopback {
				if u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Port() == "" {
					t.Fatalf("HTML did not load from loopback HTTP: %q", source)
				}
				conn, err := net.DialTimeout("tcp", u.Host, time.Second)
				if err == nil {
					conn.Close()
					t.Fatal("HTML listener survived capture")
				}
			} else {
				if u.Scheme != "file" {
					t.Fatalf("HTML did not load from a file URL: %q", source)
				}
				if _, err := os.Stat(u.Path); !os.IsNotExist(err) {
					t.Fatalf("temporary HTML file survived capture: %v", err)
				}
			}
		default:
			t.Fatal("HTML image was not requested")
		}
	}
	assertSourceRemoved()
	for _, tc := range []struct {
		x, y int
		want color.RGBA
	}{
		{0, 0, color.RGBA{20, 180, 60, 255}},
		{319, 199, color.RGBA{12, 34, 56, 255}},
	} {
		if got := color.RGBAModel.Convert(img.At(tc.x, tc.y)); got != tc.want {
			t.Fatalf("pixel %d,%d: got %v, want %v", tc.x, tc.y, got, tc.want)
		}
	}
	if out := os.Getenv("WEBCAPPI_TEST_ARTIFACTS"); out != "" {
		name := "html.png"
		if mode == HTMLModeLoopback {
			name = "html-loopback.png"
		}
		if err := os.WriteFile(filepath.Join(out, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	reportSource := `<script>
const img = new Image();
img.src = '` + assets.URL + `/pixel.png?source=' + encodeURIComponent(location.href) + '&secure=' + isSecureContext;
document.body.append(img);
</script>`
	req.HTML = `<body>` + reportSource + `<img src="` + assets.URL + `/missing.png">`
	req.ReadySelector = ""
	if _, err := renderer.Capture(ctx, req); err == nil {
		t.Fatal("broken visible HTML image did not fail")
	}
	assertSourceRemoved()
	req.HTML = "<body><p>waiting</p>" + reportSource
	req.ReadySelector = "#never-ready"
	short, stop := context.WithTimeout(t.Context(), 4*time.Second)
	defer stop()
	if _, err := renderer.Capture(short, req); err == nil {
		t.Fatal("missing HTML selector did not time out")
	}
	assertSourceRemoved()
	if before != treeDigest(t, template) {
		t.Fatal("HTML capture changed template")
	}
	if entries, err := os.ReadDir(work); err != nil || len(entries) != 0 {
		t.Fatalf("HTML capture leaked working files: %v, %v", entries, err)
	}
}
