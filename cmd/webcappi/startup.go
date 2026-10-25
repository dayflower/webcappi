package main

import (
	"context"
	"log/slog"
	"maps"
	"os"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/dayflower/webcappi/internal/httpapi"
)

// Runtime resources outlive command services and all of their browser jobs.
type runtimeResources struct {
	config   config.Config
	renderer capture.Renderer
	auth     *httpapi.BearerAuth
	ready    func(context.Context) error
	cleanup  func() error
}

// Preserve the command error; report cleanup failures only after success.
func (r *runtimeResources) close(err *error) {
	if r.cleanup != nil {
		if e := r.cleanup(); *err == nil {
			*err = e
		}
	}
}

type startupDefault struct {
	profile capture.Profile
	ready   func(context.Context) error
	close   func() error
}

// These boundaries allow acquisition failures and cleanup to be checked without Chromium.
type startupSteps struct {
	browserCheck func(context.Context) error
	newDefault   func(context.Context) (startupDefault, error)
	profileCheck func(context.Context, capture.Profile) error
}

func prepareRuntime(ctx context.Context, path string) (*runtimeResources, error) {
	c, err := config.Load(path)
	if err != nil {
		return nil, err
	}
	auth, err := httpapi.NewBearerAuth(c.Auth)
	if err != nil {
		return nil, err
	}
	renderer := capture.Renderer{
		Browser: capture.Browser{Path: c.BrowserPath, NoSandbox: c.NoSandbox},
		WorkDir: c.WorkDir, MaxProfileBytes: c.MaxProfileBytes, ResizeAlgorithm: c.ResizeAlgorithm,
	}
	resources := &runtimeResources{config: c, renderer: renderer, auth: auth}
	return initializeRuntime(ctx, resources, startupSteps{
		browserCheck: renderer.Browser.Check,
		newDefault: func(ctx context.Context) (startupDefault, error) {
			p, err := renderer.NewProfile(ctx)
			if err != nil {
				return startupDefault{}, err
			}
			return startupDefault{profile: p.Profile, ready: p.CheckFiles, close: p.Close}, nil
		},
		profileCheck: renderer.Check,
	})
}

func initializeRuntime(ctx context.Context, r *runtimeResources, steps startupSteps) (_ *runtimeResources, err error) {
	if err := os.MkdirAll(r.config.WorkDir, 0700); err != nil {
		return nil, err
	}
	startup, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	defer func() {
		if err != nil {
			r.close(&err)
		}
	}()
	// Capture the configured bundles before adding a template under WorkDir.
	r.ready = r.config.CheckFiles
	if err := steps.browserCheck(startup); err != nil {
		return nil, err
	}
	if err := r.ready(startup); err != nil {
		return nil, err
	}
	if _, provided := r.config.Profiles[config.DefaultProfileName]; !provided {
		p, err := steps.newDefault(startup)
		if err != nil {
			return nil, err
		}
		r.cleanup = p.close
		providedFiles := r.ready
		r.ready = func(ctx context.Context) error {
			if err := providedFiles(ctx); err != nil {
				return err
			}
			return p.ready(ctx)
		}
		r.config.Profiles = maps.Clone(r.config.Profiles)
		if r.config.Profiles == nil {
			r.config.Profiles = make(map[string]capture.Profile)
		}
		r.config.Profiles[config.DefaultProfileName] = p.profile
		slog.Info("temporary default profile created", "user_data_dir", p.profile.UserDataDir)
	}
	for _, p := range r.config.Profiles {
		if err := steps.profileCheck(startup, p); err != nil {
			return nil, err
		}
	}
	return r, nil
}
