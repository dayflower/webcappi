package config

import (
	"encoding/json"
	"os"
	"path/filepath"
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
	p := capture.Profile{Revision: "v1", BrowserVersion: capture.BrowserVersion, UserDataDir: data, ProfileDirectory: "Default", Extensions: []string{ext}}
	descriptor, _ := json.Marshal(p)
	os.WriteFile(filepath.Join(bundle, "bundle.json"), descriptor, 0600)
	os.WriteFile(filepath.Join(data, "Local State"), []byte("{}"), 0600)
	prefs := map[string]any{"extensions": map[string]any{"settings": map[string]any{"gkofocaippcmakpnppmdankgpgikcgnb": map[string]any{"path": ext, "location": 4, "disable_reasons": []int{}}}}}
	encoded, _ := json.Marshal(prefs)
	os.WriteFile(filepath.Join(data, "Default", "Preferences"), encoded, 0600)
	c := Defaults()
	c.WorkDir = work
	c.Profiles = map[string]capture.Profile{"reader": p}
	if err = c.Validate(); err != nil {
		t.Fatal(err)
	}
	if err = c.CheckFiles(t.Context()); err != nil {
		t.Fatal(err)
	}
	p.Revision = "v2"
	c.Profiles["reader"] = p
	if err = c.CheckFiles(t.Context()); err == nil {
		t.Fatal("mismatched bundle allowed")
	}
	p.Revision = "v1"
	p.UserDataDir = work
	c.Profiles["reader"] = p
	if err = c.Validate(); err == nil {
		t.Fatal("template inside work allowed")
	}
	p.UserDataDir = data
	p.ProfileDirectory = "../Default"
	c.Profiles["reader"] = p
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
