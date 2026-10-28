package capture

import (
	"bytes"
	"context"
	"errors"
	"image"
	"image/png"
	"io"

	"golang.org/x/image/draw"
	"golang.org/x/image/math/f64"
)

type ResizeAlgorithm string

const (
	ResizeBilinear        ResizeAlgorithm = "bilinear"
	ResizeCatmullRom      ResizeAlgorithm = "catmull_rom"
	ResizeApproxBilinear  ResizeAlgorithm = "approx_bilinear"
	ResizeNearestNeighbor ResizeAlgorithm = "nearest_neighbor"
)

func (a ResizeAlgorithm) interpolator() (draw.Interpolator, error) {
	switch a {
	case ResizeBilinear:
		return draw.BiLinear, nil
	case ResizeCatmullRom:
		return draw.CatmullRom, nil
	case ResizeApproxBilinear:
		return draw.ApproxBiLinear, nil
	case ResizeNearestNeighbor:
		return draw.NearestNeighbor, nil
	default:
		return nil, errors.New("resize_algorithm must be bilinear, catmull_rom, approx_bilinear or nearest_neighbor")
	}
}

func (a ResizeAlgorithm) Validate() error {
	_, err := a.interpolator()
	return err
}

// resizePNG scales the complete image to the rounded output dimensions, or
// fills the canvas proportionally and crops around the center when crop is true.
// Decode and encode share the capture deadline, including cancellation.
func resizePNG(ctx context.Context, data []byte, width, height int, algorithm ResizeAlgorithm, crop bool) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	filter, err := algorithm.interpolator()
	if err != nil {
		return nil, err
	}
	src, err := png.Decode(contextReader{ctx, bytes.NewReader(data)})
	if err != nil {
		return nil, err
	}
	sw, sh := src.Bounds().Dx(), src.Bounds().Dy()
	matrix := f64.Aff3{float64(width) / float64(sw), 0, 0,
		0, float64(height) / float64(sh), 0}
	if crop {
		scale := max(float64(width)/float64(sw), float64(height)/float64(sh))
		matrix = f64.Aff3{scale, 0, (float64(width) - float64(sw)*scale) / 2,
			0, scale, (float64(height) - float64(sh)*scale) / 2}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	dst := image.NewRGBA(image.Rect(0, 0, width, height))
	// Transform only visible output pixels: extreme aspect ratios must not
	// allocate a huge intermediate image. Stripes also bound cancellation lag.
	for y := 0; y < height; y += 16 {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		stripe := dst.SubImage(image.Rect(0, y, width, min(y+16, height))).(*image.RGBA)
		filter.Transform(stripe, matrix, src, src.Bounds(), draw.Src, nil)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	if err := png.Encode(contextWriter{ctx, &out}, dst); err != nil {
		return nil, err
	}
	return out.Bytes(), ctx.Err()
}

type contextWriter struct {
	ctx context.Context
	w   io.Writer
}

func (w contextWriter) Write(p []byte) (int, error) {
	if err := w.ctx.Err(); err != nil {
		return 0, err
	}
	return w.w.Write(p)
}
