package capture

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	HTMLModeFile     = "file"
	HTMLModeLoopback = "loopback"
)

func prepareHTML(ctx context.Context, work, html, mode, contentType string) (string, func() error, error) {
	switch mode {
	case "", HTMLModeFile:
		u, err := writeHTML(work, html)
		// The capture's work-directory cleanup owns the file's lifetime.
		return u, func() error { return nil }, err
	case HTMLModeLoopback:
		return serveHTML(ctx, html, contentType)
	default:
		return "", nil, fmt.Errorf("unsupported HTML mode %q", mode)
	}
}

// Store HTML alongside the disposable profile so normal capture cleanup removes
// it on success, failure or cancellation. A BOM selects UTF-8 without HTTP headers.
func writeHTML(work, html string) (string, error) {
	if !strings.HasPrefix(html, "\ufeff") {
		html = "\ufeff" + html
	}
	path, err := filepath.Abs(filepath.Join(work, "page.html"))
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(html), 0600); err != nil {
		return "", err
	}
	return (&url.URL{Scheme: "file", Path: path}).String(), nil
}

// Expose only the posted document, on an ephemeral IPv4 loopback listener.
// The returned cleanup closes connections and waits for the serving goroutine.
func serveHTML(ctx context.Context, html, contentType string) (string, func() error, error) {
	// Direct renderer callers without HTTP request headers retain the UTF-8 default.
	if contentType == "" {
		contentType = "text/html; charset=utf-8"
	}
	var lc net.ListenConfig
	listener, err := lc.Listen(ctx, "tcp", "127.0.0.1:0")
	if err != nil {
		return "", nil, err
	}
	server := &http.Server{
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		IdleTimeout:       5 * time.Second,
		MaxHeaderBytes:    32 << 10,
		BaseContext:       func(net.Listener) context.Context { return ctx },
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("X-Content-Type-Options", "nosniff")
			if r.Method != http.MethodGet || r.URL.Path != "/" {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", contentType)
			_, _ = io.WriteString(w, html)
		}),
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = server.Serve(listener)
	}()
	return "http://" + listener.Addr().String() + "/", func() error {
		err := server.Close()
		<-done
		return err
	}, nil
}
