package capture

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestNewProfileCleansUpFailedStartup(t *testing.T) {
	r := Renderer{Browser: Browser{Path: filepath.Join(t.TempDir(), "missing-browser")}, WorkDir: t.TempDir(), MaxProfileBytes: 512 << 20}
	if _, err := r.NewProfile(t.Context()); err == nil {
		t.Fatal("missing browser allowed")
	}
	entries, err := os.ReadDir(r.WorkDir)
	if err != nil || len(entries) != 0 {
		t.Fatalf("failed initialization leaked files: %v, %v", entries, err)
	}
}

func TestTemporaryProfileLifecycle(t *testing.T) {
	b := fixtureBrowser(t)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	r := Renderer{Browser: b, WorkDir: t.TempDir(), MaxProfileBytes: 512 << 20}
	p, err := r.NewProfile(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	before := treeDigest(t, p.root)
	if err := r.Check(ctx, p.Profile); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Capture(ctx, Request{HTML: "<p>Ephemeral default</p>", Width: 800, Height: 480, Profile: p.Profile}); err != nil {
		t.Fatal(err)
	}
	if before != treeDigest(t, p.root) {
		t.Fatal("temporary template changed during capture")
	}
	if err := p.CheckFiles(ctx); err != nil {
		t.Fatal(err)
	}
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	if err := p.CheckFiles(ctx); err == nil {
		t.Fatal("deleted template considered ready")
	}
	entries, err := os.ReadDir(r.WorkDir)
	if err != nil || len(entries) != 0 {
		t.Fatalf("temporary template leaked files: %v, %v", entries, err)
	}
}

func TestArchiveBundle(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "default"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "default", "bundle.json"), []byte("private data"), 0600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := archiveBundle(t.Context(), &output, root); err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(&output)
	found := false
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if h.Uid != 10001 || h.Gid != 10001 || h.Mode&0077 != 0 || !strings.HasPrefix(h.Name, "default") {
			t.Fatalf("unexpected archive metadata: %+v", h)
		}
		if h.Name == "default/bundle.json" {
			found = true
			data, err := io.ReadAll(tr)
			if err != nil || string(data) != "private data" {
				t.Fatalf("archive contents changed: %q, %v", data, err)
			}
		}
	}
	if !found {
		t.Fatal("archive omitted bundle contents")
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := archiveBundle(ctx, io.Discard, root); !errors.Is(err, context.Canceled) {
		t.Fatalf("archive ignored cancellation: %v", err)
	}
	if err := archiveBundle(t.Context(), failingArchiveWriter{}, root); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("archive ignored output failure: %v", err)
	}
	if err := os.Symlink("/etc/passwd", filepath.Join(root, "default", "outside")); err != nil {
		t.Fatal(err)
	}
	if err := archiveBundle(t.Context(), io.Discard, root); err == nil {
		t.Fatal("archive accepted a symlink")
	}
}

type failingArchiveWriter struct{}

func (failingArchiveWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }
