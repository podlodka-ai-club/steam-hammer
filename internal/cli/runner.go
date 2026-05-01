package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type Runner interface {
	Run(ctx context.Context, name string, args ...string) error
}

type DetachedStarter interface {
	Start(req DetachedRequest) (DetachedProcess, error)
}

type BatchClonePreparer interface {
	Prepare(sourceDir, targetDir string) (string, error)
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

type ExecBatchClonePreparer struct{}

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
	configureDetachedProcess(cmd)

	if err := cmd.Start(); err != nil {
		_ = logFile.Close()
		return DetachedProcess{}, err
	}
	_ = logFile.Close()
	return DetachedProcess{PID: cmd.Process.Pid}, nil
}

func (ExecBatchClonePreparer) Prepare(sourceDir, targetDir string) (string, error) {
	repoRoot, err := gitOutput(sourceDir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", fmt.Errorf("failed to resolve source repository root: %w", err)
	}
	originURL, err := gitOutput(repoRoot, "config", "--get", "remote.origin.url")
	if err != nil {
		return "", fmt.Errorf("failed to resolve source repository origin: %w", err)
	}
	if err := os.RemoveAll(targetDir); err != nil {
		return "", fmt.Errorf("failed to reset worker clone directory: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(targetDir), 0o755); err != nil {
		return "", fmt.Errorf("failed to create worker clone parent directory: %w", err)
	}
	cmd := exec.Command("git", "clone", "--quiet", originURL, targetDir)
	if output, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("git clone %q into %q failed: %w%s", originURL, targetDir, err, formatCommandOutput(output))
	}
	return targetDir, nil
}

func gitOutput(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s failed: %w%s", strings.Join(args, " "), err, formatCommandOutput(output))
	}
	return strings.TrimSpace(string(output)), nil
}

func formatCommandOutput(output []byte) string {
	trimmed := strings.TrimSpace(string(output))
	if trimmed == "" {
		return ""
	}
	return ": " + trimmed
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
