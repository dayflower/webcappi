package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
)

var ErrBusy = errors.New("capture capacity exhausted")
var ErrClosed = errors.New("service is shutting down")

type RenderFunc func(context.Context, capture.Request) (capture.Result, error)

type Image struct {
	PNG         []byte
	ETag        string
	Source      *capture.SourceResponse
	NotModified bool
}

// Service bounds independent captures. It retains no completed images.
type Service struct {
	cfg    config.Config
	render RenderFunc
	ctx    context.Context
	cancel context.CancelFunc
	slots  chan struct{}
	mu     sync.Mutex
	closed bool
	active int
	wg     sync.WaitGroup
}

func New(parent context.Context, cfg config.Config, render RenderFunc) *Service {
	ctx, cancel := context.WithCancel(parent)
	return &Service{cfg: cfg, render: render, ctx: ctx, cancel: cancel, slots: make(chan struct{}, cfg.Concurrency)}
}

func Key(req capture.Request) string {
	// Capture conditions identify metadata; no PNG bytes are retained.
	outputWidth, outputHeight := req.OutputWidth, req.OutputHeight
	// Preserve single-dimension requests: rounding may make their complete-image
	// resize differ from an explicit canvas with the same resolved dimensions.
	if !req.FullPage && outputWidth == 0 && outputHeight == 0 {
		outputWidth, outputHeight = req.Width, req.Height
	}
	b, _ := json.Marshal(struct {
		Schema        string
		URL           string
		Width         int
		Height        int
		OutputWidth   int
		OutputHeight  int
		ProfileName   string
		ReadySelector string
		DelayMS       int
		FullPage      bool
	}{
		Schema:        "page-png-v3-dsf1-cover-center",
		URL:           req.URL,
		Width:         req.Width,
		Height:        req.Height,
		OutputWidth:   outputWidth,
		OutputHeight:  outputHeight,
		ProfileName:   req.ProfileName,
		ReadySelector: req.ReadySelector,
		DelayMS:       req.DelayMS,
		FullPage:      req.FullPage,
	})
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func (s *Service) Get(ctx context.Context, req capture.Request) (*Image, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	if s.closed || s.ctx.Err() != nil {
		s.mu.Unlock()
		return nil, ErrClosed
	}
	if s.active >= s.cfg.Concurrency+s.cfg.QueueSize {
		s.mu.Unlock()
		return nil, ErrBusy
	}
	s.active++
	s.wg.Add(1)
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.active--
		s.mu.Unlock()
		s.wg.Done()
	}()

	// Queueing and rendering share the request deadline and service shutdown.
	job, cancel := context.WithTimeout(ctx, s.cfg.Timeout())
	stop := context.AfterFunc(s.ctx, cancel)
	defer stop()
	defer cancel()
	select {
	case s.slots <- struct{}{}:
		defer func() { <-s.slots }()
	case <-job.Done():
		return nil, job.Err()
	}
	if err := job.Err(); err != nil {
		return nil, err
	}
	result, err := s.render(job, req)
	// Wait for renderer cleanup before releasing capacity, even on cancellation.
	if job.Err() != nil {
		return nil, job.Err()
	}
	if err != nil {
		return nil, err
	}
	if int64(len(result.PNG)) > s.cfg.MaxPNGBytes {
		return nil, fmt.Errorf("generated PNG exceeds size limit")
	}
	if result.NotModified {
		return &Image{Source: result.Source, NotModified: true}, nil
	}
	h := sha256.Sum256(result.PNG)
	return &Image{PNG: result.PNG, ETag: `"` + hex.EncodeToString(h[:]) + `"`, Source: result.Source}, nil
}

func (s *Service) Close() {
	s.mu.Lock()
	s.closed = true
	s.cancel()
	s.mu.Unlock()
	s.wg.Wait()
}
