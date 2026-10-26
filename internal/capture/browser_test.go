package capture

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/chromedp/chromedp"
)

func fixtureBrowser(t *testing.T) Browser {
	t.Helper()
	path := os.Getenv("WEBCAPPI_TEST_BROWSER")
	if path == "" {
		t.Skip("set WEBCAPPI_TEST_BROWSER to run real browser tests")
	}
	b := Browser{Path: path, NoSandbox: os.Getenv("WEBCAPPI_TEST_NO_SANDBOX") == "1"}
	if err := b.Check(t.Context()); err != nil {
		t.Fatal(err)
	}
	return b
}

func treeDigest(t *testing.T, root string) string {
	t.Helper()
	h := sha256.New()
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		fmt.Fprint(h, rel, d.Type())
		if d.Type().IsRegular() {
			b, e := os.ReadFile(path)
			if e != nil {
				return e
			}
			h.Write(b)
		}
		if d.Type()&os.ModeSymlink != 0 {
			s, e := os.Readlink(path)
			if e != nil {
				return e
			}
			fmt.Fprint(h, s)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}

func TestExtensionProfileLifecycle(t *testing.T) {
	b := fixtureBrowser(t)
	initialTemp, _ := filepath.Glob("/tmp/wc-*")
	cwd, _ := os.Getwd()
	fixtures := filepath.Join(cwd, "../../testdata")
	extension, _ := filepath.Abs(filepath.Join(fixtures, "extension"))
	page := httptest.NewServer(http.FileServer(http.Dir(filepath.Join(fixtures, "page"))))
	defer page.Close()
	root := t.TempDir()
	template := filepath.Join(root, "template")
	ctx, cancel := context.WithTimeout(t.Context(), 60*time.Second)
	defer cancel()
	if prepared := os.Getenv("WEBCAPPI_TEST_PREPARED_USER_DATA"); prepared != "" {
		template = prepared
	} else {
		if err := os.Mkdir(template, 0700); err != nil {
			t.Fatal(err)
		}
		t.Log("preparing headed profile")
		s, err := b.Open(ctx, template, "Default", true)
		if err != nil {
			t.Fatal(err)
		}
		if err = chromedp.Do(s.Context, chromedp.Navigate("chrome://extensions/")); err != nil {
			t.Fatal(err)
		}
		if _, err = chromedp.Run(s.Context, chromedp.Evaluate[bool](`(async () => { await chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true}); return true; })()`, chromedp.EvalAwaitPromise)); err != nil {
			t.Fatal(err)
		}
		if _, err = chromedp.Run(s.Context, chromedp.Evaluate[chromedp.Void](`chrome.developerPrivate.loadUnpacked({}).then(() => { window.fixtureInstalled = true; }).catch(e => { window.fixtureError = String(e); });`)); err != nil {
			t.Fatal(err)
		}
		window, err := exec.CommandContext(ctx, "xdotool", "search", "--sync", "--onlyvisible", "--name", "Select.*extension").Output()
		if err != nil {
			t.Fatal(err)
		}
		wid := strings.Fields(string(window))[0]
		for _, args := range [][]string{{"windowfocus", "--sync", wid}, {"key", "ctrl+l"}, {"type", "--clearmodifiers", extension}} {
			if output, err := exec.CommandContext(ctx, "xdotool", args...).CombinedOutput(); err != nil {
				t.Fatalf("xdotool: %s: %v", output, err)
			}
			// Allow GTK to process focus/location changes before the next native key.
			// This delay belongs only to the test's file picker, not capture readiness.
			time.Sleep(150 * time.Millisecond)
		}
		time.Sleep(500 * time.Millisecond)
		geometry, err := exec.CommandContext(ctx, "xdotool", "getwindowgeometry", "--shell", wid).Output()
		if err != nil {
			t.Fatal(err)
		}
		var width, height int
		for _, line := range strings.Split(string(geometry), "\n") {
			if strings.HasPrefix(line, "WIDTH=") {
				fmt.Sscanf(line, "WIDTH=%d", &width)
			}
			if strings.HasPrefix(line, "HEIGHT=") {
				fmt.Sscanf(line, "HEIGHT=%d", &height)
			}
		}
		if output, err := exec.CommandContext(ctx, "xdotool", "mousemove", "--window", wid, fmt.Sprint(width-50), fmt.Sprint(height-22), "click", "1").CombinedOutput(); err != nil {
			t.Fatalf("xdotool: %s: %v", output, err)
		}
		if _, err = chromedp.Run(s.Context, chromedp.Poll[bool](`window.fixtureInstalled === true || !!window.fixtureError`, chromedp.WithPollingInterval(50*time.Millisecond))); err != nil {
			t.Fatal(err)
		}
		if result, err := chromedp.Run(s.Context, chromedp.Evaluate[string](`window.fixtureError || ''`)); err != nil || result != "" {
			t.Fatalf("native install: %s: %v", result, err)
		}
		// File access is a saved, explicit preparation step, never a runtime edit.
		if _, err = chromedp.Run(s.Context, chromedp.Evaluate[bool](`(async () => {
	 await chrome.developerPrivate.updateExtensionConfiguration({extensionId:'gkofocaippcmakpnppmdankgpgikcgnb',fileAccess:true});
	 return true;
	})()`, chromedp.EvalAwaitPromise)); err != nil {
			t.Fatal(err)
		}
		// Load the local page in a deliberately new target, just as production does.
		tab, stop := chromedp.NewContext(s.Context)
		t.Log("navigating headed target")
		if err = chromedp.Do(tab, chromedp.Navigate(page.URL)); err != nil {
			t.Fatal(err)
		}
		t.Log("waiting for installed extension")
		if _, err = chromedp.Run(tab, chromedp.Poll[bool](`document.querySelector('#webcappi-extension-ready')?.dataset.installCount === '1'`, chromedp.WithPollingInterval(50*time.Millisecond))); err != nil {
			text, _ := chromedp.Run(tab, chromedp.Evaluate[string](`document.body.outerHTML`))
			t.Log(text)
			t.Fatal(err)
		}
		targets, err := chromedp.Targets(s.Context)
		if err != nil {
			t.Fatal(err)
		}
		welcomes := 0
		for _, target := range targets {
			if strings.HasSuffix(target.URL, "/welcome.html") {
				welcomes++
			}
		}
		if welcomes < 2 {
			t.Fatalf("expected install welcome and deliberately opened tab, got %d", welcomes)
		}
		stop()
		if err = s.Close(); err != nil {
			t.Fatal(err)
		}
	}
	before := treeDigest(t, template)
	if out := os.Getenv("WEBCAPPI_TEST_ARTIFACTS"); out != "" {
		if err := CopyProfile(ctx, template, filepath.Join(out, "user-data"), 512<<20); err != nil {
			t.Fatal(err)
		}
	}

	// Two independently cloned sessions preserve extension data, without a new
	// onInstalled welcome. Closing Chromium flushes the original LevelDB first.
	for i := 0; i < 2; i++ {
		clone := filepath.Join(root, fmt.Sprintf("clone-%d", i))
		if err := CopyProfile(ctx, template, clone, 512<<20); err != nil {
			t.Fatal(err)
		}
		s, err := b.Open(ctx, clone, "Default", false)
		if err != nil {
			t.Fatal(err)
		}
		tab, stop := chromedp.NewContext(s.Context)
		if err = chromedp.Do(tab, chromedp.Navigate(page.URL)); err != nil {
			t.Fatal(err)
		}
		if _, err = chromedp.Run(tab, chromedp.Poll[bool](`document.querySelector('#webcappi-extension-ready')?.dataset.installCount === '1'`, chromedp.WithPollingInterval(50*time.Millisecond))); err != nil {
			t.Fatal(err)
		}
		targets, err := chromedp.Targets(s.Context)
		if err != nil {
			t.Fatal(err)
		}
		welcomes := 0
		for _, target := range targets {
			if strings.HasSuffix(target.URL, "/welcome.html") {
				welcomes++
			}
		}
		if welcomes != 1 {
			t.Fatalf("clone unexpectedly installed extension again: %d welcome tabs", welcomes)
		}
		stop()
		if err = s.Close(); err != nil {
			t.Fatal(err)
		}
	}
	work := filepath.Join(root, "work")
	os.Mkdir(work, 0700)
	renderer := Renderer{Browser: b, WorkDir: work, MaxProfileBytes: 512 << 20}
	req := Request{URL: page.URL, Width: 800, Height: 480, Profile: Profile{UserDataDir: template, ProfileDirectory: "Default", Extensions: []string{extension}, ReadySelectors: []string{"#webcappi-extension-ready"}}}
	image, err := renderer.Capture(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	img, err := png.Decode(bytes.NewReader(image))
	if err != nil {
		t.Fatal(err)
	}
	if img.Bounds().Dx() != 800 || img.Bounds().Dy() != 480 {
		t.Fatalf("wrong size: %v", img.Bounds())
	}
	red, green, blue, _ := img.At(799, 20).RGBA()
	if red>>8 != 20 || green>>8 != 180 || blue>>8 != 60 {
		t.Fatalf("extension marker missing: %d,%d,%d", red>>8, green>>8, blue>>8)
	}
	if out := os.Getenv("WEBCAPPI_TEST_ARTIFACTS"); out != "" {
		if err = os.WriteFile(filepath.Join(out, "extension.png"), image, 0600); err != nil {
			t.Fatal(err)
		}
	}
	htmlReq := req
	htmlReq.URL = ""
	htmlReq.HTML = "<!doctype html><p>Posted HTML with an installed extension</p>"
	for _, mode := range []string{HTMLModeFile, HTMLModeLoopback} {
		htmlReq.HTMLMode = mode
		htmlImage, err := renderer.Capture(ctx, htmlReq)
		if err != nil {
			t.Fatal(err)
		}
		htmlPNG, err := png.Decode(bytes.NewReader(htmlImage))
		if err != nil {
			t.Fatal(err)
		}
		red, green, blue, _ = htmlPNG.At(799, 20).RGBA()
		if red>>8 != 20 || green>>8 != 180 || blue>>8 != 60 {
			t.Fatalf("%s HTML extension marker missing: %d,%d,%d", mode, red>>8, green>>8, blue>>8)
		}
	}
	if before != treeDigest(t, template) {
		t.Fatal("template changed")
	}
	entries, _ := os.ReadDir(work)
	if len(entries) != 0 {
		t.Fatalf("work directory leaked: %v", entries)
	}
	// A missing readiness selector exercises timeout, process cleanup and deletion.
	req.Profile.ReadySelectors = []string{"#never-ready"}
	short, stopShort := context.WithTimeout(t.Context(), 1500*time.Millisecond)
	_, err = renderer.Capture(short, req)
	stopShort()
	if err == nil {
		t.Fatal("expected timeout")
	}
	entries, _ = os.ReadDir(work)
	if len(entries) != 0 {
		t.Fatal("timeout leaked working files")
	}
	req.Profile.ReadySelectors = nil
	req.URL = "http://127.0.0.1:1/"
	_, err = renderer.Capture(ctx, req)
	if err == nil {
		t.Fatal("expected navigation error")
	}
	entries, _ = os.ReadDir(work)
	if len(entries) != 0 {
		t.Fatal("failure leaked working files")
	}
	if before != treeDigest(t, template) {
		t.Fatal("failure changed template")
	}
	// /proc should have no live Chromium referencing any of our private dirs.
	entries, _ = os.ReadDir("/proc")
	for _, entry := range entries {
		cmdline, _ := os.ReadFile("/proc/" + entry.Name() + "/cmdline")
		if bytes.Contains(cmdline, []byte("--user-data-dir="+root)) || bytes.HasPrefix(cmdline, []byte(b.Path+"\x00")) {
			t.Fatalf("browser survived cleanup: %s", cmdline)
		}
	}
	remainingTemp, _ := filepath.Glob("/tmp/wc-*")
	if len(remainingTemp) != len(initialTemp) {
		t.Fatalf("browser temporary directories leaked: %v", remainingTemp)
	}
}
