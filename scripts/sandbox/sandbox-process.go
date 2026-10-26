//go:build ignore

package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ChromeProcessEvidence is an observation, not an assertion of complete isolation.
type ChromeProcessEvidence struct {
	UserDataDir     string                     `json:"user_data_dir"`
	StartedAt       time.Time                  `json:"started_at"`
	DurationMS      int64                      `json:"duration_ms"`
	Processes       []ChromeProcessObservation `json:"processes"`
	DiscoveryErrors []ChromeProcReadError      `json:"discovery_errors,omitempty"`
	Validation      ChromeFilterValidation     `json:"filter_validation"`
}

type ChromeProcReadError struct {
	PID   int    `json:"pid"`
	Path  string `json:"path"`
	Error string `json:"error"`
}

type ChromeProcessObservation struct {
	PID                 int                   `json:"pid"`
	ParentPID           int                   `json:"parent_pid"`
	Role                string                `json:"role"`
	Selection           string                `json:"selection"`
	Cmdline             []string              `json:"cmdline,omitempty"`
	StartTicks          string                `json:"start_ticks,omitempty"`
	StableIdentity      bool                  `json:"stable_identity"`
	Status              map[string]string     `json:"status"`
	MissingStatusFields []string              `json:"missing_status_fields,omitempty"`
	Namespaces          map[string]string     `json:"namespaces"`
	Errors              []ChromeProcReadError `json:"errors,omitempty"`
}

type ChromeFilterValidation struct {
	Result          string   `json:"result"`
	BaselineFilters int      `json:"baseline_filters"`
	BaselinePIDs    []int    `json:"baseline_pids,omitempty"`
	RendererPIDs    []int    `json:"renderer_pids,omitempty"`
	Limitations     []string `json:"limitations,omitempty"`
}

type chromeProcCandidate struct {
	pid, parent int
	start       string
	args        []string
	direct      bool
	errors      []ChromeProcReadError
}

// CollectChromeProcessEvidence samples visible processes within at most a three
// second cooperative deadline. It never signals, ptraces, or changes a process.
// Individual procfs reads are not interruptible; context is checked between reads.
func CollectChromeProcessEvidence(ctx context.Context, userDataDir string) (report ChromeProcessEvidence, resultErr error) {
	started := time.Now()
	report.UserDataDir, report.StartedAt = userDataDir, started.UTC()
	defer func() {
		report.DurationMS = time.Since(started).Milliseconds()
		report.Validation = validateChromeFilterEvidence(report.Processes)
	}()
	if !filepath.IsAbs(userDataDir) || filepath.Clean(userDataDir) == "/" {
		return report, errors.New("user-data directory must be an absolute non-root path")
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	own := os.Getpid()
	candidates := make(map[int]chromeProcCandidate)
	selected := map[int]string{own: "own-process-baseline"}
	proc, err := os.Open("/proc")
	if err != nil {
		return report, fmt.Errorf("open /proc: %w", err)
	}
	defer proc.Close()
	count := 0
	for {
		if err := ctx.Err(); err != nil {
			resultErr = err
			break
		}
		entries, err := proc.ReadDir(128)
		for _, entry := range entries {
			if err := ctx.Err(); err != nil {
				resultErr = err
				break
			}
			pid, parseErr := strconv.Atoi(entry.Name())
			if parseErr != nil || pid < 1 {
				continue
			}
			count++
			if count > 16384 {
				resultErr = errors.New("proc enumeration exceeded 16384 processes")
				break
			}
			c := chromeProcCandidate{pid: pid}
			var readErr error
			c.parent, c.start, readErr = readChromeProcIdentity(ctx, pid)
			if readErr != nil {
				c.errors = append(c.errors, chromeReadError(pid, "stat", readErr))
			}
			raw, readErr := readChromeProcFile(ctx, "/proc/"+entry.Name()+"/cmdline")
			if readErr == nil {
				for _, arg := range bytes.Split(bytes.TrimRight(raw, "\x00"), []byte{0}) {
					if len(arg) > 0 {
						c.args = append(c.args, string(arg))
					}
				}
				c.direct = bytes.Contains(raw, []byte(userDataDir))
			} else {
				c.errors = append(c.errors, chromeReadError(pid, "cmdline", readErr))
			}
			candidates[pid] = c
			report.DiscoveryErrors = append(report.DiscoveryErrors, c.errors...)
			if c.direct && pid != own {
				selected[pid] = "cmdline-contains-user-data-directory"
			}
		}
		if resultErr != nil {
			break
		}
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			resultErr = fmt.Errorf("enumerate /proc: %w", err)
			break
		}
	}
	// Children can omit the user-data argument. Do not seed closure from the helper
	// itself, which would include unrelated diagnostics running alongside Chrome.
	for changed := true; changed; {
		changed = false
		for pid, c := range candidates {
			if err := ctx.Err(); err != nil {
				resultErr = err
				break
			}
			if _, ok := selected[pid]; ok {
				continue
			}
			if reason, ok := selected[c.parent]; ok && reason != "own-process-baseline" {
				selected[pid] = "descendant-of-matched-process"
				changed = true
			}
		}
	}
	if _, ok := candidates[own]; !ok {
		candidates[own] = chromeProcCandidate{pid: own}
	}
	pids := make([]int, 0, len(selected))
	for pid := range selected {
		pids = append(pids, pid)
	}
	sort.Ints(pids)
	for _, pid := range pids {
		c := candidates[pid]
		o := ChromeProcessObservation{PID: pid, ParentPID: c.parent, StartTicks: c.start,
			Selection: selected[pid], Cmdline: c.args, Role: chromeObservedRole(c.args),
			Status: make(map[string]string), Namespaces: make(map[string]string), Errors: c.errors}
		if pid == own {
			o.Role = "own-baseline"
		}
		status, err := readChromeProcFile(ctx, fmt.Sprintf("/proc/%d/status", pid))
		if err != nil {
			o.Errors = append(o.Errors, chromeReadError(pid, "status", err))
		}
		wanted := []string{"Uid", "Gid", "CapEff", "NoNewPrivs", "Seccomp", "Seccomp_filters", "NSpid"}
		fields := make(map[string]string)
		for _, line := range strings.Split(string(status), "\n") {
			key, value, ok := strings.Cut(line, ":")
			if ok {
				fields[key] = strings.TrimSpace(value)
			}
		}
		for _, field := range wanted {
			if value, ok := fields[field]; ok {
				o.Status[field] = value
			} else {
				o.MissingStatusFields = append(o.MissingStatusFields, field)
			}
		}
		for _, ns := range []string{"user", "pid", "pid_for_children", "net", "mnt", "ipc", "uts", "cgroup", "time", "time_for_children"} {
			path := fmt.Sprintf("/proc/%d/ns/%s", pid, ns)
			if err := ctx.Err(); err != nil {
				o.Errors = append(o.Errors, chromeReadError(pid, "ns/"+ns, err))
				continue
			}
			value, err := os.Readlink(path)
			if err != nil {
				o.Errors = append(o.Errors, chromeReadError(pid, "ns/"+ns, err))
			} else {
				o.Namespaces[ns] = value
			}
		}
		_, after, err := readChromeProcIdentity(ctx, pid)
		if err != nil {
			o.Errors = append(o.Errors, chromeReadError(pid, "stat-after", err))
		} else if c.start != "" && c.start == after {
			o.StableIdentity = true
		} else {
			o.Errors = append(o.Errors, chromeReadError(pid, "stat-after", errors.New("identity changed or initial start time unavailable")))
		}
		report.Processes = append(report.Processes, o)
	}
	if err := ctx.Err(); err != nil && resultErr == nil {
		resultErr = err
	}
	return report, resultErr
}

func chromeObservedRole(args []string) string {
	if len(args) == 0 {
		return "unknown"
	}
	// Chrome can rewrite cmdline into a single NUL-delimited process title.
	// Tokenize that observation locally; retain the original args in the report.
	roleArgs := args
	if len(args) == 1 {
		roleArgs = strings.Fields(args[0])
	}
	if len(roleArgs) == 0 {
		return "unknown"
	}
	base := filepath.Base(roleArgs[0])
	if !strings.Contains(base, "chrome") {
		return "other"
	}
	for _, arg := range roleArgs[1:] {
		if strings.HasPrefix(arg, "--type=") {
			return strings.TrimPrefix(arg, "--type=")
		}
	}
	if base == "chrome" {
		return "browser"
	}
	return base
}

func chromeReadError(pid int, path string, err error) ChromeProcReadError {
	return ChromeProcReadError{PID: pid, Path: fmt.Sprintf("/proc/%d/%s", pid, path), Error: err.Error()}
}

func readChromeProcFile(ctx context.Context, path string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, 256*1024+1))
	if len(b) > 256*1024 {
		return nil, errors.New("proc file exceeds 256 KiB observation limit")
	}
	return b, err
}

func readChromeProcIdentity(ctx context.Context, pid int) (int, string, error) {
	b, err := readChromeProcFile(ctx, fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0, "", err
	}
	// comm (field 2) can contain spaces and closing parentheses. Remaining fields
	// start with state (field 3); starttime is field 22, hence offset 19.
	end := bytes.LastIndexByte(b, ')')
	if end < 0 {
		return 0, "", errors.New("stat has no comm terminator")
	}
	fields := strings.Fields(string(b[end+1:]))
	if len(fields) <= 19 {
		return 0, "", errors.New("stat lacks starttime")
	}
	parent, err := strconv.Atoi(fields[1])
	if err != nil {
		return 0, "", fmt.Errorf("stat parent: %w", err)
	}
	if _, err := strconv.ParseUint(fields[19], 10, 64); err != nil {
		return 0, "", fmt.Errorf("stat starttime: %w", err)
	}
	return parent, fields[19], nil
}

func validateChromeFilterEvidence(processes []ChromeProcessObservation) ChromeFilterValidation {
	v := ChromeFilterValidation{Result: "unavailable", BaselineFilters: -1}
	ownOK, browserOK, rendererSeen, rendererCountSeen := false, false, false, false
	for _, p := range processes {
		if !p.StableIdentity || (p.Role != "own-baseline" && p.Role != "browser") {
			continue
		}
		n, err := strconv.Atoi(p.Status["Seccomp_filters"])
		if err != nil || n < 0 || p.Status["Seccomp"] != "2" {
			continue
		}
		if p.Role == "own-baseline" {
			ownOK = true
		} else {
			browserOK = true
		}
		v.BaselinePIDs = append(v.BaselinePIDs, p.PID)
		if n > v.BaselineFilters {
			v.BaselineFilters = n
		}
	}
	for _, p := range processes {
		if p.Role != "renderer" {
			continue
		}
		rendererSeen = true
		if !p.StableIdentity {
			continue
		}
		n, err := strconv.Atoi(p.Status["Seccomp_filters"])
		if err != nil || n < 0 || p.Status["Seccomp"] != "2" {
			continue
		}
		rendererCountSeen = true
		if ownOK && browserOK && n > v.BaselineFilters {
			v.RendererPIDs = append(v.RendererPIDs, p.PID)
		}
	}
	if !ownOK {
		v.Limitations = append(v.Limitations, "own baseline filter mode/count unavailable or unstable")
	}
	if !browserOK {
		v.Limitations = append(v.Limitations, "browser baseline filter mode/count unavailable or unstable")
	}
	if !rendererSeen {
		v.Limitations = append(v.Limitations, "no live renderer selected; keep a rendered target open and retry")
	} else if !rendererCountSeen {
		v.Limitations = append(v.Limitations, "renderer filter mode/count unavailable or unstable")
	}
	if !ownOK || !browserOK || !rendererCountSeen {
		v.Limitations = append(v.Limitations, "missing Seccomp_filters may indicate a kernel before Linux 5.9; read errors and exit races are separate limitations")
		return v
	}
	v.Result = "not-demonstrated"
	if len(v.RendererPIDs) > 0 {
		v.Result = "additional-renderer-filter-observed"
	}
	return v
}
