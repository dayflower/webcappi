package service

import (
	"container/list"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
)

var ErrBusy = errors.New("capture capacity exhausted")
var ErrClosed = errors.New("service is shutting down")

type RenderFunc func(context.Context, capture.Request) ([]byte, error)
type Image struct {
	PNG     []byte
	ETag    string
	Created time.Time
	Expires time.Time
}
type cached struct {
	key   string
	image *Image
}
type flight struct {
	ctx     context.Context
	cancel  context.CancelFunc
	done    chan struct{}
	waiters int
	image   *Image
	err     error
}
type Service struct {
	cfg     config.Config
	render  RenderFunc
	ctx     context.Context
	cancel  context.CancelFunc
	slots   chan struct{}
	mu      sync.Mutex
	closed  bool
	flights map[string]*flight
	cache   map[string]*list.Element
	lru     *list.List
	bytes   int64
	wg      sync.WaitGroup
}

func New(parent context.Context, cfg config.Config, render RenderFunc) *Service {
	ctx, cancel := context.WithCancel(parent)
	return &Service{cfg: cfg, render: render, ctx: ctx, cancel: cancel, slots: make(chan struct{}, cfg.Concurrency), flights: map[string]*flight{}, cache: map[string]*list.Element{}, lru: list.New()}
}

func Key(req capture.Request) string {
	// Include every rendering input, the entire profile descriptor and a renderer
	// schema version. Bundle revision must change when bundle contents change.
	b, _ := json.Marshal(struct {
		Schema  string
		Request capture.Request
	}{"viewport-png-v1-dsf1", req})
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func (s *Service) Get(ctx context.Context, req capture.Request) (*Image, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	key := Key(req)
	s.mu.Lock()
	if s.closed || s.ctx.Err() != nil {
		s.mu.Unlock()
		return nil, ErrClosed
	}
	s.expire(time.Now())
	if e := s.cache[key]; e != nil {
		s.lru.MoveToFront(e)
		img := e.Value.(cached).image
		s.mu.Unlock()
		return img, nil
	}
	f := s.flights[key]
	if f == nil {
		if len(s.flights) >= s.cfg.Concurrency+s.cfg.QueueSize {
			s.mu.Unlock()
			return nil, ErrBusy
		}
		job, cancel := context.WithTimeout(s.ctx, s.cfg.Timeout())
		f = &flight{ctx: job, cancel: cancel, done: make(chan struct{})}
		s.flights[key] = f
		s.wg.Add(1)
		go s.run(key, f, req)
	} else if f.ctx.Err() != nil {
		s.mu.Unlock()
		return nil, ErrBusy
	}
	f.waiters++
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		f.waiters--
		if f.waiters == 0 {
			f.cancel()
		}
		s.mu.Unlock()
	}()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-f.done:
		return f.image, f.err
	}
}

func (s *Service) run(key string, f *flight, req capture.Request) {
	defer s.wg.Done()
	defer f.cancel()
	select {
	case s.slots <- struct{}{}:
	case <-f.ctx.Done():
		s.finish(key, f, nil, f.ctx.Err())
		return
	}
	png, err := s.render(f.ctx, req)
	<-s.slots
	// Cancellation wins even when a renderer returns late with an image.
	if f.ctx.Err() != nil {
		err = f.ctx.Err()
	}
	var image *Image
	if err == nil {
		if int64(len(png)) > s.cfg.MaxPNGBytes {
			err = fmt.Errorf("generated PNG exceeds size limit")
		} else {
			h := sha256.Sum256(png)
			now := time.Now()
			image = &Image{PNG: png, ETag: `"` + hex.EncodeToString(h[:]) + `"`, Created: now, Expires: now.Add(s.cfg.TTL())}
		}
	}
	s.finish(key, f, image, err)
}

func (s *Service) finish(key string, f *flight, image *Image, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.flights, key)
	f.image = image
	f.err = err
	if err == nil && !s.closed && f.ctx.Err() == nil && s.cfg.CacheTTLSeconds > 0 && s.cfg.CacheMaxEntries > 0 && int64(len(image.PNG)) <= s.cfg.CacheMaxBytes {
		s.expire(time.Now())
		for s.lru.Len() >= s.cfg.CacheMaxEntries || s.bytes+int64(len(image.PNG)) > s.cfg.CacheMaxBytes {
			s.remove(s.lru.Back())
		}
		e := s.lru.PushFront(cached{key, image})
		s.cache[key] = e
		s.bytes += int64(len(image.PNG))
	}
	close(f.done)
}
func (s *Service) remove(e *list.Element) {
	if e == nil {
		return
	}
	v := e.Value.(cached)
	delete(s.cache, v.key)
	s.bytes -= int64(len(v.image.PNG))
	s.lru.Remove(e)
}
func (s *Service) expire(now time.Time) {
	for e := s.lru.Back(); e != nil; {
		previous := e.Prev()
		if !now.Before(e.Value.(cached).image.Expires) {
			s.remove(e)
		}
		e = previous
	}
}
func (s *Service) Close() { s.mu.Lock(); s.closed = true; s.cancel(); s.mu.Unlock(); s.wg.Wait() }
