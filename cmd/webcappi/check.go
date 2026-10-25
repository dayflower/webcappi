package main

import "fmt"

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
	fmt.Println("configuration, bundle and browser version verified")
	return nil
}
