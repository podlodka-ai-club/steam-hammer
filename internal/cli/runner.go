package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

type Runner interface {
	Run(ctx context.Context, name string, args ...string) error
}

type DetachedStarter interface {
	Start(req DetachedRequest) (DetachedProcess, error)
}

type DetachedRequest struct {
	Name    string
	Args    []string
	Dir     string
	LogPath string
}

type DetachedProcess struct {
	PID int
}

type ExecRunner struct {
	Stdout io.Writer
	Stderr io.Writer
}

func (r ExecRunner) Run(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout = r.Stdout
	cmd.Stderr = r.Stderr
	cmd.Stdin = os.Stdin
	return cmd.Run()
}

type ExecDetachedStarter struct{}

func (ExecDetachedStarter) Start(req DetachedRequest) (DetachedProcess, error) {
	if err := os.MkdirAll(filepath.Dir(req.LogPath), 0o755); err != nil {
		return DetachedProcess{}, fmt.Errorf("failed to create log directory: %w", err)
	}
	logFile, err := os.OpenFile(req.LogPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return DetachedProcess{}, fmt.Errorf("failed to open log file: %w", err)
	}

	cmd := exec.Command(req.Name, req.Args...)
	cmd.Dir = req.Dir
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.Stdin = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		_ = logFile.Close()
		return DetachedProcess{}, err
	}
	_ = logFile.Close()
	return DetachedProcess{PID: cmd.Process.Pid}, nil
}

func (a *App) runPython(ctx context.Context, args []string) int {
	if err := a.runner.Run(ctx, "python3", args...); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			_, _ = fmt.Fprintln(a.err, "orchestrator: python runner timed out")
			return 124
		}
		if errors.Is(ctx.Err(), context.Canceled) {
			_, _ = fmt.Fprintln(a.err, "orchestrator: python runner canceled")
			return 130
		}

		var exitErr interface{ ExitCode() int }
		if errors.As(err, &exitErr) {
			code := exitErr.ExitCode()
			if code >= 0 {
				_, _ = fmt.Fprintf(a.err, "orchestrator: python runner exited with code %d\n", code)
				return code
			}
		}

		_, _ = fmt.Fprintf(a.err, "orchestrator: python runner failed: %v\n", err)
		return 1
	}
	return 0
}
