package capture_test

import (
	"encoding/base64"
	"image"
	"image/color"
	"image/png"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/httpapi"
	"github.com/dayflower/webcappi/internal/service"
)

func TestCaptureMetadataHTTP(t *testing.T) {
	path := os.Getenv("WEBCAPPI_TEST_BROWSER")
	if path == "" {
		t.Skip("set WEBCAPPI_TEST_BROWSER to run real browser tests")
	}
	cfg := config.Defaults()
	cfg.Profiles = map[string]capture.Profile{
		"default": {UserDataDir: t.TempDir(), ProfileDirectory: "Default"},
	}
	renderer := capture.Renderer{
		Browser: capture.Browser{Path: path, NoSandbox: os.Getenv("WEBCAPPI_TEST_NO_SANDBOX") == "1"},
		WorkDir: t.TempDir(), MaxProfileBytes: 512 << 20,
	}
	s := service.New(t.Context(), cfg, renderer.CaptureWithMetadata)
	defer s.Close()
	api := httpapi.New(cfg, s, nil)
	const html = `<!doctype html><style>
 body { margin: 0; height: 220px; background: white }
 article { position: absolute; top: 20px; width: 24px; height: 24px }
</style>
<article style="left: 10px; background: #f00"></article>
<article style="left: 40px; background: #0f0"></article>
<article style="left: 70px; background: #00f"></article>
<article style="left: 100px; background: #000"></article>
<div id=metadata hidden></div>
<script>
 setTimeout(() => {
   document.querySelector('article:last-of-type').remove();
   const metadata = document.querySelector('#metadata');
   metadata.textContent = document.querySelectorAll('article').length;
   metadata.setAttribute('data-count', metadata.textContent);
   const ready = document.createElement('div');
   ready.id = 'ready';
   document.body.append(ready);
 }, 100);
</script>`
	for _, mode := range []string{capture.HTMLModeFile, capture.HTMLModeLoopback} {
		for _, full := range []string{"false", "true"} {
			t.Run(mode+"/full_page="+full, func(t *testing.T) {
				metadata, height := "textContent:#metadata", 100
				if full == "true" {
					metadata, height = "attributeValue:data-count:#metadata", 220
				}
				query := url.Values{
					"html_mode": {mode}, "full_page": {full}, "ready_selector": {"#ready"},
					"width": {"160"}, "height": {"100"}, "capture_metadata": {metadata},
				}
				req := httptest.NewRequest(http.MethodPost, "/capture?"+query.Encode(), strings.NewReader(html))
				req.Header.Set("Content-Type", "text/html; charset=utf-8")
				req.Header.Set("If-None-Match", "*")
				w := httptest.NewRecorder()
				api.ServeHTTP(w, req)
				if w.Code != 200 || w.Header().Get("Content-Type") != "image/png" || w.Header().Get("Cache-Control") != "no-store" {
					t.Fatalf("unexpected capture response: %d %v %.128q", w.Code, w.Header(), w.Body)
				}
				img, err := png.Decode(w.Body)
				if err != nil {
					t.Fatal(err)
				}
				if img.Bounds() != image.Rect(0, 0, 160, height) {
					t.Fatalf("unexpected PNG dimensions: %v", img.Bounds())
				}
				count := 0
				background := color.RGBA{255, 255, 255, 255}
				for i, want := range []color.RGBA{
					{255, 0, 0, 255}, {0, 255, 0, 255}, {0, 0, 255, 255}, background,
				} {
					got := color.RGBAModel.Convert(img.At(15+30*i, 25)).(color.RGBA)
					if got != want {
						t.Fatalf("article position %d pixel = %v, want %v", i+1, got, want)
					}
					if got != background {
						count++
					}
				}
				header := w.Header().Get("Webcappi-Capture-Metadata")
				encoded, ok := strings.CutPrefix(header, "b64url=")
				decoded, err := base64.RawURLEncoding.DecodeString(encoded)
				if !ok || err != nil || string(decoded) != strconv.Itoa(count) {
					t.Fatalf("metadata header = %q, want count %d from PNG", header, count)
				}
			})
		}
	}
}
