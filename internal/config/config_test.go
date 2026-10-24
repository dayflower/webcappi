package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
)

func TestBundleChecks(t *testing.T) {
	root := t.TempDir()
	work := filepath.Join(root, "work")
	os.Mkdir(work, 0700)
	bundle := filepath.Join(root, "bundle")
	data := filepath.Join(bundle, "user-data")
	os.MkdirAll(filepath.Join(data, "Default"), 0700)
	ext := filepath.Join(bundle, "extension")
	os.Mkdir(ext, 0700)
	manifest, err := os.ReadFile("../../testdata/extension/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(ext, "manifest.json"), manifest, 0600)
	p := capture.Profile{UserDataDir: data, ProfileDirectory: "Default", Extensions: []string{ext}}
	descriptor, _ := json.Marshal(p)
	os.WriteFile(filepath.Join(bundle, "bundle.json"), descriptor, 0600)
	os.WriteFile(filepath.Join(data, "Local State"), []byte("{}"), 0600)
	prefs := map[string]any{"extensions": map[string]any{"settings": map[string]any{"gkofocaippcmakpnppmdankgpgikcgnb": map[string]any{"path": ext, "location": 4, "disable_reasons": []int{}}}}}
	encoded, _ := json.Marshal(prefs)
	os.WriteFile(filepath.Join(data, "Default", "Preferences"), encoded, 0600)
	c := Defaults()
	c.WorkDir = work
	c.Profiles = map[string]capture.Profile{"default": p}
	if err = c.Validate(); err != nil {
		t.Fatal(err)
	}
	if err = c.CheckFiles(t.Context()); err != nil {
		t.Fatal(err)
	}
	p.ReadySelectors = []string{"#ready"}
	c.Profiles["default"] = p
	if err = c.CheckFiles(t.Context()); err == nil {
		t.Fatal("mismatched bundle allowed")
	}
	p.ReadySelectors = nil
	p.UserDataDir = work
	c.Profiles["default"] = p
	if err = c.Validate(); err == nil {
		t.Fatal("template inside work allowed")
	}
	p.UserDataDir = data
	p.ProfileDirectory = "../Default"
	c.Profiles["default"] = p
	if err = c.Validate(); err == nil {
		t.Fatal("profile directory traversal allowed")
	}
}

func TestConfigDecodeAndDefaults(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	for _, input := range []string{`{"typo":1}`, `{} {}`, `null`} {
		os.WriteFile(path, []byte(input), 0600)
		if _, err := Load(path); err == nil {
			t.Fatalf("accepted %s", input)
		}
	}
	if c := Defaults(); c.Concurrency != 1 || c.NoSandbox || !strings.HasPrefix(c.BrowserPath, "/") {
		t.Fatal("unexpected defaults")
	}
}

func TestAuthConfig(t *testing.T) {
	input, err := os.ReadFile("../../config.example.json")
	if err != nil {
		t.Fatal(err)
	}
	var base map[string]any
	if err := json.Unmarshal(input, &base); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "config.json")
	for _, tc := range []struct {
		name string
		auth any
		ok   bool
	}{
		{"omitted", nil, true},
		{"enabled", map[string]any{"bearer_token_file": "/run/secrets/webcappi/token"}, true},
		{"empty object", map[string]any{}, false},
		{"empty path", map[string]any{"bearer_token_file": ""}, false},
		{"relative path", map[string]any{"bearer_token_file": "token"}, false},
		{"unknown field", map[string]any{"token_file": "/token"}, false},
		{"wrong type", "token", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			delete(base, "auth")
			if tc.auth != nil {
				base["auth"] = tc.auth
			}
			b, err := json.Marshal(base)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, b, 0600); err != nil {
				t.Fatal(err)
			}
			c, err := Load(path)
			if (err == nil) != tc.ok {
				t.Fatalf("unexpected validation result: %v", err)
			}
			if err == nil && (c.Auth != nil) != (tc.auth != nil) {
				t.Fatal("auth configuration not preserved")
			}
		})
	}
}

func TestMaxPOSTBytesConfig(t *testing.T) {
	input, err := os.ReadFile("../../config.example.json")
	if err != nil {
		t.Fatal(err)
	}
	var base map[string]any
	if err := json.Unmarshal(input, &base); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "config.json")
	for _, tc := range []struct {
		name  string
		value any
		want  int64
	}{
		{"omitted", nil, 1 << 20},
		{"small", 16, 16},
		{"maximum", 128 << 20, 128 << 20},
		{"zero", 0, 0},
		{"negative", -1, 0},
		{"too large", (128 << 20) + 1, 0},
		{"wrong type", "1024", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			delete(base, "max_post_bytes")
			if tc.value != nil {
				base["max_post_bytes"] = tc.value
			}
			b, err := json.Marshal(base)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, b, 0600); err != nil {
				t.Fatal(err)
			}
			cfg, err := Load(path)
			if tc.want == 0 {
				if err == nil {
					t.Fatal("invalid POST limit accepted")
				}
			} else if err != nil || cfg.MaxPOSTBytes != tc.want {
				t.Fatalf("max_post_bytes=%d, error=%v", cfg.MaxPOSTBytes, err)
			}
		})
	}
}

func TestProfileReadySelectorsConfig(t *testing.T) {
	input, err := os.ReadFile("../../config.example.json")
	if err != nil {
		t.Fatal(err)
	}
	var cfg Config
	if err := json.Unmarshal(input, &cfg); err != nil {
		t.Fatal(err)
	}
	maximum := make([]string, 64)
	for i := range maximum {
		maximum[i] = "#ready"
	}
	path := filepath.Join(t.TempDir(), "config.json")
	for _, tc := range []struct {
		name      string
		selectors []string
		ok        bool
	}{
		{"omitted", nil, true},
		{"multiple", []string{"#extension-a-ready", "#extension-b-ready"}, true},
		{"maximum count", maximum, true},
		{"too many", append(append([]string{}, maximum...), "#extra"), false},
		{"maximum length", []string{strings.Repeat("a", 1024)}, true},
		{"too long", []string{strings.Repeat("a", 1025)}, false},
		{"empty entry", []string{"#ready", ""}, false},
		{"whitespace entry", []string{" \t\n"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			profile := cfg.Profiles["default"]
			profile.ReadySelectors = tc.selectors
			cfg.Profiles["default"] = profile
			data, err := json.Marshal(cfg)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, data, 0600); err != nil {
				t.Fatal(err)
			}
			loaded, err := Load(path)
			if (err == nil) != tc.ok {
				t.Fatalf("unexpected validation result: %v", err)
			}
			if err == nil && !reflect.DeepEqual(loaded.Profiles["default"], profile) {
				t.Fatal("profile readiness settings not preserved")
			}
		})
	}
	t.Run("removed singular field", func(t *testing.T) {
		var document map[string]any
		if err := json.Unmarshal(input, &document); err != nil {
			t.Fatal(err)
		}
		profiles := document["profiles"].(map[string]any)
		profiles["default"].(map[string]any)["ready_selector"] = "#obsolete-ready"
		data, err := json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := Load(path); err == nil || !strings.Contains(err.Error(), `unknown field "ready_selector"`) {
			t.Fatalf("removed profile field was not rejected: %v", err)
		}
	})
}
