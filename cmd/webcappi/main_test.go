package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
)

func TestInvalidAuthFailsBeforeBrowserStartup(t *testing.T) {
	b, err := os.ReadFile("../../config.example.json")
	if err != nil {
		t.Fatal(err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(b, &cfg); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	// A missing browser makes it observable if authentication is checked too late.
	cfg["browser_path"] = filepath.Join(root, "missing-browser")
	path := filepath.Join(root, "config.json")
	for _, contents := range []string{"", "invalid token"} {
		token := filepath.Join(root, "token")
		if err := os.WriteFile(token, []byte(contents), 0600); err != nil {
			t.Fatal(err)
		}
		cfg["auth"] = map[string]any{"bearer_token_file": token}
		b, err := json.Marshal(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, b, 0600); err != nil {
			t.Fatal(err)
		}
		for _, command := range []string{"serve", "check", "capture"} {
			if err := run([]string{command, "-config", path}); err == nil || !strings.Contains(err.Error(), "auth.bearer_token_file") {
				t.Fatalf("%s did not reject invalid authentication first: %v", command, err)
			}
		}
	}
}

func TestMissingDefaultProfileReachesBrowserStartup(t *testing.T) {
	b, err := os.ReadFile("../../config.example.json")
	if err != nil {
		t.Fatal(err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(b, &cfg); err != nil {
		t.Fatal(err)
	}
	profiles := cfg["profiles"].(map[string]any)
	profiles["alternate"] = profiles["default"]
	delete(profiles, "default")
	delete(cfg, "auth")
	cfg["work_dir"] = t.TempDir()
	cfg["browser_path"] = filepath.Join(t.TempDir(), "missing-browser")
	b, err = json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
	for _, command := range []string{"serve", "check", "capture"} {
		if err := run([]string{command, "-config", path}); err == nil || !strings.Contains(err.Error(), "missing-browser") {
			t.Fatalf("%s did not reach browser startup: %v", command, err)
		}
	}
}

func TestProfileStartupFailures(t *testing.T) {
	root := t.TempDir()
	browser := filepath.Join(root, "chrome")
	script := "#!/bin/sh\nif [ \"$1\" = --version ]; then\n echo 'Google Chrome for Testing " + capture.BrowserVersion + "'\n exit 0\nfi\necho 'intentional browser startup failure' >&2\nexit 1\n"
	if err := os.WriteFile(browser, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	for _, provided := range []bool{false, true} {
		work := t.TempDir()
		cfg := map[string]any{"browser_path": browser, "work_dir": work}
		want := "intentional browser startup failure"
		if provided {
			want = filepath.Join(root, "missing-template")
			cfg["profiles"] = map[string]capture.Profile{"default": {UserDataDir: want, ProfileDirectory: "Default"}}
		}
		data, err := json.Marshal(cfg)
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(root, "config.json")
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
		if err := run([]string{"check", "-config", path}); err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("provided=%t: expected %q, got %v", provided, want, err)
		}
		entries, err := os.ReadDir(work)
		if err != nil || len(entries) != 0 {
			t.Fatalf("failed startup leaked files: %v, %v", entries, err)
		}
	}
}
