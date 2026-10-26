//go:build ignore

// Exercise the same browser launcher as the API and report Chromium's own sandbox status.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/chromedp"
	"github.com/dayflower/webcappi/internal/capture"
)

func main() {
	if err := check(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func check() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	dir, err := os.MkdirTemp("/tmp", "sandbox-evidence-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	disabled := len(os.Args) == 2 && os.Args[1] == "disabled"
	b := capture.Browser{Path: "/opt/chrome/chrome", NoSandbox: disabled}
	if err := b.Check(ctx); err != nil {
		return err
	}
	session, err := b.Open(ctx, dir+"/user-data", "Default", false)
	if err != nil {
		return err
	}
	defer session.Close()
	if err := chromedp.Do(session.Context, chromedp.Navigate("chrome://sandbox")); err != nil {
		return err
	}
	text, err := chromedp.Run(session.Context, chromedp.Evaluate[string]("document.body.innerText"))
	if err != nil {
		return err
	}
	command, err := chromedp.Call(session.Context, browser.GetBrowserCommandLine, cdp.Empty{})
	if err != nil {
		return err
	}
	args := command.Arguments
	processes, err := CollectChromeProcessEvidence(ctx, dir+"/user-data")
	if err != nil {
		return err
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"chrome": capture.BrowserVersion, "sandbox_page": text, "arguments": args, "no_sandbox": disabled, "processes": processes}); err != nil {
		return err
	}
	actual := false
	for _, arg := range args {
		if arg == "--no-sandbox" {
			actual = true
		}
	}
	if actual != disabled {
		return fmt.Errorf("browser invocation has unexpected sandbox mode")
	}
	if !disabled {
		if processes.Validation.Result != "additional-renderer-filter-observed" {
			return fmt.Errorf("Chrome renderer filter not demonstrated: %+v", processes.Validation)
		}
		rows := make(map[string]string)
		for _, line := range strings.Split(text, "\n") {
			label, value, ok := strings.Cut(line, "\t")
			if ok {
				rows[label] = value
			}
		}
		for label, expected := range map[string]string{
			"Layer 1 Sandbox":                    "Namespace",
			"PID namespaces":                     "Yes",
			"Network namespaces":                 "Yes",
			"Seccomp-BPF sandbox":                "Yes",
			"Seccomp-BPF sandbox supports TSYNC": "Yes",
		} {
			if rows[label] != expected {
				return fmt.Errorf("Chromium sandbox evidence missing %q=%q: %s", label, expected, text)
			}
		}
	}
	return nil
}
