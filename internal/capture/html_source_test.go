package capture

import (
	"bytes"
	"io"
	"net/http"
	"testing"
)

func TestLoopbackHTMLSource(t *testing.T) {
	const body = "<meta charset=shift_jis><p>\x93\xfa\x96\x7b\x8c\xea</p>"
	for _, contentType := range []string{
		"text/html",
		`text/html; charset="Shift_JIS"; language=ja`,
		"text/html; charset=utf-8",
		"",
	} {
		t.Run(contentType, func(t *testing.T) {
			url, closeSource, err := serveHTML(t.Context(), body, contentType)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := closeSource(); err != nil {
					t.Error(err)
				}
			})
			client := &http.Client{Transport: &http.Transport{Proxy: nil}}
			defer client.CloseIdleConnections()
			response, err := client.Get(url)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			want := contentType
			if want == "" {
				want = "text/html; charset=utf-8"
			}
			if got := response.Header.Get("Content-Type"); got != want {
				t.Fatalf("Content-Type = %q, want %q", got, want)
			}
			data, err := io.ReadAll(response.Body)
			if err != nil || !bytes.Equal(data, []byte(body)) {
				t.Fatalf("body bytes changed: %q, %v", data, err)
			}
		})
	}
}
