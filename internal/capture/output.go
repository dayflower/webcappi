package capture

import (
	"errors"
	"math/bits"
)

// OutputSize resolves omitted dimensions using the captured source aspect ratio.
// Requests retain zero for omitted dimensions so resizing can distinguish a
// complete-image resize from an explicitly sized, center-cropped canvas.
func (r Request) OutputSize(sourceWidth, sourceHeight int) (int, int, error) {
	if sourceWidth < 1 || sourceHeight < 1 || r.OutputWidth < 0 || r.OutputHeight < 0 {
		return 0, 0, errors.New("invalid output or source dimensions")
	}
	w, h := r.OutputWidth, r.OutputHeight
	switch {
	case w == 0 && h == 0:
		return sourceWidth, sourceHeight, nil
	case h == 0:
		var err error
		h, err = proportionalDimension(sourceHeight, w, sourceWidth)
		return w, h, err
	case w == 0:
		var err error
		w, err = proportionalDimension(sourceWidth, h, sourceHeight)
		return w, h, err
	default:
		return w, h, nil
	}
}

// proportionalDimension rounds a positive ratio to the nearest integer, with
// halves rounded up and a minimum of one pixel. The wide product avoids overflow
// even when valid source and target dimensions approach the largest int.
func proportionalDimension(source, target, divisor int) (int, error) {
	hi, lo := bits.Mul64(uint64(source), uint64(target))
	d := uint64(divisor)
	if hi >= d {
		return 0, errors.New("output dimensions overflow")
	}
	n, remainder := bits.Div64(hi, lo, d)
	maxInt := uint64(^uint(0) >> 1)
	if n > maxInt || n == maxInt && remainder >= (d+1)/2 {
		return 0, errors.New("output dimensions overflow")
	}
	if remainder >= (d+1)/2 {
		n++
	}
	return max(1, int(n)), nil
}
