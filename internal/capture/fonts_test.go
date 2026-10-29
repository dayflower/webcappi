package capture

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/css"
	"github.com/chromedp/chromedp"
)

// Check rendered fonts, rather than assuming fc-match equals browser behavior.
// Check common family substitutions as well as explicitly installed families.
func TestRenderedFontFamilies(t *testing.T) {
	b := fixtureBrowser(t)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	s, err := b.Open(ctx, filepath.Join(t.TempDir(), "user-data"), "Default", false)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := s.Close(); err != nil {
			t.Error(err)
		}
	}()
	cases := []struct{ family, expected string }{
		{"Arial", "Liberation Sans"},
		{"Times New Roman", "Liberation Serif"},
		{"Courier New", "Liberation Mono"},
		{"Arial Narrow", "Liberation Serif"},
		{"Helvetica", "Liberation Sans"},
		{"Times", "Liberation Serif"},
		{"Courier", "Liberation Mono"},
		{"Palatino", "Liberation Serif"},
		{"Palatino Linotype", "Liberation Serif"},
		{"Bookman Old Style", "Liberation Serif"},
		{"New Century Schoolbook", "Liberation Serif"},
		{"Roboto", "Roboto"},
		{"Noto Sans Mono", "Noto Sans Mono"},
		{"monospace", "Noto Sans Mono"},
	}
	var page strings.Builder
	page.WriteString(`<!doctype html><html lang="en"><meta charset="utf-8">`)
	for i, tc := range cases {
		family := fmt.Sprintf("'%s'", tc.family)
		if tc.family == "monospace" {
			family = "monospace"
		}
		fmt.Fprintf(&page, `<p id="sample%d" style="font-family:%s;font-size:30px;font-weight:400">Hello 012345</p>`, i, family)
	}
	page.WriteString(`</html>`)
	url := "data:text/html;base64," + base64.StdEncoding.EncodeToString([]byte(page.String()))
	if err := chromedp.Do(s.Context, chromedp.Navigate(url)); err != nil {
		t.Fatal(err)
	}
	if _, err := chromedp.Run(s.Context, chromedp.Evaluate[bool](`document.fonts.ready.then(() => { document.body.getBoundingClientRect(); return true; })`, chromedp.EvalAwaitPromise)); err != nil {
		t.Fatal(err)
	}
	if _, err = chromedp.Call(s.Context, css.Enable, cdp.Empty{}); err != nil {
		t.Fatal(err)
	}
	for i, tc := range cases {
		t.Run(tc.family, func(t *testing.T) {
			// Let chromedp synchronize the node with DOM document updates.
			nodes, err := chromedp.Run(s.Context, chromedp.Nodes(chromedp.CSS(fmt.Sprintf("#sample%d", i))))
			if err != nil {
				t.Fatal(err)
			}
			result, err := chromedp.Call(s.Context, css.GetPlatformFontsForNode, css.GetPlatformFontsForNodeParams{NodeID: nodes[0].NodeID})
			if err != nil {
				t.Fatal(err)
			}
			if len(result.Fonts) != 1 || result.Fonts[0].FamilyName != tc.expected {
				fonts, _ := json.Marshal(result.Fonts)
				t.Fatalf("expected %s, rendered %s", tc.expected, fonts)
			}
			if tc.expected == "Noto Sans Mono" && result.Fonts[0].PostScriptName != "NotoSansMono-Regular" {
				t.Fatalf("expected normal-width Regular, rendered %s", result.Fonts[0].PostScriptName)
			}
		})
	}
}

// Exercise the same multilingual page exported by the screenshot tests.
func TestMultilingualRenderedFonts(t *testing.T) {
	b := fixtureBrowser(t)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	page := httptest.NewServer(http.FileServer(http.Dir(filepath.Join(cwd, "../../testdata/page"))))
	defer page.Close()
	s, err := b.Open(ctx, filepath.Join(t.TempDir(), "user-data"), "Default", false)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := s.Close(); err != nil {
			t.Error(err)
		}
	}()
	if err := chromedp.Do(s.Context, chromedp.EmulateViewport(800, 480), chromedp.Navigate(page.URL)); err != nil {
		t.Fatal(err)
	}
	ready, err := chromedp.Run(s.Context, chromedp.Evaluate[bool](`document.fonts.ready.then(() => document.documentElement.scrollWidth <= 800 && document.documentElement.scrollHeight <= 480)`, chromedp.EvalAwaitPromise))
	if err != nil || !ready {
		t.Fatalf("multilingual page does not fit the viewport: %v", err)
	}
	if _, err = chromedp.Call(s.Context, css.Enable, cdp.Empty{}); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ id, family string }{
		{"latin", "Liberation Sans"},
		{"ja", "Noto Sans CJK JP"},
		{"sc", "Noto Sans CJK SC"},
		{"tc", "Noto Sans CJK TC"},
		{"ko", "Noto Sans CJK KR"},
		{"ar", "Noto Kufi Arabic"},
		{"he", "Noto Sans Hebrew"},
		{"th", "Noto Sans Thai"},
		{"hi", "Noto Sans Devanagari"},
		{"ru", "Liberation Sans"},
		{"el", "Liberation Sans"},
		{"emoji", "Noto Color Emoji"},
	} {
		t.Run(tc.id, func(t *testing.T) {
			nodes, err := chromedp.Run(s.Context, chromedp.Nodes(chromedp.CSS("#sample-"+tc.id)))
			if err != nil {
				t.Fatal(err)
			}
			result, err := chromedp.Call(s.Context, css.GetPlatformFontsForNode, css.GetPlatformFontsForNodeParams{NodeID: nodes[0].NodeID})
			if err != nil {
				t.Fatal(err)
			}
			for _, font := range result.Fonts {
				if font.FamilyName == tc.family && font.GlyphCount > 0 && !font.IsCustomFont {
					return
				}
			}
			fonts, _ := json.Marshal(result.Fonts)
			t.Fatalf("expected system font %s, rendered %s", tc.family, fonts)
		})
	}
}
