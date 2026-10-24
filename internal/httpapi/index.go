package httpapi

import (
	"bytes"
	_ "embed"
	"html/template"
	"net/http"
)

//go:embed index.html
var indexHTML string

var indexTemplate = template.Must(template.New("index").Parse(indexHTML))

type indexData struct {
	AuthEnabled           bool
	MaxWidth              int
	MaxHeight             int
	MaxPixels             int64
	MaxPOSTBytes          int64
	CaptureTimeoutSeconds int
}

func (a *API) index(w http.ResponseWriter) {
	data := indexData{
		AuthEnabled:           a.cfg.Auth != nil,
		MaxWidth:              a.cfg.MaxWidth,
		MaxHeight:             a.cfg.MaxHeight,
		MaxPixels:             a.cfg.MaxPixels,
		MaxPOSTBytes:          a.cfg.MaxPOSTBytes,
		CaptureTimeoutSeconds: a.cfg.CaptureTimeoutSeconds,
	}
	var page bytes.Buffer
	if err := indexTemplate.Execute(&page, data); err != nil {
		fail(w, http.StatusInternalServerError, "could not render guide")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(page.Bytes())
}
