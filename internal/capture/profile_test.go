package capture

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestCopyProfileIsolationAndLimits(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "template")
	dst := filepath.Join(root, "copy")
	os.MkdirAll(filepath.Join(src, "Default"), 0700)
	os.WriteFile(filepath.Join(src, "Local State"), []byte("state"), 0400)
	os.WriteFile(filepath.Join(src, "Default", "Preferences"), []byte("prefs"), 0400)
	os.Symlink("/private/machine-lock", filepath.Join(src, "SingletonLock"))
	if err := CopyProfile(t.Context(), src, dst, 100); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(filepath.Join(dst, "SingletonLock")); !os.IsNotExist(err) {
		t.Fatal("copied lock")
	}
	if err := os.WriteFile(filepath.Join(dst, "Default", "Preferences"), []byte("changed"), 0600); err != nil {
		t.Fatal(err)
	}
	original, _ := os.ReadFile(filepath.Join(src, "Default", "Preferences"))
	if string(original) != "prefs" {
		t.Fatal("template changed")
	}
	if err := CopyProfile(t.Context(), src, filepath.Join(root, "small"), 1); err == nil {
		t.Fatal("byte limit missing")
	}
	os.Symlink("/etc/passwd", filepath.Join(src, "secret"))
	if err := CopyProfile(t.Context(), src, filepath.Join(root, "links"), 100); err == nil {
		t.Fatal("followed symlink")
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := CopyProfile(ctx, src, filepath.Join(root, "canceled"), 100); err == nil {
		t.Fatal("ignored cancellation")
	}
}
