package capture

import (
	"bytes"
	"context"
	"fmt"
	"image/png"
	"math"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
)

// captureFullPage measures and bounds the source before asking Chrome to create
// a bitmap. An explicit clip prevents later DOM growth from bypassing the limit.
func (r Renderer) captureFullPage(ctx context.Context, req Request) ([]byte, error) {
	metrics, err := chromedp.Call(ctx, page.GetLayoutMetrics, cdp.Empty{})
	if err != nil {
		return nil, err
	}
	size := metrics.CSSContentSize
	if size == nil {
		return nil, fmt.Errorf("missing full-page dimensions")
	}
	width, height := math.Ceil(size.Width), math.Ceil(size.Height)
	if math.IsNaN(width) || math.IsNaN(height) || width < 1 || height < 1 || width > float64(r.maxWidth()) || height > float64(r.maxHeight()) {
		return nil, fmt.Errorf("full page exceeds configured dimensions")
	}
	sw, sh := int(width), int(height)
	if err := r.checkPageSize(sw, sh); err != nil {
		return nil, err
	}
	w, h := req.OutputWidth, req.OutputHeight
	if w == 0 {
		w = sw
	}
	if h == 0 {
		h = sh
	}
	if err := r.checkPageSize(w, h); err != nil {
		return nil, fmt.Errorf("full-page output: %w", err)
	}
	enabled := true
	shot, err := chromedp.Call(ctx, page.CaptureScreenshot, page.CaptureScreenshotParams{
		Format: page.CaptureScreenshotFormatPng, FromSurface: &enabled, CaptureBeyondViewport: &enabled,
		Clip: &page.Viewport{X: size.X, Y: size.Y, Width: width, Height: height, Scale: 1},
	})
	if err != nil {
		return nil, err
	}
	actual, err := png.DecodeConfig(contextReader{ctx, bytes.NewReader(shot.Data)})
	if err != nil {
		return nil, err
	}
	if actual.Width != sw || actual.Height != sh {
		return nil, fmt.Errorf("full-page screenshot dimensions differ from requested clip")
	}
	if w == sw && h == sh {
		return shot.Data, nil
	}
	algorithm := r.ResizeAlgorithm
	if algorithm == "" {
		algorithm = ResizeBilinear
	}
	return resizePNG(ctx, shot.Data, w, h, algorithm)
}

func (r Renderer) maxWidth() int {
	if r.MaxWidth > 0 {
		return r.MaxWidth
	}
	return 4096
}

func (r Renderer) maxHeight() int {
	if r.MaxHeight > 0 {
		return r.MaxHeight
	}
	return 4096
}

func (r Renderer) checkPageSize(width, height int) error {
	pixels := r.MaxPixels
	if pixels == 0 {
		pixels = 8_388_608
	}
	if width < 1 || height < 1 || width > r.maxWidth() || height > r.maxHeight() || int64(width) > pixels/int64(height) {
		return fmt.Errorf("full page exceeds configured limits")
	}
	return nil
}
