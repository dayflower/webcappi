package capture

import (
	"bytes"
	"context"
	"errors"
	"image"
	"image/color"
	"image/png"
	"testing"
)

func TestResizeAlgorithmSelection(t *testing.T) {
	src := image.NewRGBA(image.Rect(0, 0, 4, 2))
	for y := range 2 {
		for x := range 4 {
			c := color.RGBA{0, 0, 0, 255}
			if x == 1 {
				c = color.RGBA{255, 255, 255, 255}
			}
			src.Set(x, y, c)
		}
	}
	var data bytes.Buffer
	if err := png.Encode(&data, src); err != nil {
		t.Fatal(err)
	}
	linear, err := resizePNG(t.Context(), data.Bytes(), 8, 4, ResizeBilinear, true)
	if err != nil {
		t.Fatal(err)
	}
	img, err := png.Decode(bytes.NewReader(linear))
	if err != nil {
		t.Fatal(err)
	}
	// The output sample is one quarter of the way between black and white.
	if got := color.RGBAModel.Convert(img.At(1, 1)); got != (color.RGBA{64, 64, 64, 255}) {
		t.Fatalf("bilinear interpolation: got %v", got)
	}
	cubic, err := resizePNG(t.Context(), data.Bytes(), 8, 4, ResizeCatmullRom, true)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(linear, cubic) {
		t.Fatal("algorithm selection did not change interpolated pixels")
	}
	for _, tc := range []struct {
		algorithm     ResizeAlgorithm
		width, height int
		x, y          int
		gray          uint8
	}{
		{ResizeApproxBilinear, 8, 4, 1, 1, 64},
		{ResizeNearestNeighbor, 8, 4, 1, 1, 0},
		// The approximate filter uses a smaller footprint when shrinking.
		{ResizeApproxBilinear, 1, 1, 0, 0, 128},
		{ResizeBilinear, 1, 1, 0, 0, 96},
	} {
		out, err := resizePNG(t.Context(), data.Bytes(), tc.width, tc.height, tc.algorithm, true)
		if err != nil {
			t.Fatal(err)
		}
		img, err := png.Decode(bytes.NewReader(out))
		if err != nil {
			t.Fatal(err)
		}
		want := color.RGBA{tc.gray, tc.gray, tc.gray, 255}
		got := color.RGBAModel.Convert(img.At(tc.x, tc.y)).(color.RGBA)
		// The approximate implementation can truncate by one grayscale level.
		if got.R != got.G || got.R != got.B || got.A != 255 || max(got.R, tc.gray)-min(got.R, tc.gray) > 1 {
			t.Fatalf("%s at %dx%d: got %v, want %v", tc.algorithm, tc.width, tc.height, got, want)
		}
	}
	if _, err := resizePNG(t.Context(), data.Bytes(), 8, 4, "unknown", true); err == nil {
		t.Fatal("unknown algorithm accepted")
	}
}

func TestResizePNG(t *testing.T) {
	for _, algorithm := range []ResizeAlgorithm{ResizeBilinear, ResizeCatmullRom, ResizeApproxBilinear, ResizeNearestNeighbor} {
		t.Run(string(algorithm), func(t *testing.T) { testResizePNG(t, algorithm) })
	}
}

func testResizePNG(t *testing.T, algorithm ResizeAlgorithm) {
	src := image.NewRGBA(image.Rect(0, 0, 12, 8))
	red, green := color.RGBA{255, 0, 0, 255}, color.RGBA{0, 180, 0, 255}
	for y := range 8 {
		for x := range 12 {
			c := red
			if x >= 4 && x < 8 && y >= 2 && y < 6 {
				c = green
			}
			src.Set(x, y, c)
		}
	}
	var data bytes.Buffer
	if err := png.Encode(&data, src); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name          string
		width, height int
		left, top     color.RGBA
	}{
		{"same size", 12, 8, red, red},
		{"downscale", 6, 4, red, red},
		{"upscale", 24, 16, red, red},
		{"crop left and right", 4, 8, green, red},
		{"crop top and bottom", 12, 2, red, green},
		{"odd dimensions", 9, 7, red, red},
		{"extremely wide output", 8192, 1, red, green},
		{"extremely tall output", 1, 8192, green, red},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out, err := resizePNG(t.Context(), data.Bytes(), tc.width, tc.height, algorithm, true)
			if err != nil {
				t.Fatal(err)
			}
			img, err := png.Decode(bytes.NewReader(out))
			if err != nil {
				t.Fatal(err)
			}
			if img.Bounds() != image.Rect(0, 0, tc.width, tc.height) {
				t.Fatalf("wrong output dimensions: %v", img.Bounds())
			}
			for _, pixel := range []struct {
				x, y int
				want color.RGBA
			}{
				{tc.width / 2, tc.height / 2, green},
				{0, tc.height / 2, tc.left},
				{tc.width - 1, tc.height / 2, tc.left},
				{tc.width / 2, 0, tc.top},
				{tc.width / 2, tc.height - 1, tc.top},
			} {
				want := pixel.want
				if algorithm == ResizeBilinear && tc.name == "downscale" && pixel.x == tc.width/2 && pixel.y == tc.height/2 {
					// The wider downsampling footprint blends the center box's edge.
					want = color.RGBA{60, 138, 0, 255}
				}
				got := color.RGBAModel.Convert(img.At(pixel.x, pixel.y)).(color.RGBA)
				// Filtering may blend colors near the edge of the center box.
				if max(got.R, want.R)-min(got.R, want.R) > 33 || max(got.G, want.G)-min(got.G, want.G) > 33 || got.A != 255 {
					t.Fatalf("pixel %d,%d: got %v, want %v", pixel.x, pixel.y, got, want)
				}
			}
		})
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := resizePNG(ctx, data.Bytes(), 8, 8, algorithm, true); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation ignored: %v", err)
	}
	if _, err := resizePNG(t.Context(), []byte("invalid PNG"), 8, 8, algorithm, true); err == nil {
		t.Fatal("invalid PNG accepted")
	}
}

func TestResizePNGExtremeSourceAspectRatios(t *testing.T) {
	for _, algorithm := range []ResizeAlgorithm{ResizeBilinear, ResizeCatmullRom, ResizeApproxBilinear, ResizeNearestNeighbor} {
		t.Run(string(algorithm), func(t *testing.T) { testResizePNGExtremeSourceAspectRatios(t, algorithm) })
	}
}

func TestResizePNGCompleteImage(t *testing.T) {
	src := image.NewRGBA(image.Rect(0, 0, 12, 1))
	red, green, blue := color.RGBA{255, 0, 0, 255}, color.RGBA{0, 180, 0, 255}, color.RGBA{0, 0, 255, 255}
	for y := range 1 {
		for x := range 12 {
			c := green
			if x < 4 {
				c = red
			} else if x >= 8 {
				c = blue
			}
			src.Set(x, y, c)
		}
	}
	var data bytes.Buffer
	if err := png.Encode(&data, src); err != nil {
		t.Fatal(err)
	}
	for _, algorithm := range []ResizeAlgorithm{ResizeBilinear, ResizeCatmullRom, ResizeApproxBilinear, ResizeNearestNeighbor} {
		t.Run(string(algorithm), func(t *testing.T) {
			// Width 4 derives a height of 1/3, clamped to one pixel. Complete-image
			// resizing must retain the edges that center cropping would remove.
			out, err := resizePNG(t.Context(), data.Bytes(), 4, 1, algorithm, false)
			if err != nil {
				t.Fatal(err)
			}
			img, err := png.Decode(bytes.NewReader(out))
			if err != nil {
				t.Fatal(err)
			}
			if img.Bounds() != image.Rect(0, 0, 4, 1) {
				t.Fatal("unexpected complete-image output size", img.Bounds())
			}
			for _, pixel := range []struct {
				x    int
				want color.RGBA
			}{{0, red}, {3, blue}} {
				got := color.RGBAModel.Convert(img.At(pixel.x, 0)).(color.RGBA)
				if max(got.R, pixel.want.R)-min(got.R, pixel.want.R) > 33 || max(got.G, pixel.want.G)-min(got.G, pixel.want.G) > 33 || max(got.B, pixel.want.B)-min(got.B, pixel.want.B) > 33 || got.A != 255 {
					t.Fatalf("complete-image resize cropped an edge: %v, want %v", got, pixel.want)
				}
			}
		})
	}
}

func testResizePNGExtremeSourceAspectRatios(t *testing.T, algorithm ResizeAlgorithm) {
	for _, size := range []image.Point{{1, 8192}, {8192, 1}} {
		src := image.NewRGBA(image.Rectangle{Max: size})
		green := color.RGBA{20, 180, 60, 255}
		for y := range size.Y {
			for x := range size.X {
				src.Set(x, y, green)
			}
		}
		var data bytes.Buffer
		if err := png.Encode(&data, src); err != nil {
			t.Fatal(err)
		}
		// A full scaled intermediate would have over 67 million pixels.
		out, err := resizePNG(t.Context(), data.Bytes(), size.Y, size.X, algorithm, true)
		if err != nil {
			t.Fatal(err)
		}
		img, err := png.Decode(bytes.NewReader(out))
		if err != nil {
			t.Fatal(err)
		}
		if img.Bounds() != image.Rect(0, 0, size.Y, size.X) {
			t.Fatalf("wrong output dimensions: %v", img.Bounds())
		}
		for y := range size.X {
			for x := range size.Y {
				if got := color.RGBAModel.Convert(img.At(x, y)); got != green {
					t.Fatalf("extreme aspect ratio left padding at %d,%d: %v", x, y, got)
				}
			}
		}
	}
}
