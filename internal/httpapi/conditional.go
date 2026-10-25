package httpapi

import (
	"errors"
	"strings"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/service"
)

// captureValidation holds one GET's metadata snapshot and request-start order.
// It does not retain PNG bytes or own another cache.
type captureValidation struct {
	key      string
	order    uint64
	previous *metadata
	matches  bool
}

func (a *API) lookupValidation(req capture.Request, tags []string, order uint64) captureValidation {
	key := service.Key(req)
	previous := a.metadata.get(key)
	return captureValidation{
		key: key, order: order, previous: previous,
		matches: previous != nil && matchesETag(tags, previous.etag),
	}
}

func (v captureValidation) fresh(now time.Time) bool {
	return v.matches && now.Before(v.previous.expires())
}

// The renderer decides whether the source has a usable validator.
func (v captureValidation) revalidationSource() *capture.SourceResponse {
	if v.matches {
		return v.previous.source
	}
	return nil
}

func (a *API) recordMetadata(v captureValidation, img *service.Image) (*metadata, error) {
	now := a.now()
	etag, source := img.ETag, img.Source
	if img.NotModified {
		if v.previous == nil {
			return nil, errors.New("unexpected source validation response")
		}
		// Service results may be shared; merging must not change the result.
		etag = v.previous.etag
		source = mergeSource(v.previous.source, source)
	}
	entry := &metadata{
		key: v.key, etag: etag, source: source, validated: now,
		policy: cachePolicy(a.cfg.MaxAgeSeconds, source, now), order: v.order,
	}
	// The existing cache enforces ordering, no-store removal and size limits.
	a.metadata.put(entry)
	return entry, nil
}

func matchesETag(headers []string, etag string) bool {
	for _, h := range headers {
		for _, item := range strings.Split(h, ",") {
			item = strings.TrimSpace(item)
			if item == "*" || strings.TrimPrefix(item, "W/") == etag {
				return true
			}
		}
	}
	return false
}
