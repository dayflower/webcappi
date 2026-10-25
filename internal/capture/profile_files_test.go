package capture

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func profileFilesFixture(t *testing.T, directory string) Profile {
	t.Helper()
	bundle := t.TempDir()
	p := Profile{UserDataDir: filepath.Join(bundle, "user-data"), ProfileDirectory: directory, Extensions: []string{}}
	if err := os.MkdirAll(filepath.Join(p.UserDataDir, directory), 0700); err != nil {
		t.Fatal(err)
	}
	if err := writeProfileManifest(bundle, p); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"Local State", filepath.Join(directory, "Preferences")} {
		if err := os.WriteFile(filepath.Join(p.UserDataDir, path), []byte("{}"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return p
}

func TestCheckProfileFiles(t *testing.T) {
	for _, directory := range []string{"Default", "Profile 2"} {
		t.Run(directory, func(t *testing.T) {
			p := profileFilesFixture(t, directory)
			if err := CheckProfileFiles(t.Context(), p); err != nil {
				t.Fatal(err)
			}
			for _, path := range []string{"bundle.json", "user-data/Local State", filepath.Join("user-data", directory, "Preferences")} {
				t.Run("missing "+path, func(t *testing.T) {
					p := profileFilesFixture(t, directory)
					if err := os.Remove(filepath.Join(filepath.Dir(p.UserDataDir), path)); err != nil {
						t.Fatal(err)
					}
					err := CheckProfileFiles(t.Context(), p)
					if !errors.Is(err, os.ErrNotExist) || !strings.Contains(err.Error(), path) {
						t.Fatalf("missing file was not identified: %v", err)
					}
				})
			}
		})
	}
	for _, tc := range []struct {
		name   string
		change func(*Profile)
	}{
		{"user data directory", func(p *Profile) { p.UserDataDir += "-other" }},
		{"profile directory", func(p *Profile) { p.ProfileDirectory = "Profile 3" }},
		{"extensions", func(p *Profile) { p.Extensions = []string{"/extensions/example"} }},
		{"readiness selectors", func(p *Profile) { p.ReadySelectors = []string{"#ready"} }},
	} {
		t.Run("descriptor mismatch "+tc.name, func(t *testing.T) {
			p := profileFilesFixture(t, "Profile 2")
			declared := p
			tc.change(&declared)
			if err := writeProfileManifest(filepath.Dir(p.UserDataDir), declared); err != nil {
				t.Fatal(err)
			}
			if err := CheckProfileFiles(t.Context(), p); err == nil || !strings.Contains(err.Error(), "bundle.json does not match") {
				t.Fatalf("mismatched descriptor allowed: %v", err)
			}
		})
	}
	t.Run("invalid manifest", func(t *testing.T) {
		p := profileFilesFixture(t, "Default")
		if err := os.WriteFile(filepath.Join(filepath.Dir(p.UserDataDir), "bundle.json"), []byte("{"), 0600); err != nil {
			t.Fatal(err)
		}
		if err := CheckProfileFiles(t.Context(), p); err == nil || !strings.Contains(err.Error(), "bundle manifest") {
			t.Fatalf("invalid manifest allowed: %v", err)
		}
	})
	t.Run("cancellation", func(t *testing.T) {
		p := profileFilesFixture(t, "Default")
		ctx, cancel := context.WithCancel(t.Context())
		cancel()
		if err := CheckProfileFiles(ctx, p); !errors.Is(err, context.Canceled) {
			t.Fatalf("ignored cancellation: %v", err)
		}
		ctx, cancel = context.WithDeadline(t.Context(), time.Now().Add(-time.Second))
		defer cancel()
		if err := CheckProfileFiles(ctx, p); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("ignored deadline: %v", err)
		}
	})
}

func TestTemporaryProfileCheckFiles(t *testing.T) {
	p := &TemporaryProfile{Profile: profileFilesFixture(t, "Profile 2")}
	if err := p.CheckFiles(t.Context()); err != nil {
		t.Fatal(err)
	}
	p.Profile.ReadySelectors = []string{"#changed"}
	if err := p.CheckFiles(t.Context()); err == nil || !strings.Contains(err.Error(), "temporary profile:") || !strings.Contains(err.Error(), "bundle.json does not match") {
		t.Fatalf("temporary profile failure lost its origin: %v", err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := p.CheckFiles(ctx); !errors.Is(err, context.Canceled) || !strings.Contains(err.Error(), "temporary profile:") {
		t.Fatalf("temporary profile cancellation lost its origin: %v", err)
	}
}
