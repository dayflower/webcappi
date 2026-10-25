package httpapi

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"strings"
	"time"

	"github.com/dayflower/webcappi/internal/config"
)

var errMetadataUnavailable = errors.New("metadata cache unavailable")

// A snapshot carries a fencing token and a clock reference for one admitted GET.
type metadataSnapshot struct {
	key         string
	token       string
	order       uint64
	previous    *metadata
	now         time.Time
	received    time.Time
	clockMargin time.Duration
}

func (s metadataSnapshot) currentTime() time.Time {
	if s.received.IsZero() {
		return s.now
	}
	return s.now.Add(s.clockMargin + time.Since(s.received))
}

type metadataStore interface {
	begin(context.Context, string, time.Time) (metadataSnapshot, error)
	commit(context.Context, metadataSnapshot, *metadata) (time.Time, error)
	finish(metadataSnapshot)
	check(context.Context) error
	close() error
}

// Cache owns the selected metadata store and its connections.
type Cache struct{ store metadataStore }

func OpenCache(ctx context.Context, c config.Config) (*Cache, error) {
	// The runtime may already contain a generated profile under work_dir, which
	// is deliberately not a valid user-supplied profile configuration.
	if err := c.Cache.Validate(c.MaxAgeSeconds); err != nil {
		return nil, err
	}
	if c.Cache.Backend != "redis" {
		return &Cache{store: newMetadataCache(c.MetadataMaxEntries, c.MetadataMaxBytes)}, nil
	}
	s, err := openRedisMetadata(ctx, c)
	if err != nil {
		return nil, err
	}
	return &Cache{store: s}, nil
}

func (c *Cache) Check(ctx context.Context) error { return c.store.check(ctx) }
func (c *Cache) Close() error                    { return c.store.close() }

func metadataFailure(operation string, causes ...error) error {
	// Do not log URLs, credentials, source headers, or Redis error replies.
	kind := "unavailable"
	if operation == "decode" {
		kind = "invalid-data"
	}
	if len(causes) > 0 {
		cause := causes[0]
		var networkError net.Error
		switch {
		case errors.Is(cause, context.Canceled):
			kind = "canceled"
		case errors.Is(cause, context.DeadlineExceeded):
			kind = "timeout"
		case errors.As(cause, &networkError) && networkError.Timeout():
			kind = "timeout"
		case strings.HasPrefix(cause.Error(), "WRONGPASS"), strings.HasPrefix(cause.Error(), "NOAUTH"), strings.HasPrefix(cause.Error(), "NOPERM"):
			kind = "authentication-or-permission"
		case strings.HasPrefix(cause.Error(), "OOM"):
			kind = "capacity"
		}
	}
	slog.Warn("metadata cache operation failed", "operation", operation, "kind", kind)
	return errMetadataUnavailable
}
