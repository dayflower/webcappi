package httpapi

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/service"
)

// captureValidation holds one GET's metadata snapshot and update fence.
// It does not retain PNG bytes or own another cache.
type captureValidation struct {
	key      string
	order    uint64
	previous *metadata
	matches  bool
	snapshot metadataSnapshot
}

func (a *API) lookupValidation(ctx context.Context, req capture.Request, tags []string) (captureValidation, error) {
	key := service.Key(req)
	snapshot, err := a.metadata.begin(ctx, key, a.now())
	if err != nil {
		return captureValidation{}, err
	}
	previous := snapshot.previous
	return captureValidation{
		key: key, order: snapshot.order, previous: previous, snapshot: snapshot,
		matches: previous != nil && matchesETag(tags, previous.etag),
	}, nil
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

func (a *API) recordMetadata(ctx context.Context, v captureValidation, img *service.Image) (*metadata, error) {
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
	// The selected store enforces ordering, no-store removal and size limits.
	responseTime, err := a.metadata.commit(ctx, v.snapshot, entry)
	if err != nil {
		return nil, err
	}
	entry.responseTime = responseTime
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
