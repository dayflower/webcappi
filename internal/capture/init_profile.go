package capture

import (
	"archive/tar"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"

	"github.com/chromedp/chromedp"
)

// TemporaryProfile owns a process-local template. Captures only use clones.
type TemporaryProfile struct {
	Profile Profile
	root    string
}

func (p *TemporaryProfile) Close() error { return os.RemoveAll(p.root) }

func (p *TemporaryProfile) CheckFiles(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	bundle := filepath.Dir(p.Profile.UserDataDir)
	manifest, err := os.ReadFile(filepath.Join(bundle, "bundle.json"))
	if err != nil {
		return err
	}
	var declared Profile
	if err := json.Unmarshal(manifest, &declared); err != nil {
		return err
	}
	if !reflect.DeepEqual(declared, p.Profile) {
		return fmt.Errorf("temporary profile descriptor changed")
	}
	for _, path := range []string{"Local State", "Default/Preferences"} {
		f, err := os.Open(filepath.Join(p.Profile.UserDataDir, path))
		if err != nil {
			return err
		}
		if err := f.Close(); err != nil {
			return err
		}
	}
	return ctx.Err()
}

// NewProfile initializes a complete, extension-free template without a display.
// The caller closes it after all captures have finished.
func (r Renderer) NewProfile(ctx context.Context) (profile *TemporaryProfile, err error) {
	work, err := os.MkdirTemp(r.WorkDir, "profile-")
	if err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(work)
		}
	}()
	data := filepath.Join(work, "browser", "user-data")
	s, err := r.Browser.Open(ctx, data, "Default", false)
	if err != nil {
		return nil, err
	}
	// Close normally before copying so Preferences and profile databases flush.
	navigateErr := chromedp.Do(s.Context, chromedp.Navigate("about:blank"))
	closeErr := s.Close()
	if navigateErr != nil {
		return nil, navigateErr
	}
	if closeErr != nil {
		return nil, closeErr
	}
	for _, name := range []string{"Local State", "Default/Preferences"} {
		if _, err := os.Stat(filepath.Join(data, name)); err != nil {
			return nil, fmt.Errorf("initialized profile: %w", err)
		}
	}
	root := filepath.Join(work, "bundle")
	bundle := filepath.Join(root, "default")
	if err := CopyProfile(ctx, data, filepath.Join(bundle, "user-data"), r.MaxProfileBytes); err != nil {
		return nil, err
	}
	if err := os.Mkdir(filepath.Join(bundle, "extensions"), 0700); err != nil {
		return nil, err
	}
	profile = &TemporaryProfile{
		Profile: Profile{UserDataDir: filepath.Join(bundle, "user-data"), ProfileDirectory: "Default", Extensions: []string{}},
		root:    work,
	}
	if err := writeProfileManifest(bundle, profile.Profile); err != nil {
		return nil, err
	}
	if err := profile.CheckFiles(ctx); err != nil {
		return nil, err
	}
	// Only the snapshot is retained for the process lifetime.
	if err := os.RemoveAll(filepath.Join(work, "browser")); err != nil {
		return nil, err
	}
	return profile, nil
}

// InitProfile exports an initialized template using the standard runtime paths.
func (r Renderer) InitProfile(ctx context.Context, dst io.Writer) (err error) {
	profile, err := r.NewProfile(ctx)
	if err != nil {
		return err
	}
	defer func() {
		if e := profile.Close(); err == nil {
			err = e
		}
	}()
	exported := profile.Profile
	exported.UserDataDir = "/bundles/default/user-data"
	if err := writeProfileManifest(filepath.Dir(profile.Profile.UserDataDir), exported); err != nil {
		return err
	}
	return archiveBundle(ctx, dst, filepath.Join(profile.root, "bundle"))
}

func writeProfileManifest(bundle string, profile Profile) error {
	manifest, err := json.MarshalIndent(profile, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(bundle, "bundle.json"), append(manifest, '\n'), 0600)
}

func archiveBundle(ctx context.Context, dst io.Writer, root string) (err error) {
	tw := tar.NewWriter(dst)
	defer func() {
		if e := tw.Close(); err == nil {
			err = e
		}
	}()
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if path == root {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return fmt.Errorf("bundle contains non-regular file: %s", path)
		}
		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		header.Name = filepath.ToSlash(rel)
		header.Uid, header.Gid, header.Uname, header.Gname = 10001, 10001, "", ""
		if err := tw.WriteHeader(header); err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		_, copyErr := io.Copy(tw, contextReader{ctx, f})
		closeErr := f.Close()
		if copyErr != nil {
			return copyErr
		}
		return closeErr
	})
}

type contextReader struct {
	ctx context.Context
	src io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.src.Read(p)
}
