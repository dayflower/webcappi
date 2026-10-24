package httpapi

import (
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
)

// responsePolicy limits downstream freshness using the main document.
// No source policy can prove freshness of scripts, images or extension state.
type responsePolicy struct {
	maxAge  int
	noStore bool
}

func (p responsePolicy) control() string {
	if p.noStore {
		return "private, no-store"
	}
	if p.maxAge <= 0 {
		return "private, no-cache"
	}
	return fmt.Sprintf("private, max-age=%d, must-revalidate", p.maxAge)
}

func cachePolicy(configured int, source *capture.SourceResponse, now time.Time) responsePolicy {
	maxAge := configured
	if source != nil {
		var noStore, noCache, hasMaxAge bool
		var sourceMaxAge time.Duration
		for _, directive := range splitCacheDirectives(strings.Join(source.Headers.Values("Cache-Control"), ",")) {
			key, value, _ := strings.Cut(directive, "=")
			switch strings.ToLower(strings.TrimSpace(key)) {
			case "no-store":
				noStore = true
			case "no-cache":
				noCache = true
			case "max-age":
				value = strings.TrimSpace(value)
				if len(value) >= 2 && value[0] == '"' && value[len(value)-1] == '"' {
					value = value[1 : len(value)-1]
				}
				seconds, valid := deltaSeconds(value)
				if hasMaxAge || !valid {
					noCache = true
				}
				hasMaxAge = true
				sourceMaxAge = time.Duration(seconds) * time.Second
			}
		}
		if noStore {
			return responsePolicy{noStore: true}
		}
		if noCache {
			return responsePolicy{}
		}
		if hasMaxAge {
			// Conservatively include navigation, rendering and browser cleanup.
			age := max(time.Duration(0), now.Sub(source.RequestStarted))
			if value := source.Headers.Get("Age"); value != "" {
				seconds, valid := deltaSeconds(value)
				if !valid || len(source.Headers.Values("Age")) != 1 {
					return responsePolicy{}
				}
				age += time.Duration(seconds) * time.Second
			}
			if date, err := http.ParseTime(source.Headers.Get("Date")); err == nil {
				age = max(age, now.Sub(date))
			}
			remaining := max(time.Duration(0), sourceMaxAge-age)
			maxAge = min(maxAge, int(remaining/time.Second))
		}
	}
	return responsePolicy{maxAge: max(0, maxAge)}
}

// deltaSeconds accepts HTTP non-negative integers and saturates large values.
func deltaSeconds(value string) (int64, bool) {
	if value == "" {
		return 0, false
	}
	for _, c := range value {
		if c < '0' || c > '9' {
			return 0, false
		}
	}
	seconds, err := strconv.ParseUint(value, 10, 64)
	if err != nil || seconds > 1<<31 {
		return 1 << 31, true
	}
	return int64(seconds), true
}

// Commas inside quoted extension directives do not separate directives.
func splitCacheDirectives(value string) []string {
	var parts []string
	start := 0
	quoted, escaped := false, false
	for i, c := range value {
		if escaped {
			escaped = false
			continue
		}
		if quoted && c == '\\' {
			escaped = true
		} else if c == '"' {
			quoted = !quoted
		} else if c == ',' && !quoted {
			parts = append(parts, value[start:i])
			start = i + 1
		}
	}
	return append(parts, value[start:])
}
