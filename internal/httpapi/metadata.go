package httpapi

import (
	"container/list"
	"context"
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
)

// metadata deliberately contains no image bytes. Expired entries can supply
// source validators, but cannot produce a 304 without checking the source.
type metadata struct {
	key          string
	etag         string
	source       *capture.SourceResponse
	validated    time.Time
	policy       responsePolicy
	order        uint64
	bytes        int64
	responseTime time.Time
}

func (m *metadata) expires() time.Time {
	return m.validated.Add(time.Duration(m.policy.maxAge) * time.Second)
}

type metadataCache struct {
	mu         sync.Mutex
	entries    map[string]*list.Element
	lru        *list.List
	bytes      int64
	maxBytes   int64
	maxEntries int
	next       uint64
	active     map[string]*metadataFence
}

type metadataFence struct {
	order uint64
	count int
}

func newMetadataCache(entries int, bytes int64) *metadataCache {
	return &metadataCache{entries: make(map[string]*list.Element), lru: list.New(), maxEntries: entries, maxBytes: bytes, active: make(map[string]*metadataFence)}
}

func (c *metadataCache) begin(_ context.Context, key string, now time.Time) (metadataSnapshot, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.next++
	f := c.active[key]
	if f == nil {
		f = &metadataFence{}
		c.active[key] = f
	}
	f.count++
	s := metadataSnapshot{key: key, order: c.next, now: now}
	if e := c.entries[key]; e != nil {
		c.lru.MoveToFront(e)
		s.previous = e.Value.(*metadata)
	}
	return s, nil
}

func (c *metadataCache) commit(_ context.Context, s metadataSnapshot, value *metadata) (time.Time, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if f := c.active[s.key]; f != nil && f.order <= s.order {
		c.putLocked(value)
		f.order = s.order
	}
	return value.validated, nil
}

func (c *metadataCache) finish(s metadataSnapshot) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if f := c.active[s.key]; f != nil {
		f.count--
		if f.count == 0 {
			delete(c.active, s.key)
		}
	}
}
func (c *metadataCache) check(context.Context) error { return nil }
func (c *metadataCache) close() error                { return nil }

func (c *metadataCache) get(key string) *metadata {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		return nil
	}
	c.lru.MoveToFront(e)
	return e.Value.(*metadata)
}

func (c *metadataCache) put(value *metadata) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.putLocked(value)
}

func (c *metadataCache) putLocked(value *metadata) {
	// Keep only response metadata used for freshness and conditional navigation.
	value = cloneMetadata(value)
	if old := c.entries[value.key]; old != nil {
		if old.Value.(*metadata).order > value.order {
			return
		}
		c.remove(old)
	}
	if value.policy.noStore || c.maxEntries == 0 || value.bytes > c.maxBytes {
		return
	}
	for c.lru.Len() >= c.maxEntries || c.bytes+value.bytes > c.maxBytes {
		c.remove(c.lru.Back())
	}
	c.entries[value.key] = c.lru.PushFront(value)
	c.bytes += value.bytes
}

func (c *metadataCache) remove(e *list.Element) {
	value := e.Value.(*metadata)
	delete(c.entries, value.key)
	c.bytes -= value.bytes
	c.lru.Remove(e)
}

func cloneMetadata(value *metadata) *metadata {
	copy := *value
	copy.bytes = int64(len(copy.key) + len(copy.etag) + 128)
	if value.source != nil {
		source := *value.source
		source.Headers = make(http.Header)
		copy.bytes += int64(len(source.URL))
		for _, key := range []string{"Cache-Control", "Date", "Age", "ETag", "Last-Modified"} {
			for _, v := range value.source.Headers.Values(key) {
				source.Headers.Add(key, v)
				copy.bytes += int64(len(key) + len(v) + 32)
			}
		}
		copy.source = &source
	}
	return &copy
}

func mergeSource(previous, response *capture.SourceResponse) *capture.SourceResponse {
	if previous == nil || response == nil {
		return response
	}
	merged := *response
	merged.Headers = previous.Headers.Clone()
	// Date and Age describe this validation, not the previous response.
	merged.Headers.Del("Date")
	merged.Headers.Del("Age")
	for key, values := range response.Headers {
		merged.Headers[key] = append([]string(nil), values...)
	}
	return &merged
}

func writeValidationHeaders(w http.ResponseWriter, value *metadata, now time.Time) {
	w.Header().Set("ETag", value.etag)
	w.Header().Set("Cache-Control", value.policy.control())
	if value.policy.maxAge > 0 && !value.policy.noStore {
		age := max(0, int(math.Ceil(now.Sub(value.validated).Seconds())))
		w.Header().Set("Age", strconv.Itoa(age))
	}
}
