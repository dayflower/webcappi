package capture

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
)

// CheckProfileFiles reads the bundle descriptor and required browser files.
// It does not enforce template placement, check extensions or launch Chromium.
func CheckProfileFiles(ctx context.Context, p Profile) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	manifest, err := os.ReadFile(filepath.Join(filepath.Dir(p.UserDataDir), "bundle.json"))
	if err != nil {
		return fmt.Errorf("bundle manifest: %w", err)
	}
	var declared Profile
	if err := json.Unmarshal(manifest, &declared); err != nil {
		return fmt.Errorf("bundle manifest: %w", err)
	}
	if !reflect.DeepEqual(declared, p) {
		return fmt.Errorf("bundle.json does not match the configured profile descriptor")
	}
	for _, path := range []string{filepath.Join(p.UserDataDir, "Local State"), filepath.Join(p.UserDataDir, p.ProfileDirectory, "Preferences")} {
		if err := ctx.Err(); err != nil {
			return err
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		if err := f.Close(); err != nil {
			return fmt.Errorf("close %s: %w", path, err)
		}
	}
	return ctx.Err()
}
