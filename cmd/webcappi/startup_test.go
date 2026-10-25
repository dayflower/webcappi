package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/service"
)

func TestRuntimeStartupLifecycle(t *testing.T) {
	failure := errors.New("startup failed")
	cleanupFailure := errors.New("cleanup failed")
	for _, failAt := range []string{"browser", "files", "initialize", "profile", "success"} {
		t.Run(failAt, func(t *testing.T) {
			c := config.Defaults()
			c.WorkDir = t.TempDir()
			if failAt == "files" {
				c.Profiles = map[string]capture.Profile{"default": {UserDataDir: filepath.Join(t.TempDir(), "missing")}}
			}
			r := &runtimeResources{config: c}
			var events []string
			var startup context.Context
			observe := func(ctx context.Context, stage string) error {
				events = append(events, stage)
				if startup == nil {
					startup = ctx
					deadline, ok := ctx.Deadline()
					if !ok || time.Until(deadline) <= 29*time.Second || time.Until(deadline) > 30*time.Second {
						t.Fatal("startup must have one 30-second budget", deadline)
					}
				} else if ctx != startup {
					t.Fatal("startup context was replaced between stages")
				}
				if stage == failAt {
					return failure
				}
				return nil
			}
			marker := filepath.Join(c.WorkDir, "template")
			steps := startupSteps{
				browserCheck: func(ctx context.Context) error { return observe(ctx, "browser") },
				newDefault: func(ctx context.Context) (startupDefault, error) {
					if err := observe(ctx, "initialize"); err != nil {
						return startupDefault{}, err
					}
					if err := os.WriteFile(marker, nil, 0600); err != nil {
						t.Fatal(err)
					}
					return startupDefault{
						profile: capture.Profile{UserDataDir: marker, ProfileDirectory: "Default"},
						ready: func(ctx context.Context) error {
							if err := ctx.Err(); err != nil {
								return err
							}
							_, err := os.Stat(marker)
							return err
						},
						close: func() error {
							events = append(events, "cleanup")
							if err := os.Remove(marker); err != nil {
								t.Fatal(err)
							}
							return cleanupFailure
						},
					}, nil
				},
				profileCheck: func(ctx context.Context, p capture.Profile) error {
					if p.UserDataDir != marker {
						t.Fatal("generated profile was not selected")
					}
					return observe(ctx, "profile")
				},
			}
			got, err := initializeRuntime(t.Context(), r, steps)
			want := []string{"browser"}
			switch failAt {
			case "files":
				if err == nil || errors.Is(err, cleanupFailure) {
					t.Fatal("configured file failure lost", err)
				}
			case "initialize":
				want = append(want, "initialize")
			case "profile":
				want = append(want, "initialize", "profile", "cleanup")
			case "success":
				want = append(want, "initialize", "profile", "cleanup")
				if err != nil || got != r {
					t.Fatal("startup failed", err)
				}
				if c.Profiles != nil {
					t.Fatal("input configuration was changed")
				}
				if !errors.Is(startup.Err(), context.Canceled) {
					t.Fatal("startup context was not released")
				}
				// Readiness and capture jobs survive the startup context's cancellation.
				if err := r.ready(t.Context()); err != nil {
					t.Fatal("generated template received configured placement checks", err)
				}
				s := service.New(t.Context(), r.config, func(ctx context.Context, _ capture.Request) (capture.Result, error) {
					return capture.Result{PNG: []byte("PNG")}, ctx.Err()
				})
				if _, err := s.Get(t.Context(), capture.Request{}); err != nil {
					t.Fatal("job inherited startup cancellation", err)
				}
				s.Close()
				r.close(&err)
				if !errors.Is(err, cleanupFailure) {
					t.Fatal("successful command lost cleanup failure", err)
				}
			}
			if failAt != "success" && failAt != "files" && !errors.Is(err, failure) {
				t.Fatal("startup error must take priority over cleanup", err)
			}
			if !reflect.DeepEqual(events, want) {
				t.Fatalf("events %v, want %v", events, want)
			}
			entries, err := os.ReadDir(c.WorkDir)
			if err != nil || len(entries) != 0 {
				t.Fatal("startup leaked resources", entries, err)
			}
		})
	}
}

func TestRuntimeStartupCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	c := config.Defaults()
	c.WorkDir = t.TempDir()
	closed := false
	_, err := initializeRuntime(ctx, &runtimeResources{config: c}, startupSteps{
		browserCheck: func(context.Context) error { return nil },
		newDefault: func(context.Context) (startupDefault, error) {
			cancel()
			return startupDefault{
				profile: capture.Profile{},
				close:   func() error { closed = true; return nil },
			}, nil
		},
		profileCheck: func(ctx context.Context, _ capture.Profile) error { return ctx.Err() },
	})
	if !errors.Is(err, context.Canceled) || !closed {
		t.Fatal("canceled startup did not release its template", err, closed)
	}
}
