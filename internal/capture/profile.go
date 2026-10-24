package capture

import (
	"context"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// CopyProfile copies an entire User Data Directory, never just Default.
// Runtime lock files refer to the original machine and must not be copied.
func CopyProfile(ctx context.Context, src, dst string, maxBytes int64) error {
	var total int64
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if rel == "SingletonLock" || rel == "SingletonSocket" || rel == "SingletonCookie" || rel == "DevToolsActivePort" {
			return nil
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0700)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("profile contains non-regular file: %s", rel)
		}
		total += info.Size()
		if total > maxBytes {
			return fmt.Errorf("profile exceeds %d bytes", maxBytes)
		}
		in, err := os.Open(path)
		if err != nil {
			return err
		}
		defer in.Close()
		out, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		// Check cancellation between chunks even for a large profile database.
		buf := make([]byte, 128*1024)
		for {
			if err = ctx.Err(); err != nil {
				break
			}
			var n int
			n, err = in.Read(buf)
			if n > 0 {
				if _, werr := out.Write(buf[:n]); werr != nil {
					err = werr
					break
				}
			}
			if err == io.EOF {
				err = nil
				break
			}
			if err != nil {
				break
			}
		}
		cerr := out.Close()
		if err != nil {
			return err
		}
		return cerr
	})
}

func ValidProfileDirectory(s string) bool {
	return s != "" && s != "." && s != ".." && !strings.ContainsAny(s, `/\\`)
}
