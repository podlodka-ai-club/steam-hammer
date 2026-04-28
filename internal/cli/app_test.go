package cli

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type recordingRunner struct {
	name  string
	args  []string
	calls int
	cmds  [][]string
}

func (r *recordingRunner) Run(_ context.Context, name string, args ...string) error {
	r.name = name
	r.args = append([]string(nil), args...)
	r.calls++
	r.cmds = append(r.cmds, append([]string{name}, args...))
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

func TestAutoDoctorCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"autodoctor", "--repo", "owner/repo", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--doctor", "--repo", "owner/repo", "--dry-run"})
}

func TestStatusIssueCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"status", "--issue", "71", "--repo", "owner/repo", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--status", "--issue", "71", "--repo", "owner/repo", "--dry-run"})
}

func TestStatusPRCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"status", "--pr", "72", "--repo", "owner/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--status", "--pr", "72", "--repo", "owner/repo"})
}

func TestInitCreatesConfigScaffolds(t *testing.T) {
	targetDir := t.TempDir()
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})

	code := app.Run([]string{"init", "--dir", targetDir})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}

	for _, name := range []string{defaultProjectConfigName, defaultLocalConfigName} {
		path := filepath.Join(targetDir, name)
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("ReadFile(%q) error = %v", path, err)
		}
		if len(data) == 0 {
			t.Fatalf("ReadFile(%q) returned empty scaffold", path)
		}
	}
	if !strings.Contains(out.String(), filepath.Join(targetDir, defaultProjectConfigName)) {
		t.Fatalf("stdout = %q, want created project-config path", out.String())
	}
}

func TestScaffoldsMatchExampleConfigs(t *testing.T) {
	projectExample, err := os.ReadFile(filepath.Join("..", "..", "project-config.example.json"))
	if err != nil {
		t.Fatalf("ReadFile(project example) error = %v", err)
	}
	if got := strings.TrimSpace(projectConfigScaffold); got != strings.TrimSpace(string(projectExample)) {
		t.Fatalf("project scaffold drifted from example config")
	}

	localExample, err := os.ReadFile(filepath.Join("..", "..", "local-config.example.json"))
	if err != nil {
		t.Fatalf("ReadFile(local example) error = %v", err)
	}
	if got := strings.TrimSpace(localConfigScaffold); got != strings.TrimSpace(string(localExample)) {
		t.Fatalf("local scaffold drifted from example config")
	}
}

func TestInitRefusesToOverwriteWithoutForce(t *testing.T) {
	targetDir := t.TempDir()
	projectPath := filepath.Join(targetDir, defaultProjectConfigName)
	if err := os.WriteFile(projectPath, []byte("existing\n"), 0o644); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)

	if code := app.Run([]string{"init", "--dir", targetDir, "--skip-local-config"}); code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if !strings.Contains(errOut.String(), "already exists") {
		t.Fatalf("stderr = %q, want overwrite guidance", errOut.String())
	}
	data, err := os.ReadFile(projectPath)
	if err != nil {
		t.Fatalf("ReadFile() error = %v", err)
	}
	if string(data) != "existing\n" {
		t.Fatalf("project config = %q, want original contents preserved", string(data))
	}
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
		"--conflict-recovery-only",
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
		"--conflict-recovery-only",
		"--no-sync-reused-branch",
		"--sync-strategy", "merge",
	})
}

func TestRunIssueCommandForwardsExplicitDefaultTrueFlags(t *testing.T) {
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

func TestRunIssueCommandAcceptsIssueAlias(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--issue", "71"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--issue", "71"})
}

func TestRunIssueCommandMapsCoreCompatibilityFlags(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{
		"run", "issue",
		"--id", "20",
		"--repo", "owner/repo",
		"--tracker", "jira",
		"--codehost", "github",
		"--preset", "hard",
		"--runner", "opencode",
		"--agent", "build",
		"--model", "openai/gpt-4o",
		"--max-attempts", "3",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{
		runnerScript, "--issue", "20",
		"--repo", "owner/repo",
		"--tracker", "jira",
		"--codehost", "github",
		"--runner", "opencode",
		"--agent", "build",
		"--model", "openai/gpt-4o",
		"--preset", "hard",
		"--max-attempts", "3",
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

func TestRunDaemonCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--autonomous", "--state", "open", "--limit", "3", "--repo", "owner/repo", "--dry-run"})
	assertCommandContainsFlag(t, runner.args, "--autonomous-session-file")
}

func TestRunDaemonReusesAutonomousSessionFileAcrossCycles(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--poll-interval-seconds", "0", "--max-cycles", "2", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 2 {
		t.Fatalf("runner calls = %d, want 2", runner.calls)
	}
	firstSessionPath := flagValue(runner.cmds[0][1:], "--autonomous-session-file")
	secondSessionPath := flagValue(runner.cmds[1][1:], "--autonomous-session-file")
	if firstSessionPath == "" || secondSessionPath == "" {
		t.Fatalf("missing autonomous session file flag in daemon calls: %#v", runner.cmds)
	}
	if firstSessionPath != secondSessionPath {
		t.Fatalf("session file path mismatch: first=%q second=%q", firstSessionPath, secondSessionPath)
	}
	if _, err := os.Stat(firstSessionPath); !os.IsNotExist(err) {
		t.Fatalf("session file %q still exists after daemon run, err=%v", firstSessionPath, err)
	}
}

func TestRunDaemonRejectsParallelismAboveOne(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "daemon", "--max-parallel-tasks", "2", "--poll-interval-seconds", "1", "--max-cycles", "1"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "--max-parallel-tasks=1") {
		t.Fatalf("stderr = %q, want concurrency validation", errOut.String())
	}
}

func TestRunPRCommandAcceptsPythonPRFlags(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "pr", "--pr", "72", "--from-review-comments"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "72", "--from-review-comments"})
}

func TestRunPRCommandForwardsConflictRecoveryOnly(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "pr", "--id", "72", "--conflict-recovery-only", "--sync-strategy", "merge"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "72", "--from-review-comments", "--sync-strategy", "merge", "--conflict-recovery-only"})
}

func TestRunPRCommandMapsCoreCompatibilityFlags(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{
		"run", "pr",
		"--id", "72",
		"--repo", "owner/repo",
		"--tracker", "jira",
		"--codehost", "github",
		"--runner", "opencode",
		"--agent", "review",
		"--model", "openai/gpt-4o",
		"--opencode-auto-approve",
		"--agent-timeout-seconds", "900",
		"--dry-run",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{
		runnerScript, "--pr", "72", "--from-review-comments",
		"--repo", "owner/repo",
		"--tracker", "jira",
		"--codehost", "github",
		"--runner", "opencode",
		"--agent", "review",
		"--model", "openai/gpt-4o",
		"--opencode-auto-approve",
		"--dry-run",
		"--agent-timeout-seconds", "900",
	})
}

func TestRunDaemonCommandSupportsAllState(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--state", "all", "--dry-run", "--poll-interval-seconds", "1"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--autonomous", "--state", "all", "--limit", "3", "--repo", "owner/repo", "--dry-run"})
}

func TestRunDaemonCommandMapsIssueFlowFlags(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{
		"run", "daemon",
		"--limit", "1",
		"--poll-interval-seconds", "0",
		"--include-empty",
		"--stop-on-error",
		"--fail-on-existing",
		"--force-issue-flow",
		"--no-skip-if-pr-exists",
		"--no-skip-if-branch-exists",
		"--force-reprocess",
		"--no-sync-reused-branch",
		"--sync-strategy", "merge",
		"--base", "current",
		"--dry-run",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{
		runnerScript, "--autonomous", "--state", "open", "--limit", "1",
		"--dry-run",
		"--base", "current",
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

func TestUnsupportedPythonFlagFailsFastWithActionableError(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "issue", "--id", "71", "--from-review-comments"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "unsupported flag --from-review-comments") || !strings.Contains(errOut.String(), "run pr") {
		t.Fatalf("stderr = %q, want actionable unsupported flag message", errOut.String())
	}
}

func TestRunIssueRequiresID(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "issue", "--dry-run"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
}

func TestRunIssueRejectsBatchFlags(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "issue", "--id", "71", "--limit", "1"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "unsupported flag --limit") {
		t.Fatalf("stderr = %q, want batch flag rejection", errOut.String())
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
	if !reflect.DeepEqual(stripFlagPair(runner.args, "--autonomous-session-file"), wantArgs) {
		t.Fatalf("runner args = %#v, want %#v", runner.args, wantArgs)
	}
}

func assertCommandContainsFlag(t *testing.T, args []string, flagName string) {
	t.Helper()
	if flagValue(args, flagName) == "" {
		t.Fatalf("runner args %v missing %s", args, flagName)
	}
}

func flagValue(args []string, flagName string) string {
	for i := 0; i < len(args)-1; i++ {
		if args[i] == flagName {
			return args[i+1]
		}
	}
	return ""
}

func stripFlagPair(args []string, flagName string) []string {
	cleaned := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		if args[i] == flagName {
			i++
			continue
		}
		cleaned = append(cleaned, args[i])
	}
	return cleaned
}
