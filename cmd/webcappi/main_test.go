package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
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
