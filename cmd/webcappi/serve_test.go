package main

import (
	"context"
	"errors"
	"net"
	"testing"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/httpapi"
	"github.com/dayflower/webcappi/internal/service"
)

func TestServeListenFailureClosesService(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	c := config.Defaults()
	c.Listen = listener.Addr().String()
	auth, err := httpapi.NewBearerAuth(nil)
	if err != nil {
		t.Fatal(err)
	}
	entered := make(chan struct{})
	cleaned := make(chan struct{})
	s := service.New(t.Context(), c, func(ctx context.Context, _ capture.Request) (capture.Result, error) {
		close(entered)
		<-ctx.Done()
		close(cleaned)
		return capture.Result{}, ctx.Err()
	})
	defer s.Close()
	job := make(chan error, 1)
	go func() { _, err := s.Get(t.Context(), capture.Request{}); job <- err }()
	<-entered
	r := &runtimeResources{config: c, auth: auth, ready: func(context.Context) error { return nil }}
	r.cleanup = func() error {
		select {
		case <-cleaned:
		default:
			t.Error("template removed before renderer cleanup")
		}
		return errors.New("cleanup failed")
	}
	err = serveHTTP(t.Context(), r, s)
	var listenError *net.OpError
	if !errors.As(err, &listenError) {
		t.Fatal("listen failure lost", err)
	}
	r.close(&err)
	if !errors.As(err, &listenError) {
		t.Fatal("cleanup replaced listen failure", err)
	}
	if err := <-job; !errors.Is(err, context.Canceled) {
		t.Fatal("active job was not canceled", err)
	}
	if _, err := s.Get(t.Context(), capture.Request{}); !errors.Is(err, service.ErrClosed) {
		t.Fatal("service still admits jobs", err)
	}
}
