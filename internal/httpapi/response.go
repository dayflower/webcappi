package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"

	"github.com/dayflower/webcappi/internal/service"
)

type responseError struct {
	status  int
	message string
}

func writeCaptureError(w http.ResponseWriter, err error, profileName string) {
	switch {
	case errors.Is(err, service.ErrBusy), errors.Is(err, service.ErrClosed):
		w.Header().Set("Retry-After", "1")
		fail(w, 503, err.Error())
	case errors.Is(err, context.DeadlineExceeded):
		fail(w, 504, "capture timed out")
	case errors.Is(err, context.Canceled):
		fail(w, 503, "capture canceled")
	default:
		slog.Warn("capture failed", "profile", profileName, "error", err)
		fail(w, 502, "page capture failed")
	}
}

func writePNG(w http.ResponseWriter, png []byte) {
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Length", strconv.Itoa(len(png)))
	w.WriteHeader(200)
	_, _ = w.Write(png)
}

func fail(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
}
