// Package buildinfo identifies the application independently of its browser.
package buildinfo

import (
	"fmt"
	"runtime/debug"
)

// These values are supplied by the Makefile and Dockerfile through -ldflags.
var (
	Version  = "dev"
	Revision = "unknown"
	Modified = "unknown"
)

// Current also identifies direct go builds when Go has recorded VCS metadata.
func Current() (version, revision, modified string) {
	version, revision, modified = Version, Revision, Modified
	if info, ok := debug.ReadBuildInfo(); ok && revision == "unknown" {
		for _, setting := range info.Settings {
			switch setting.Key {
			case "vcs.revision":
				revision = setting.Value
			case "vcs.modified":
				modified = setting.Value
			}
		}
	}
	return
}

// String returns a human-readable application identity.
func String() string {
	version, revision, modified := Current()
	return fmt.Sprintf("webcappi %s (commit %s; modified %s)", version, revision, modified)
}
