package httpapi

import (
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/dayflower/webcappi/internal/capture"
)

// readHTMLRequest must run after authentication, query validation and admission.
// MaxBytesReader also needs the writer to preserve oversized-upload handling.
func readHTMLRequest(w http.ResponseWriter, r *http.Request, req capture.Request, maxBytes int64) (capture.Request, *responseError) {
	contentType := r.Header.Get("Content-Type")
	mediaType, params, err := mime.ParseMediaType(contentType)
	if err != nil || mediaType != "text/html" {
		return req, &responseError{415, "Content-Type must be text/html"}
	}
	if req.HTMLMode == capture.HTMLModeFile && params["charset"] != "" && !strings.EqualFold(params["charset"], "utf-8") {
		return req, &responseError{415, "file mode requires UTF-8 encoding"}
	}
	if encoding := r.Header.Get("Content-Encoding"); encoding != "" && !strings.EqualFold(encoding, "identity") {
		return req, &responseError{415, "Content-Encoding is not supported"}
	}
	// Admission precedes body buffering so uploads share the handler bound.
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return req, &responseError{413, fmt.Sprintf("HTML must be <=%d bytes", maxBytes)}
		} else {
			return req, &responseError{400, "could not read HTML body"}
		}
	}
	if len(strings.TrimSpace(string(body))) == 0 {
		return req, &responseError{400, "HTML must be non-empty"}
	}
	if req.HTMLMode == capture.HTMLModeFile && !utf8.Valid(body) {
		return req, &responseError{400, "HTML must be UTF-8 in file mode"}
	}
	req.HTML = string(body)
	req.HTMLContentType = contentType
	return req, nil
}
