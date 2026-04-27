package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/podlodka-ai-club/steam-hammer/internal/cli"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	app := cli.NewApp(os.Stdout, os.Stderr)
	os.Exit(app.RunContext(ctx, os.Args[1:]))
}
