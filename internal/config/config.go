package config

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
)

const DefaultProfileName = "default"

type Config struct {
	Listen                string                     `json:"listen"`
	Auth                  *Auth                      `json:"auth,omitempty"`
	BrowserPath           string                     `json:"browser_path"`
	NoSandbox             bool                       `json:"no_sandbox"`
	WorkDir               string                     `json:"work_dir"`
	Concurrency           int                        `json:"concurrency"`
	QueueSize             int                        `json:"queue_size"`
	MaxRequests           int                        `json:"max_requests"`
	CaptureTimeoutSeconds int                        `json:"capture_timeout_seconds"`
	CacheTTLSeconds       int                        `json:"cache_ttl_seconds"`
	CacheMaxBytes         int64                      `json:"cache_max_bytes"`
	CacheMaxEntries       int                        `json:"cache_max_entries"`
	MaxWidth              int                        `json:"max_width"`
	MaxHeight             int                        `json:"max_height"`
	MaxPixels             int64                      `json:"max_pixels"`
	ResizeAlgorithm       capture.ResizeAlgorithm    `json:"resize_algorithm"`
	MaxProfileBytes       int64                      `json:"max_profile_bytes"`
	MaxPOSTBytes          int64                      `json:"max_post_bytes"`
	MaxPNGBytes           int64                      `json:"max_png_bytes"`
	Profiles              map[string]capture.Profile `json:"profiles"`
}

type Auth struct {
	BearerTokenFile string `json:"bearer_token_file"`
}

func Defaults() Config {
	return Config{Listen: ":8080", BrowserPath: "/opt/chrome/chrome", WorkDir: "/work", Concurrency: 1, QueueSize: 8, MaxRequests: 64, CaptureTimeoutSeconds: 30, CacheTTLSeconds: 60, CacheMaxBytes: 64 << 20, CacheMaxEntries: 128, MaxWidth: 4096, MaxHeight: 4096, MaxPixels: 8_388_608, ResizeAlgorithm: capture.ResizeBilinear, MaxProfileBytes: 512 << 20, MaxPOSTBytes: 1 << 20, MaxPNGBytes: 40 << 20}
}

func Load(path string) (Config, error) {
	c := Defaults()
	f, err := os.Open(path)
	if err != nil {
		return c, err
	}
	defer f.Close()
	d := json.NewDecoder(io.LimitReader(f, 1<<20))
	d.DisallowUnknownFields()
	decoded := &c
	if err = d.Decode(&decoded); err != nil {
		return c, err
	}
	if decoded == nil {
		return c, errors.New("config must contain one JSON object")
	}
	var extra any
	if err = d.Decode(&extra); err != io.EOF {
		return c, errors.New("config must contain one JSON object")
	}
	return c, c.Validate()
}

func (c Config) Timeout() time.Duration { return time.Duration(c.CaptureTimeoutSeconds) * time.Second }
func (c Config) TTL() time.Duration     { return time.Duration(c.CacheTTLSeconds) * time.Second }

func (c Config) Validate() error {
	if err := c.ResizeAlgorithm.Validate(); err != nil {
		return err
	}
	if c.Listen == "" || !filepath.IsAbs(c.BrowserPath) || !filepath.IsAbs(c.WorkDir) {
		return errors.New("listen and absolute browser_path/work_dir are required")
	}
	if c.Auth != nil && !filepath.IsAbs(c.Auth.BearerTokenFile) {
		return errors.New("auth.bearer_token_file must be an absolute path")
	}
	if c.Concurrency < 1 || c.Concurrency > 32 || c.QueueSize < 0 || c.QueueSize > 1024 || c.MaxRequests < 1 || c.MaxRequests > 4096 {
		return errors.New("invalid concurrency, queue_size or max_requests")
	}
	if c.CaptureTimeoutSeconds < 1 || c.CaptureTimeoutSeconds > 300 || c.CacheTTLSeconds < 0 || c.CacheTTLSeconds > 86400 {
		return errors.New("invalid capture timeout or cache TTL")
	}
	if c.CacheMaxBytes < 0 || c.CacheMaxBytes > 4<<30 || c.CacheMaxEntries < 0 || c.CacheMaxEntries > 10000 || c.MaxPNGBytes < 1024 || c.MaxPNGBytes > 128<<20 || c.MaxProfileBytes < 1 || c.MaxProfileBytes > 8<<30 {
		return errors.New("invalid byte or entry limit")
	}
	if c.MaxPOSTBytes < 1 || c.MaxPOSTBytes > 128<<20 {
		return errors.New("max_post_bytes must be between 1 and 134217728")
	}
	if c.MaxWidth < capture.DefaultViewportWidth || c.MaxHeight < capture.DefaultViewportHeight || c.MaxWidth > 8192 || c.MaxHeight > 8192 || c.MaxPixels < capture.DefaultViewportWidth*capture.DefaultViewportHeight || c.MaxPixels > 32_000_000 {
		return errors.New("invalid viewport limits")
	}
	count := len(c.Profiles)
	if _, ok := c.Profiles[DefaultProfileName]; !ok {
		count++
	}
	if count > 128 {
		return errors.New("configure at most 128 profiles including the automatic default")
	}
	for name, p := range c.Profiles {
		if !regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`).MatchString(name) {
			return fmt.Errorf("invalid profile name %q", name)
		}
		if !capture.ValidProfileDirectory(p.ProfileDirectory) {
			return fmt.Errorf("profile %s: valid profile_directory required", name)
		}
		if !filepath.IsAbs(p.UserDataDir) || within(p.UserDataDir, c.WorkDir) || within(c.WorkDir, p.UserDataDir) {
			return fmt.Errorf("profile %s: absolute user_data_dir must be separate from work_dir", name)
		}
		if len(p.ReadySelectors) > 64 {
			return fmt.Errorf("profile %s: ready_selectors must contain at most 64 selectors", name)
		}
		for _, selector := range p.ReadySelectors {
			if strings.TrimSpace(selector) == "" || len(selector) > 1024 {
				return fmt.Errorf("profile %s: ready_selectors entries must be non-empty and <=1024 bytes", name)
			}
		}
		for _, ext := range p.Extensions {
			if !filepath.IsAbs(ext) || strings.Contains(ext, ",") || within(ext, p.UserDataDir) || within(p.UserDataDir, ext) || within(ext, c.WorkDir) || within(c.WorkDir, ext) {
				return fmt.Errorf("profile %s: extension path must be absolute and separate from profile/work", name)
			}
		}
	}
	return nil
}

func within(path, root string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// CheckFiles reads immutable bundles and checks writable scratch space. It never
// launches Chromium against a template. It can also serve readiness probes.
func (c Config) CheckFiles(ctx context.Context) error {
	work, err := filepath.EvalSymlinks(c.WorkDir)
	if err != nil {
		return err
	}
	for name, p := range c.Profiles {
		template, err := filepath.EvalSymlinks(p.UserDataDir)
		if err != nil {
			return err
		}
		if within(template, work) || within(work, template) {
			return fmt.Errorf("profile %s resolves inside work directory", name)
		}
		manifest, err := os.ReadFile(filepath.Join(filepath.Dir(p.UserDataDir), "bundle.json"))
		if err != nil {
			return fmt.Errorf("profile %s bundle manifest: %w", name, err)
		}
		var declared capture.Profile
		if err = json.Unmarshal(manifest, &declared); err != nil {
			return err
		}
		if !reflect.DeepEqual(declared, p) {
			return fmt.Errorf("profile %s bundle.json does not match the configured profile descriptor", name)
		}
		for _, path := range []string{filepath.Join(p.UserDataDir, "Local State"), filepath.Join(p.UserDataDir, p.ProfileDirectory, "Preferences")} {
			f, err := os.Open(path)
			if err != nil {
				return fmt.Errorf("profile %s: %w", name, err)
			}
			f.Close()
		}
		if err := checkExtensions(p); err != nil {
			return fmt.Errorf("profile %s: %w", name, err)
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
	f, err := os.CreateTemp(c.WorkDir, ".ready-")
	if err != nil {
		return err
	}
	path := f.Name()
	err = f.Close()
	if e := os.Remove(path); err == nil {
		err = e
	}
	return err
}

func checkExtensions(p capture.Profile) error {
	// Chromium can place extension preferences in either file. No prefs are edited.
	type setting struct {
		Path           string `json:"path"`
		Location       int    `json:"location"`
		State          *int   `json:"state"`
		DisableReasons []int  `json:"disable_reasons"`
		InstalledByCDP bool   `json:"installed_by_cdp"`
	}
	settings := map[string]setting{}
	for _, file := range []string{"Preferences", "Secure Preferences"} {
		b, err := os.ReadFile(filepath.Join(p.UserDataDir, p.ProfileDirectory, file))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		var prefs struct {
			Extensions struct {
				Settings map[string]setting `json:"settings"`
			} `json:"extensions"`
		}
		if err = json.Unmarshal(b, &prefs); err != nil {
			return err
		}
		for id, s := range prefs.Extensions.Settings {
			settings[id] = s
		}
	}
	for _, path := range p.Extensions {
		b, err := os.ReadFile(filepath.Join(path, "manifest.json"))
		if err != nil {
			return err
		}
		var m struct {
			Key string `json:"key"`
		}
		if err = json.Unmarshal(b, &m); err != nil {
			return err
		}
		key, err := base64.StdEncoding.DecodeString(m.Key)
		if err != nil || len(key) == 0 {
			return fmt.Errorf("extension %s requires a stable manifest key", path)
		}
		h := sha256.Sum256(key)
		id := make([]byte, 32)
		for i, v := range h[:16] {
			id[i*2] = 'a' + v>>4
			id[i*2+1] = 'a' + v&15
		}
		s, ok := settings[string(id)]
		if !ok || s.Location != 4 || (s.State != nil && *s.State != 1) || len(s.DisableReasons) > 0 || s.Path != path || s.InstalledByCDP {
			return fmt.Errorf("extension %s (%s) must be enabled and installed through Load unpacked at the same path", path, id)
		}
	}
	return nil
}
