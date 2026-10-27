package capture

import "testing"

func TestOutputSize(t *testing.T) {
	maxInt := int(^uint(0) >> 1)
	for _, tc := range []struct {
		name                  string
		sw, sh, width, height int
		wantW, wantH          int
		wantErr               bool
	}{
		{"original", 1200, 800, 0, 0, 1200, 800, false},
		{"width", 1200, 800, 600, 0, 600, 400, false},
		{"height", 1200, 800, 0, 400, 600, 400, false},
		{"explicit canvas", 1200, 800, 600, 600, 600, 600, false},
		{"round up", 1200, 800, 400, 0, 400, 267, false},
		{"round down", 3, 1, 4, 0, 4, 1, false},
		{"half up height", 2, 1, 3, 0, 3, 2, false},
		{"half up width", 1, 2, 0, 3, 2, 3, false},
		{"minimum height", 4096, 1, 1, 0, 1, 1, false},
		{"minimum width", 1, 4096, 0, 1, 1, 1, false},
		{"enlarge", 400, 1200, 800, 0, 800, 2400, false},
		{"wide product fits", maxInt, maxInt, maxInt, 0, maxInt, maxInt, false},
		{"wide product fits height", maxInt, maxInt, 0, maxInt, maxInt, maxInt, false},
		{"height overflows", 1, maxInt, 2, 0, 0, 0, true},
		{"width overflows", maxInt, 1, 0, 2, 0, 0, true},
		{"wide quotient overflows", 1, maxInt, maxInt, 0, 0, 0, true},
		{"invalid source", 0, 800, 600, 0, 0, 0, true},
		{"invalid output", 1200, 800, -1, 0, 0, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := Request{OutputWidth: tc.width, OutputHeight: tc.height}
			w, h, err := req.OutputSize(tc.sw, tc.sh)
			if tc.wantErr {
				if err == nil {
					t.Fatal("invalid or overflowing dimensions accepted")
				}
				return
			}
			if err != nil || w != tc.wantW || h != tc.wantH {
				t.Fatalf("size = %dx%d, error = %v; want %dx%d", w, h, err, tc.wantW, tc.wantH)
			}
		})
	}
}
