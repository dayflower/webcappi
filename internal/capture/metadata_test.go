package capture

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/chromedp/chromedp"
)

func TestCaptureMetadataDOM(t *testing.T) {
	b := fixtureBrowser(t)
	const html = `<!doctype html><meta charset=utf-8>
<div id=empty data-count=""></div><div class=duplicate></div><div class=duplicate></div>
<div id=hidden hidden>  日本語
 😀 </div><iframe srcdoc="<p id=inside>iframe</p>"></iframe>
<div id=under></div><div id=limit></div><div id=over></div><div id=long></div><div id=surrogate></div>
<script>
 document.querySelector('#under').textContent = '界'.repeat(341);
 document.querySelector('#limit').textContent = '界'.repeat(341) + 'x';
 document.querySelector('#over').textContent = '界'.repeat(341) + 'é';
 document.querySelector('#long').textContent = 'x'.repeat(1025);
 document.querySelector('#surrogate').textContent = '\uD800😀\uDC00' + 'x'.repeat(1014);
 document.querySelectorAll = () => { throw new Error('page override'); };
 Element.prototype.getAttribute = () => 'page override';
 window.TextEncoder = undefined;
</script>`
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	url, closeHTML, err := prepareHTML(ctx, t.TempDir(), html, HTMLModeFile, "")
	if err != nil {
		t.Fatal(err)
	}
	defer closeHTML()
	browser, err := b.Open(ctx, t.TempDir(), "Default", false)
	if err != nil {
		t.Fatal(err)
	}
	defer browser.Close()
	tab, closeTab := chromedp.NewContext(browser.Context)
	defer closeTab()
	if err := chromedp.Do(tab, chromedp.Navigate(url)); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name, selector, attribute, value, failure string
	}{
		{name: "empty attribute", selector: "#empty", attribute: "data-count"},
		{name: "empty text", selector: "#empty"},
		{name: "missing attribute", selector: "#empty", attribute: "missing", failure: "capture metadata attribute is missing"},
		{name: "invalid CSS", selector: "[:private-selector", failure: "invalid capture metadata selector"},
		{name: "no match", selector: "#absent", failure: "capture metadata selector matched no elements"},
		{name: "multiple matches", selector: ".duplicate", failure: "capture metadata selector matched multiple elements"},
		{name: "hidden element", selector: "#hidden", value: "  日本語\n 😀 "},
		{name: "iframe document excluded", selector: "#inside", failure: "capture metadata selector matched no elements"},
		{name: "1023 bytes", selector: "#under", value: strings.Repeat("界", 341)},
		{name: "1024 bytes", selector: "#limit", value: strings.Repeat("界", 341) + "x"},
		{name: "1025 bytes", selector: "#over", failure: "capture metadata exceeds size limit"},
		{name: "1025 UTF-16 code units", selector: "#long", failure: "capture metadata exceeds size limit"},
		{name: "unpaired surrogates", selector: "#surrogate", value: "�😀�" + strings.Repeat("x", 1014)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			metadata := MetadataRequest{Method: MetadataTextContent, Selector: tc.selector}
			if tc.attribute != "" {
				metadata.Method, metadata.Attribute = MetadataAttributeValue, tc.attribute
			}
			value, err := readCaptureMetadata(tab, &metadata)
			if tc.failure != "" {
				if err == nil || err.Error() != tc.failure || value != nil {
					t.Fatalf("metadata = %v, error = %v, want %q", value, err, tc.failure)
				}
				return
			}
			if err != nil || value == nil || *value != tc.value {
				t.Fatalf("capture metadata did not match for %q, error %v", tc.selector, err)
			}
		})
	}
}
