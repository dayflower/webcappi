package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
)

func TestInitProfileInputAndBrowserFailure(t *testing.T) {
	for _, args := range [][]string{{"-out", ""}, {"unexpected"}, {"-config", "unused"}} {
		if err := runInitProfile(args); err == nil {
			t.Fatalf("invalid initialization arguments accepted: %v", args)
		}
	}
	work := t.TempDir()
	output := filepath.Join(t.TempDir(), "profile.tar")
	err := runInitProfile([]string{"-work-dir", work, "-browser-path", filepath.Join(work, "missing"), "-out", output})
	if err == nil || !strings.Contains(err.Error(), "browser version") {
		t.Fatalf("missing browser did not fail initialization: %v", err)
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatal("failed initialization created an output archive")
	}
	if entries, err := os.ReadDir(work); err != nil || len(entries) != 0 {
		t.Fatalf("failed initialization leaked working files: %v %v", entries, err)
	}
}

func TestInitProfileDoesNotOverwriteOutput(t *testing.T) {
	root := t.TempDir()
	browser := filepath.Join(root, "browser")
	stub := "#!/bin/sh\nprintf '%s\\n' 'Google Chrome for Testing " + capture.BrowserVersion + "'\n"
	if err := os.WriteFile(browser, []byte(stub), 0700); err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(root, "existing.tar")
	if err := os.WriteFile(output, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	err := runInitProfile([]string{"-browser-path", browser, "-work-dir", root, "-out", output})
	if !os.IsExist(err) {
		t.Fatalf("existing archive was not rejected: %v", err)
	}
	if data, err := os.ReadFile(output); err != nil || string(data) != "keep" {
		t.Fatalf("existing archive changed: %q %v", data, err)
	}
}
