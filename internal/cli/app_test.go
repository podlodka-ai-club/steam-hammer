package cli

import (
	"context"
	"reflect"
	"strings"
	"testing"
)

type recordingRunner struct {
	name  string
	args  []string
	calls int
}

func (r *recordingRunner) Run(_ context.Context, name string, args ...string) error {
	r.name = name
	r.args = append([]string(nil), args...)
	r.calls++
	return nil
}

type failingRunner struct {
	err error
}

func (r failingRunner) Run(_ context.Context, _ string, _ ...string) error {
	return r.err
}

type contextRunner struct{}

func (contextRunner) Run(ctx context.Context, _ string, _ ...string) error {
	return ctx.Err()
}

type exitCodeError int

func (e exitCodeError) Error() string { return "exit" }

func (e exitCodeError) ExitCode() int { return int(e) }

func TestHelpDoesNotInvokeRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	if code := app.Run([]string{"--help"}); code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
}

func TestDoctorCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"doctor", "--repo", "owner/repo", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--doctor", "--repo", "owner/repo", "--dry-run"})
}

func TestRunIssueCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dry-run", "--base", "current"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--issue", "71", "--repo", "owner/repo", "--dry-run", "--base", "current"})
}

func TestRunIssueBatchCommandMapsREADMEExample(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--repo", "owner/repo", "--limit", "1", "--runner", "opencode", "--agent", "build", "--model", "openai/gpt-4o"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--repo", "owner/repo", "--runner", "opencode", "--agent", "build", "--model", "openai/gpt-4o", "--limit", "1"})
}

func TestRunIssueAcceptsPythonIssueAlias(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--issue", "20", "--runner", "opencode"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--issue", "20", "--runner", "opencode"})
}

func TestRunIssueCommandMapsPythonRunnerFlags(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{
		"run", "issue",
		"--id", "71",
		"--opencode-auto-approve",
		"--branch-prefix", "fix",
		"--include-empty",
		"--stop-on-error",
		"--fail-on-existing",
		"--force-issue-flow",
		"--no-skip-if-pr-exists",
		"--no-skip-if-branch-exists",
		"--force-reprocess",
		"--no-sync-reused-branch",
		"--sync-strategy", "merge",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{
		runnerScript, "--issue", "71",
		"--opencode-auto-approve",
		"--branch-prefix", "fix",
		"--include-empty",
		"--stop-on-error",
		"--fail-on-existing",
		"--force-issue-flow",
		"--no-skip-if-pr-exists",
		"--no-skip-if-branch-exists",
		"--force-reprocess",
		"--no-sync-reused-branch",
		"--sync-strategy", "merge",
	})
}

func TestRunIssueCommandForwardsExplicitTrueFlagsForConfigPrecedence(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{
		"run", "issue",
		"--id", "71",
		"--skip-if-pr-exists",
		"--skip-if-branch-exists",
		"--sync-reused-branch",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{
		runnerScript, "--issue", "71",
		"--skip-if-pr-exists",
		"--skip-if-branch-exists",
		"--sync-reused-branch",
	})
}

func TestRunPRCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "pr", "--id", "72", "--dry-run", "--isolate-worktree"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "72", "--from-review-comments", "--dry-run", "--isolate-worktree"})
}

func TestRunPRAcceptsPythonAliases(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "pr", "--pr", "72", "--from-review-comments", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "72", "--from-review-comments", "--dry-run"})
}

func TestRunPRRequiresID(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "pr", "--dry-run"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
}

func TestPythonRunnerExitCodeIsPreserved(t *testing.T) {
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(failingRunner{err: exitCodeError(17)})

	if code := app.Run([]string{"run", "issue", "--id", "71"}); code != 17 {
		t.Fatalf("Run() code = %d, want 17", code)
	}
	if !strings.Contains(errOut.String(), "python runner exited with code 17") {
		t.Fatalf("stderr = %q, want exit code message", errOut.String())
	}
}

func TestPythonRunnerContextCancellation(t *testing.T) {
	var errOut strings.Builder
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(contextRunner{})

	if code := app.RunContext(ctx, []string{"run", "issue", "--id", "71"}); code != 130 {
		t.Fatalf("RunContext() code = %d, want 130", code)
	}
	if !strings.Contains(errOut.String(), "python runner canceled") {
		t.Fatalf("stderr = %q, want cancellation message", errOut.String())
	}
}

func assertCommand(t *testing.T, runner *recordingRunner, wantArgs []string) {
	t.Helper()
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	if runner.name != "python3" {
		t.Fatalf("runner name = %q, want python3", runner.name)
	}
	if !reflect.DeepEqual(runner.args, wantArgs) {
		t.Fatalf("runner args = %#v, want %#v", runner.args, wantArgs)
	}
}
