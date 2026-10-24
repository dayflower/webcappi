package capture

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/chromedp/chromedp"
)

const BrowserVersion = "156.0.8078.47"

type Browser struct {
	Path      string
	NoSandbox bool
}

type Session struct {
	Context context.Context
	close   func() error
}

func (s *Session) Close() error { return s.close() }

func (b Browser) Check(ctx context.Context) error {
	out, err := exec.CommandContext(ctx, b.Path, "--version").Output()
	if err != nil {
		return fmt.Errorf("browser version: %w", err)
	}
	if strings.TrimSpace(string(out)) != "Google Chrome for Testing "+BrowserVersion {
		return fmt.Errorf("expected Chrome for Testing %s, got %q", BrowserVersion, strings.TrimSpace(string(out)))
	}
	return nil
}

func (b Browser) Open(ctx context.Context, dataDir, profileDir string, headful bool) (*Session, error) {
	var command *exec.Cmd
	base := filepath.Dir(dataDir)
	for _, dir := range []string{"home", "config", "cache"} {
		if err := os.MkdirAll(filepath.Join(base, dir), 0700); err != nil {
			return nil, err
		}
	}
	// Chromium puts a Unix domain socket under TMPDIR. A deeply nested configured
	// work directory can exceed Linux's socket path limit, so use a short private
	// directory and delete it only after all browser processes have stopped.
	tmp, err := os.MkdirTemp("/tmp", "wc-")
	if err != nil {
		return nil, err
	}
	opts := append([]chromedp.ExecAllocatorOption{}, chromedp.DefaultExecAllocatorOptions[:]...)
	if os.Getenv("WEBCAPPI_BROWSER_DEBUG") == "1" {
		opts = append(opts, chromedp.CombinedOutput(os.Stderr))
	}
	opts = append(opts,
		chromedp.ExecPath(b.Path), chromedp.UserDataDir(dataDir),
		chromedp.Env("HOME="+filepath.Join(base, "home"), "XDG_CONFIG_HOME="+filepath.Join(base, "config"), "XDG_CACHE_HOME="+filepath.Join(base, "cache"), "TMPDIR="+tmp),
		chromedp.Flag("profile-directory", profileDir),
		chromedp.Flag("headless", "new"),
		chromedp.Flag("disable-extensions", false),
		chromedp.Flag("no-sandbox", b.NoSandbox),
		chromedp.Flag("disable-dev-shm-usage", false),
		chromedp.Flag("password-store", "basic"),
		chromedp.Flag("disable-session-crashed-bubble", true),
		chromedp.Flag("force-device-scale-factor", "1"),
		chromedp.ModifyCmdFunc(func(cmd *exec.Cmd) {
			cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
			cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
			cmd.WaitDelay = 2 * time.Second
			command = cmd
		}),
	)
	if headful {
		opts = append(opts, chromedp.VisibleWindow)
	}
	alloc, stopAlloc := chromedp.NewExecAllocator(ctx, opts...)
	root, stopRoot := chromedp.NewContext(alloc)
	s := &Session{Context: root}
	s.close = func() error {
		var err error
		if ctx.Err() == nil && chromedp.FromContext(root).Browser != nil {
			closing, cancel := context.WithTimeout(context.WithoutCancel(root), 3*time.Second)
			err = chromedp.Cancel(closing)
			cancel()
		}
		if command != nil && command.Process != nil {
			_ = syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
		}
		stopRoot()
		// A caller-supplied user-data-dir is not cleaned up by chromedp.
		killLeftovers(dataDir)
		stopAlloc()
		if e := os.RemoveAll(tmp); err == nil {
			err = e
		}
		return err
	}
	if err := chromedp.Do(root); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

func killLeftovers(dir string) {
	want := []byte("--user-data-dir=" + dir)
	deadline := time.Now().Add(2 * time.Second)
	for quiet := 0; quiet < 3 && time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		entries, _ := os.ReadDir("/proc")
		found := false
		for _, entry := range entries {
			pid, err := strconv.Atoi(entry.Name())
			if err != nil || pid == os.Getpid() {
				continue
			}
			cmdline, err := os.ReadFile("/proc/" + entry.Name() + "/cmdline")
			if err != nil {
				continue
			}
			for _, arg := range bytes.Split(cmdline, []byte{0}) {
				if bytes.Equal(arg, want) {
					_ = syscall.Kill(pid, syscall.SIGKILL)
					found = true
					break
				}
			}
		}
		if found {
			quiet = 0
		} else {
			quiet++
		}
	}
}
