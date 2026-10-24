package capture

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
)

const (
	DefaultViewportWidth  = 1280
	DefaultViewportHeight = 720
)

type Profile struct {
	UserDataDir      string   `json:"user_data_dir"`
	ProfileDirectory string   `json:"profile_directory"`
	Extensions       []string `json:"extensions"`
	ReadySelectors   []string `json:"ready_selectors,omitempty"`
}

type Request struct {
	URL             string
	HTML            string
	HTMLMode        string
	HTMLContentType string
	Width           int
	Height          int
	OutputWidth     int
	OutputHeight    int
	ProfileName     string
	Profile         Profile
	ReadySelector   string
	DelayMS         int
}

// OutputSize defaults each omitted output dimension to its viewport dimension.
func (r Request) OutputSize() (int, int) {
	w, h := r.OutputWidth, r.OutputHeight
	if w == 0 {
		w = r.Width
	}
	if h == 0 {
		h = r.Height
	}
	return w, h
}

type Renderer struct {
	Browser         Browser
	WorkDir         string
	MaxProfileBytes int64
	ResizeAlgorithm ResizeAlgorithm
}

// Check starts a browser on a disposable clone without contacting a website.
// A missing sandbox or unusable bundle fails startup before readiness is served.
func (r Renderer) Check(ctx context.Context, profile Profile) (err error) {
	work, err := os.MkdirTemp(r.WorkDir, "check-")
	if err != nil {
		return err
	}
	defer func() {
		if e := os.RemoveAll(work); err == nil {
			err = e
		}
	}()
	data := filepath.Join(work, "user-data")
	if err = CopyProfile(ctx, profile.UserDataDir, data, r.MaxProfileBytes); err != nil {
		return err
	}
	s, err := r.Browser.Open(ctx, data, profile.ProfileDirectory, false)
	if err != nil {
		return err
	}
	defer func() {
		if e := s.Close(); err == nil {
			err = e
		}
	}()
	if len(profile.Extensions) > 0 {
		if err = chromedp.Do(s.Context, chromedp.Navigate("chrome://extensions/")); err != nil {
			return err
		}
		type extensionInfo struct {
			Path  string `json:"path"`
			State string `json:"state"`
		}
		var installed []extensionInfo
		installed, err = chromedp.Run(s.Context, chromedp.Evaluate[[]extensionInfo](`chrome.developerPrivate.getExtensionsInfo({includeDisabled:true})`, chromedp.EvalAwaitPromise))
		if err != nil {
			return err
		}
		for _, path := range profile.Extensions {
			found := false
			for _, ext := range installed {
				if ext.Path == path && ext.State == "ENABLED" {
					found = true
					break
				}
			}
			if !found {
				return fmt.Errorf("extension %s is not enabled in the cloned profile", path)
			}
		}
	}
	return nil
}

func (r Renderer) Capture(ctx context.Context, req Request) (png []byte, err error) {
	slog.Info("capture started", "profile", req.ProfileName, "width", req.Width, "height", req.Height)
	work, err := os.MkdirTemp(r.WorkDir, "capture-")
	if err != nil {
		return nil, err
	}
	defer func() {
		if e := os.RemoveAll(work); e != nil && err == nil {
			err = fmt.Errorf("remove work directory: %w", e)
		}
	}()
	data := filepath.Join(work, "user-data")
	if err = CopyProfile(ctx, req.Profile.UserDataDir, data, r.MaxProfileBytes); err != nil {
		return nil, fmt.Errorf("copy profile: %w", err)
	}
	s, err := r.Browser.Open(ctx, data, req.Profile.ProfileDirectory, false)
	if err != nil {
		return nil, err
	}
	defer func() {
		if e := s.Close(); e != nil && err == nil {
			err = fmt.Errorf("close browser: %w", e)
		}
	}()
	// A child context creates a new target in the persistent default browser context.
	// Never adopt whichever startup/welcome tab happens to be first.
	tab, closeTab := chromedp.NewContext(s.Context)
	defer closeTab()
	if err = chromedp.Do(tab, chromedp.EmulateViewport(int64(req.Width), int64(req.Height), chromedp.EmulateScale(1))); err != nil {
		return nil, err
	}
	url := req.URL
	if req.HTML != "" {
		var closeHTML func() error
		url, closeHTML, err = prepareHTML(ctx, work, req.HTML, req.HTMLMode, req.HTMLContentType)
		if err != nil {
			return nil, fmt.Errorf("prepare HTML: %w", err)
		}
		defer func() {
			if e := closeHTML(); e != nil && err == nil {
				err = fmt.Errorf("close HTML source: %w", e)
			}
		}()
	}
	response, err := chromedp.Run(tab, chromedp.NavigateResponse(url))
	if err != nil {
		return nil, fmt.Errorf("navigate: %w", err)
	}
	if response != nil && response.Status >= 400 {
		return nil, fmt.Errorf("page returned HTTP %d", response.Status)
	}
	selectors := append([]string{}, req.Profile.ReadySelectors...)
	if req.ReadySelector != "" {
		selectors = append(selectors, req.ReadySelector)
	}
	if len(selectors) > 0 {
		encoded, _ := json.Marshal(selectors)
		if _, err = chromedp.Run(tab, chromedp.Poll[bool](string(encoded)+".every(selector => !!document.querySelector(selector))", chromedp.WithPollingInterval(50_000_000))); err != nil {
			return nil, fmt.Errorf("ready selector: %w", err)
		}
	}
	if _, err = chromedp.Call(tab, page.BringToFront, cdp.Empty{}); err != nil {
		return nil, err
	}
	// DOM selectors and extension signals precede generic asset checks, so assets
	// inserted by the extension participate too. No network-idle heuristic.
	if _, err = chromedp.Run(tab, chromedp.Evaluate[bool](assetsReady, chromedp.EvalAwaitPromise)); err != nil {
		return nil, fmt.Errorf("assets: %w", err)
	}
	if req.DelayMS > 0 {
		timer := time.NewTimer(time.Duration(req.DelayMS) * time.Millisecond)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-tab.Done():
			return nil, tab.Err()
		}
	}
	png, err = chromedp.Run(tab, chromedp.CaptureScreenshot())
	if err != nil {
		return nil, err
	}
	w, h := req.OutputSize()
	if w == req.Width && h == req.Height {
		return png, nil
	}
	algorithm := r.ResizeAlgorithm
	if algorithm == "" {
		algorithm = ResizeBilinear
	}
	return resizePNG(ctx, png, w, h, algorithm)
}

const assetsReady = `(async () => {
 await document.fonts.ready;
 const visible = [...document.images].filter(img => {
   const r = img.getBoundingClientRect();
   return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
 });
 await Promise.all(visible.map(async img => {
   if (!img.complete) await new Promise((resolve, reject) => {
     img.addEventListener('load', resolve, {once:true});
     img.addEventListener('error', () => reject(new Error('image load failed')), {once:true});
   });
   if (!img.naturalWidth) throw new Error('image load failed');
   await img.decode();
 }));
 await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
 return true;
})()`
