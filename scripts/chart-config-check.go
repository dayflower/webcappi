//go:build ignore

// Validate rendered JSON with the application's actual strict config loader.
package main

import (
	"fmt"
	"os"

	"github.com/dayflower/webcappi/internal/config"
)

func main() {
	for _, path := range os.Args[1:] {
		if _, err := config.Load(path); err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", path, err)
			os.Exit(1)
		}
	}
}
