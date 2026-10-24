package httpapi

import (
	"net/http"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
)

func TestSourceFreshnessPolicy(t *testing.T) {
	now := time.Date(2026, 10, 24, 0, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name, control, age string
		elapsed, dated     time.Duration
		configured         int
		want               string
	}{
		{"fallback", "", "", 0, 0, 60, "private, max-age=60, must-revalidate"},
		{"disabled lifetime", "max-age=600", "", 0, 0, 0, "private, no-cache"},
		{"configured cap", "max-age=600", "", 0, 0, 60, "private, max-age=60, must-revalidate"},
		{"source cap", "max-age=30", "", 0, 0, 60, "private, max-age=30, must-revalidate"},
		{"source age and capture time", "max-age=30", "5", 2200 * time.Millisecond, 0, 60, "private, max-age=22, must-revalidate"},
		{"source date", "max-age=30", "", time.Second, 10 * time.Second, 60, "private, max-age=20, must-revalidate"},
		{"stale", "max-age=1", "5", time.Second, 0, 60, "private, no-cache"},
		{"no cache", "max-age=600, no-cache", "", 0, 0, 60, "private, no-cache"},
		{"no store", "no-store, max-age=600", "", 0, 0, 60, "private, no-store"},
		{"qualified no cache", `no-cache="Set-Cookie"`, "", 0, 0, 60, "private, no-cache"},
		{"quoted", `Max-Age="30"`, "", 0, 0, 60, "private, max-age=30, must-revalidate"},
		{"quoted comma", `extension="a,max-age=1", max-age=30`, "", 0, 0, 60, "private, max-age=30, must-revalidate"},
		{"invalid", "max-age=abc", "", 0, 0, 60, "private, no-cache"},
		{"negative", "max-age=-1", "", 0, 0, 60, "private, no-cache"},
		{"missing", "max-age", "", 0, 0, 60, "private, no-cache"},
		{"duplicates", "max-age=30, max-age=60", "", 0, 0, 60, "private, no-cache"},
		{"huge max age", "max-age=99999999999999999999999999", "", 0, 0, 60, "private, max-age=60, must-revalidate"},
		{"invalid age", "max-age=30", "invalid", 0, 0, 60, "private, no-cache"},
		{"huge age", "max-age=30", "99999999999999999999999999", 0, 0, 60, "private, no-cache"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			headers := http.Header{"Cache-Control": {tc.control}}
			if tc.age != "" {
				headers.Set("Age", tc.age)
			}
			if tc.dated > 0 {
				headers.Set("Date", now.Add(-tc.dated).Format(http.TimeFormat))
			}
			source := &capture.SourceResponse{Headers: headers, RequestStarted: now.Add(-tc.elapsed)}
			if got := cachePolicy(tc.configured, source, now).control(); got != tc.want {
				t.Fatalf("policy = %q, want %q", got, tc.want)
			}
		})
	}
}
