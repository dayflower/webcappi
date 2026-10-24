package capture

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"image/color"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"
)

func TestCaptureDelayAfterFrames(t *testing.T) {
	b := fixtureBrowser(t)
	frames := make(chan struct{}, 4)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/frames" {
			frames <- struct{}{}
			w.WriteHeader(http.StatusNoContent)
			return
		}
		fmt.Fprint(w, "unused")
	}))
	defer server.Close()
	// Only the renderer requests animation frames. Schedule the color change
	// after its second callback so a delay before asset checks cannot pass.
	html := fmt.Sprintf(`<!doctype html><style>body{background:rgb(255,0,0)}</style>
<body><script>
const raf = window.requestAnimationFrame.bind(window);
let frames = 0;
window.requestAnimationFrame = callback => raf(timestamp => {
 callback(timestamp);
 if (++frames === 2) {
   fetch('%s/frames', {mode:'no-cors'});
   setTimeout(() => document.body.style.background = 'rgb(20,180,60)', 200);
 }
});
</script>`, server.URL)
	work := t.TempDir()
	renderer := Renderer{Browser: b, WorkDir: work, MaxProfileBytes: 512 << 20}
	req := Request{
		HTML: html, Width: 100, Height: 100, DelayMS: 600,
		Profile: Profile{UserDataDir: t.TempDir(), ProfileDirectory: "Default"},
	}
	for _, mode := range []string{HTMLModeFile, HTMLModeLoopback} {
		t.Run(mode, func(t *testing.T) {
			req.HTMLMode = mode
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			data, err := renderer.Capture(ctx, req)
			if err != nil {
				t.Fatal(err)
			}
			img, err := png.Decode(bytes.NewReader(data))
			if err != nil {
				t.Fatal(err)
			}
			if got := color.RGBAModel.Convert(img.At(50, 50)); got != (color.RGBA{20, 180, 60, 255}) {
				t.Fatalf("captured before the delay after animation frames: %v", got)
			}
			select {
			case <-frames:
			case <-ctx.Done():
				t.Fatal("animation frame notification was not received")
			}
		})
	}
	for _, reason := range []string{"cancellation", "deadline"} {
		t.Run(reason, func(t *testing.T) {
			req.DelayMS = 30000
			// Browser startup must fit independently of the delay under test.
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			done := make(chan error, 1)
			finished := make(chan struct{})
			go func() {
				defer close(finished)
				_, err := renderer.Capture(ctx, req)
				done <- err
			}()
			defer func() {
				cancel()
				<-finished
			}()
			select {
			case <-frames:
			case <-ctx.Done():
				t.Fatal("capture did not reach the delay")
			}
			start := time.Now()
			want := context.DeadlineExceeded
			maxWait := 15 * time.Second
			if reason == "cancellation" {
				cancel()
				want = context.Canceled
				maxWait = 5 * time.Second
			}
			if err := <-done; !errors.Is(err, want) {
				t.Fatalf("delay did not return %v: %v", want, err)
			}
			if time.Since(start) > maxWait {
				t.Fatal("capture waited for the entire delay")
			}
		})
	}
	if entries, err := os.ReadDir(work); err != nil || len(entries) != 0 {
		t.Fatalf("delay capture leaked working files: %v, %v", entries, err)
	}
}
