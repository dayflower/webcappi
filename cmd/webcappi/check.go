package main

import (
	"fmt"
	"github.com/dayflower/webcappi/internal/httpapi"
)

func runCheck(args []string) (err error) {
	_, path, _, err := parseCommandFlags("check", args)
	if err != nil {
		return err
	}
	ctx, stop := commandContext()
	defer stop()
	r, err := prepareRuntime(ctx, path)
	if err != nil {
		return err
	}
	defer r.close(&err)
	cache, err := httpapi.OpenCache(ctx, r.config)
	if err != nil {
		return err
	}
	defer func() {
		if e := cache.Close(); err == nil {
			err = e
		}
	}()
	fmt.Println("configuration, bundle, browser version and metadata cache verified")
	return nil
}
