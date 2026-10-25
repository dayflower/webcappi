package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/dayflower/webcappi/internal/capture"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		slog.Error("webcappi", "error", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: webcappi serve|capture|check|init-profile|version [flags]")
	}
	switch args[0] {
	case "version":
		fmt.Println("webcappi; Chrome for Testing " + capture.BrowserVersion)
		return nil
	case "init-profile":
		return runInitProfile(args[1:])
	case "serve":
		return runServe(args[1:])
	case "capture":
		return runCapture(args[1:])
	case "check":
		return runCheck(args[1:])
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

// Retain the flags historically accepted by all three configuration commands.
func parseCommandFlags(command string, args []string) (*flag.FlagSet, string, *captureFlags, error) {
	fs := flag.NewFlagSet(command, flag.ContinueOnError)
	path := fs.String("config", "config.json", "server configuration")
	flags := registerCaptureFlags(fs)
	if err := fs.Parse(args); err != nil {
		return nil, "", nil, err
	}
	if fs.NArg() != 0 {
		return nil, "", nil, errors.New("unexpected positional arguments")
	}
	return fs, *path, flags, nil
}

func commandContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
}
