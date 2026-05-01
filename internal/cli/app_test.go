package cli

import (
	"context"
	"encoding/json"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/workers"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

type recordingRunner struct {
	mu    sync.Mutex
	name  string
	args  []string
	calls int
	cmds  [][]string
}

func (r *recordingRunner) Run(_ context.Context, name string, args ...string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
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

type recordingDetachedStarter struct {
	req   DetachedRequest
	reqs  []DetachedRequest
	pid   int
	calls int
	err   error
}

func (r *recordingDetachedStarter) Start(req DetachedRequest) (DetachedProcess, error) {
	r.req = req
	r.reqs = append(r.reqs, req)
	r.calls++
	if r.err != nil {
		return DetachedProcess{}, r.err
	}
	pid := r.pid
	if pid == 0 {
		pid = 4242
	}
	return DetachedProcess{PID: pid}, nil
}

type recordingBatchClonePreparer struct {
	sourceDirs []string
	targetDirs []string
	err        error
}

func (r *recordingBatchClonePreparer) Prepare(sourceDir, targetDir string) (string, error) {
	r.sourceDirs = append(r.sourceDirs, sourceDir)
	r.targetDirs = append(r.targetDirs, targetDir)
	if r.err != nil {
		return "", r.err
	}
	if err := os.MkdirAll(targetDir, 0o755); err != nil {
		return "", err
	}
	return targetDir, nil
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

func TestStatusHelpUsesProviderNeutralTargetDescriptions(t *testing.T) {
	var out strings.Builder
	app := NewApp(&out, &out)

	if code := app.Run([]string{"status", "--help"}); code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	help := out.String()
	for _, want := range []string{"tracker issue number", "code host pull request number", "repository in owner/name format for the current runtime"} {
		if !strings.Contains(help, want) {
			t.Fatalf("status help missing %q\n%s", want, help)
		}
	}
	if strings.Contains(help, "GitHub issue number") || strings.Contains(help, "GitHub pull request number") {
		t.Fatalf("status help should not use GitHub-only target descriptions\n%s", help)
	}
}

func TestVerifyHelpUsesProviderNeutralFollowUpDescription(t *testing.T) {
	var out strings.Builder
	app := NewApp(&out, &out)

	if code := app.Run([]string{"verify", "--help"}); code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	help := out.String()
	if !strings.Contains(help, "create a tracker follow-up issue automatically when verification fails") {
		t.Fatalf("verify help missing provider-neutral follow-up wording\n%s", help)
	}
	if strings.Contains(help, "GitHub follow-up issue") {
		t.Fatalf("verify help should not advertise a GitHub-only follow-up issue\n%s", help)
	}
}

func TestVerifyCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--create-followup-issue", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--post-batch-verify", "--repo", "owner/repo", "--dry-run", "--create-followup-issue"})
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

func TestStatusAutonomousSessionCommandReadsSessionWithGoCore(t *testing.T) {
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(sessionPath, []byte("{\n  \"processed_issues\": {\"71\": {\"status\": \"ready-for-review\"}},\n  \"checkpoint\": {\n    \"phase\": \"completed\",\n    \"batch_index\": 2,\n    \"total_batches\": 2,\n    \"current\": \"Idle between autonomous runs\",\n    \"counts\": {\"processed\": 1, \"failures\": 0},\n    \"updated_at\": \"2026-04-28T12:10:00Z\",\n    \"verification\": {\"status\": \"passed\", \"summary\": \"passed (2/2 commands)\", \"follow_up_issue\": {\"status\": \"not-needed\"}}\n  }\n}\n"), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	var out strings.Builder
	runner := &recordingRunner{}
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"status", "--autonomous-session-file", sessionPath})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	for _, want := range []string{
		"Autonomous session status: completed",
		"Batch: 2/2",
		"Current: Idle between autonomous runs",
		"Counts: processed=1, failures=0",
		"Verification: passed (2/2 commands); follow-up=not-needed",
	} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("output missing %q\n%s", want, out.String())
		}
	}
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

func TestRunIssueDetachStartsBackgroundWorkerWithPredictablePaths(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	targetDir := t.TempDir()
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetDetachedStarter(starter)

	code := app.Run([]string{
		"run", "issue",
		"--id", "71",
		"--repo", "owner/repo",
		"--tracker", "github",
		"--codehost", "github",
		"--runner", "opencode",
		"--agent", "build",
		"--model", "openai/gpt-4o",
		"--dir", targetDir,
		"--detach",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 1 {
		t.Fatalf("starter calls = %d, want 1", starter.calls)
	}
	wantLogPath := filepath.Join(targetDir, ".orchestrator", "workers", "issue-71", "worker.log")
	wantStatePath := filepath.Join(targetDir, ".orchestrator", "workers", "issue-71", "worker.json")
	if starter.req.Name != "python3" {
		t.Fatalf("starter name = %q, want python3", starter.req.Name)
	}
	if !reflect.DeepEqual(starter.req.Args, []string{runnerScript, "--issue", "71", "--repo", "owner/repo", "--tracker", "github", "--codehost", "github", "--dir", targetDir, "--runner", "opencode", "--agent", "build", "--model", "openai/gpt-4o"}) {
		t.Fatalf("starter args = %#v", starter.req.Args)
	}
	if starter.req.LogPath != wantLogPath {
		t.Fatalf("starter log path = %q, want %q", starter.req.LogPath, wantLogPath)
	}
	if starter.req.Dir != targetDir {
		t.Fatalf("starter dir = %q, want %q", starter.req.Dir, targetDir)
	}
	if _, err := os.Stat(wantStatePath); err != nil {
		t.Fatalf("Stat(%q) error = %v", wantStatePath, err)
	}
	state, err := workers.ReadState(wantStatePath)
	if err != nil {
		t.Fatalf("workers.ReadState() error = %v", err)
	}
	if state.ClonePath != targetDir {
		t.Fatalf("clone path = %q, want %q", state.ClonePath, targetDir)
	}
	if state.Tracker != "github" || state.CodeHost != "github" || state.Runner != "opencode" || state.Agent != "build" || state.Model != "openai/gpt-4o" {
		t.Fatalf("worker state metadata = %#v", state)
	}
	if !strings.Contains(out.String(), "orchestrator status --worker issue-71") {
		t.Fatalf("stdout = %q, want detached status hint", out.String())
	}
}

func TestRunDaemonCommandForwardsPostBatchVerificationFlags(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{
		"run", "daemon",
		"--repo", "owner/repo",
		"--dry-run",
		"--max-cycles", "1",
		"--post-batch-verify",
		"--create-followup-issue",
	})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joined := strings.Join(runner.args, " ")
	if !strings.Contains(joined, "--post-batch-verify") {
		t.Fatalf("runner args = %q, want --post-batch-verify", joined)
	}
	if !strings.Contains(joined, "--create-followup-issue") {
		t.Fatalf("runner args = %q, want --create-followup-issue", joined)
	}
	if !strings.Contains(joined, "--autonomous-session-file") {
		t.Fatalf("runner args = %q, want daemon session file", joined)
	}
}

func TestRunDaemonDetachUsesStableSessionFile(t *testing.T) {
	starter := &recordingDetachedStarter{}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--post-batch-verify", "--create-followup-issue"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 1 {
		t.Fatalf("starter calls = %d, want 1", starter.calls)
	}
	wantSessionPath := filepath.Join(targetDir, ".orchestrator", "workers", "daemon", "session.json")
	if got := flagValue(starter.req.Args, "--autonomous-session-file"); got != wantSessionPath {
		t.Fatalf("session path = %q, want %q", got, wantSessionPath)
	}
	wantClonePath := filepath.Join(targetDir, ".orchestrator", "workers", "daemon", "repo")
	if starter.req.Dir != wantClonePath {
		t.Fatalf("starter dir = %q, want %q", starter.req.Dir, wantClonePath)
	}
	if len(cloner.targetDirs) != 1 || cloner.targetDirs[0] != wantClonePath {
		t.Fatalf("clone target dirs = %#v, want %q", cloner.targetDirs, wantClonePath)
	}
	joined := strings.Join(starter.req.Args, " ")
	if !strings.Contains(joined, "--post-batch-verify") {
		t.Fatalf("starter args = %q, want --post-batch-verify", joined)
	}
	if !strings.Contains(joined, "--create-followup-issue") {
		t.Fatalf("starter args = %q, want --create-followup-issue", joined)
	}
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

func TestRunBatchDryRunWiresPythonRunnerPerIssue(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "batch", "--ids", "71,72", "--repo", "owner/repo", "--dry-run", "--base", "current"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 2 {
		t.Fatalf("runner calls = %d, want 2", runner.calls)
	}
	if got := stripFlagPair(runner.cmds[0][1:], "--autonomous-session-file"); !reflect.DeepEqual(got, []string{runnerScript, "--issue", "71", "--repo", "owner/repo", "--dry-run", "--base", "current"}) {
		t.Fatalf("first runner args = %#v", runner.cmds[0][1:])
	}
	if got := stripFlagPair(runner.cmds[1][1:], "--autonomous-session-file"); !reflect.DeepEqual(got, []string{runnerScript, "--issue", "72", "--repo", "owner/repo", "--dry-run", "--base", "current"}) {
		t.Fatalf("second runner args = %#v", runner.cmds[1][1:])
	}
}

func TestRunBatchDetachStartsOneWorkerPerIssue(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)

	code := app.Run([]string{"run", "batch", "--ids", "71,72", "--repo", "owner/repo", "--dir", targetDir, "--detach"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 2 {
		t.Fatalf("starter calls = %d, want 2", starter.calls)
	}
	if len(cloner.targetDirs) != 2 {
		t.Fatalf("clone preparations = %d, want 2", len(cloner.targetDirs))
	}
	if len(starter.reqs) != 2 {
		t.Fatalf("starter requests = %d, want 2", len(starter.reqs))
	}
	if starter.reqs[0].Dir == starter.reqs[1].Dir {
		t.Fatalf("worker dirs should differ, got %q", starter.reqs[0].Dir)
	}
	for _, issueID := range []string{"71", "72"} {
		workerDir := filepath.Join(targetDir, ".orchestrator", "workers", "issue-"+issueID)
		statePath := filepath.Join(workerDir, "worker.json")
		if _, err := os.Stat(statePath); err != nil {
			t.Fatalf("Stat(%q) error = %v", statePath, err)
		}
		state, err := workers.ReadState(statePath)
		if err != nil {
			t.Fatalf("workers.ReadState(%q) error = %v", statePath, err)
		}
		if state.Mode != "run batch" {
			t.Fatalf("worker mode = %q, want run batch", state.Mode)
		}
		if state.TargetKind != "issue" || state.TargetID != issueID {
			t.Fatalf("worker target = %#v", state)
		}
		wantClonePath := filepath.Join(workerDir, "repo")
		if state.ClonePath != wantClonePath {
			t.Fatalf("clone path = %q, want %q", state.ClonePath, wantClonePath)
		}
		if state.WorkDir != wantClonePath {
			t.Fatalf("work dir = %q, want %q", state.WorkDir, wantClonePath)
		}
	}
	printed := out.String()
	for _, want := range []string{"started detached worker issue-71", "started detached worker issue-72"} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestRunBatchDetachPersistsBatchMetadataForChildWorkers(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)

	code := app.Run([]string{"run", "batch", "--ids", "71,72", "--repo", "owner/repo", "--dir", targetDir, "--detach"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}

	for _, issueID := range []string{"71", "72"} {
		statePath := filepath.Join(targetDir, ".orchestrator", "workers", "issue-"+issueID, "worker.json")
		state, err := workers.ReadState(statePath)
		if err != nil {
			t.Fatalf("workers.ReadState(%q) error = %v", statePath, err)
		}
		if state.Batch == nil {
			t.Fatalf("worker batch metadata = nil for %s", issueID)
		}
		if !reflect.DeepEqual(state.Batch.ChildIssueIDs, []string{"71", "72"}) {
			t.Fatalf("child issue ids = %#v, want [71 72]", state.Batch.ChildIssueIDs)
		}
		if len(state.Batch.ChildWorkers) != 2 {
			t.Fatalf("child workers len = %d, want 2", len(state.Batch.ChildWorkers))
		}
		for _, worker := range state.Batch.ChildWorkers {
			if worker.IssueID == "" || worker.WorkerName == "" || worker.LogPath == "" || worker.StatePath == "" || worker.ClonePath == "" || worker.StartedAt == "" || worker.StatusCommand == "" {
				t.Fatalf("child worker metadata incomplete: %#v", worker)
			}
			if !strings.Contains(worker.StatusCommand, "orchestrator status --issue ") {
				t.Fatalf("status command = %q, want issue status command", worker.StatusCommand)
			}
		}
	}
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

func TestRunDaemonRejectsNonPositiveParallelism(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "daemon", "--max-parallel-tasks", "0", "--poll-interval-seconds", "1", "--max-cycles", "1"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "--max-parallel-tasks > 0") {
		t.Fatalf("stderr = %q, want concurrency validation", errOut.String())
	}
}

func TestRunDaemonDetachStartsOneWorkerPerParallelTask(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--max-parallel-tasks", "2"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 2 {
		t.Fatalf("starter calls = %d, want 2", starter.calls)
	}
	if len(cloner.targetDirs) != 2 {
		t.Fatalf("clone preparations = %d, want 2", len(cloner.targetDirs))
	}
	if len(starter.reqs) != 2 {
		t.Fatalf("starter requests = %d, want 2", len(starter.reqs))
	}
	if starter.reqs[0].Dir == starter.reqs[1].Dir {
		t.Fatalf("daemon worker dirs should differ, got %q", starter.reqs[0].Dir)
	}
	for _, workerID := range []string{"1", "2"} {
		workerDir := filepath.Join(targetDir, ".orchestrator", "workers", "daemon-"+workerID)
		statePath := filepath.Join(workerDir, "worker.json")
		if _, err := os.Stat(statePath); err != nil {
			t.Fatalf("Stat(%q) error = %v", statePath, err)
		}
		state, err := workers.ReadState(statePath)
		if err != nil {
			t.Fatalf("workers.ReadState(%q) error = %v", statePath, err)
		}
		if state.Name != "daemon-"+workerID {
			t.Fatalf("worker name = %q, want daemon-%s", state.Name, workerID)
		}
		if state.TargetKind != "daemon" || state.TargetID != workerID {
			t.Fatalf("worker target = %#v", state)
		}
		wantClonePath := filepath.Join(workerDir, "repo")
		if state.ClonePath != wantClonePath {
			t.Fatalf("clone path = %q, want %q", state.ClonePath, wantClonePath)
		}
		if state.WorkDir != wantClonePath {
			t.Fatalf("work dir = %q, want %q", state.WorkDir, wantClonePath)
		}
		wantSessionPath := filepath.Join(workerDir, "session.json")
		if got := flagValue(state.Command[1:], "--autonomous-session-file"); got != wantSessionPath {
			t.Fatalf("session path = %q, want %q", got, wantSessionPath)
		}
	}
	printed := out.String()
	for _, want := range []string{"started detached worker daemon-1", "started detached worker daemon-2"} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestRunDaemonParallelUsesIsolatedClonesPerWorker(t *testing.T) {
	runner := &recordingRunner{}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetBatchClonePreparer(cloner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--limit", "3", "--max-parallel-tasks", "2", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 2 {
		t.Fatalf("runner calls = %d, want 2", runner.calls)
	}
	if len(cloner.targetDirs) != 2 {
		t.Fatalf("clone preparations = %d, want 2", len(cloner.targetDirs))
	}
	seenDirs := map[string]bool{}
	for _, cmd := range runner.cmds {
		if got := flagValue(cmd[1:], "--limit"); got != "3" {
			t.Fatalf("daemon worker limit = %q, want 3 in %#v", got, cmd)
		}
		workerDir := flagValue(cmd[1:], "--dir")
		if workerDir == "" {
			t.Fatalf("daemon worker missing --dir in %#v", cmd)
		}
		if workerDir == targetDir {
			t.Fatalf("daemon worker dir = %q, want isolated clone", workerDir)
		}
		seenDirs[workerDir] = true
		if sessionPath := flagValue(cmd[1:], "--autonomous-session-file"); sessionPath == "" {
			t.Fatalf("daemon worker missing session file in %#v", cmd)
		} else if _, err := os.Stat(sessionPath); !os.IsNotExist(err) {
			t.Fatalf("session file %q still exists after daemon run, err=%v", sessionPath, err)
		}
	}
	if len(seenDirs) != 2 {
		t.Fatalf("isolated worker dirs = %d, want 2 (%#v)", len(seenDirs), seenDirs)
	}
}

func TestRunDaemonParallelRunsVerificationOnceAfterWorkers(t *testing.T) {
	runner := &recordingRunner{}
	cloner := &recordingBatchClonePreparer{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetBatchClonePreparer(cloner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--max-parallel-tasks", "2", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run", "--post-batch-verify", "--create-followup-issue"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 3 {
		t.Fatalf("runner calls = %d, want 3", runner.calls)
	}
	verifyCalls := 0
	workerCalls := 0
	for _, cmd := range runner.cmds {
		joined := strings.Join(cmd, " ")
		if strings.Contains(joined, "--post-batch-verify") {
			verifyCalls++
			if flagValue(cmd[1:], "--limit") != "" {
				t.Fatalf("verification call should not include daemon batch limit: %#v", cmd)
			}
			continue
		}
		workerCalls++
		if got := flagValue(cmd[1:], "--limit"); got != "3" {
			t.Fatalf("daemon worker limit = %q, want 3 in %#v", got, cmd)
		}
	}
	if workerCalls != 2 {
		t.Fatalf("worker calls = %d, want 2", workerCalls)
	}
	if verifyCalls != 1 {
		t.Fatalf("verify calls = %d, want 1", verifyCalls)
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

func TestRunBatchRequiresDetachOrDryRun(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	if code := app.Run([]string{"run", "batch", "--ids", "71,72"}); code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "requires --detach") {
		t.Fatalf("stderr = %q, want detach guidance", errOut.String())
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

func TestStatusWorkerReportsDetachedMetadata(t *testing.T) {
	pythonDir := t.TempDir()
	pythonPath := filepath.Join(pythonDir, "python3")
	if err := os.WriteFile(pythonPath, []byte("#!/bin/sh\nprintf 'Target: issue #71\\nLatest state: waiting-for-ci\\nCurrent: waiting on 1 pending CI check(s)\\nNext: wait for ci\\nBlockers: pending ci\\nPR: #101\\nUpdated: 2026-04-28T12:05:00Z\\n'\n"), 0o755); err != nil {
		t.Fatalf("WriteFile(fake python) error = %v", err)
	}
	t.Setenv("PATH", pythonDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	targetDir := t.TempDir()
	workerRoot := filepath.Join(targetDir, ".orchestrator", "workers")
	workerDir := filepath.Join(workerRoot, "issue-71")
	if err := os.MkdirAll(workerDir, 0o755); err != nil {
		t.Fatalf("MkdirAll() error = %v", err)
	}
	statePath := filepath.Join(workerDir, "worker.json")
	logPath := filepath.Join(workerDir, "worker.log")
	state := detachedWorkerState{
		Name:       "issue-71",
		Mode:       "run issue",
		TargetKind: "issue",
		TargetID:   "71",
		Repo:       "owner/repo",
		Runner:     "opencode",
		Agent:      "build",
		Model:      "openai/gpt-4o",
		PID:        os.Getpid(),
		StartedAt:  "2026-04-28T12:00:00Z",
		LogPath:    logPath,
		StatePath:  statePath,
		ClonePath:  targetDir,
		WorkDir:    targetDir,
	}
	if err := os.WriteFile(logPath, []byte("line 1\nline 2\n"), 0o644); err != nil {
		t.Fatalf("WriteFile(log) error = %v", err)
	}
	if err := workers.WriteState(state); err != nil {
		t.Fatalf("workers.WriteState() error = %v", err)
	}

	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	code := app.Run([]string{"status", "--worker", "issue-71", "--worker-dir", workerRoot})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	printed := out.String()
	for _, want := range []string{
		"worker: issue-71",
		"target: issue #71",
		"process: running",
		"clone: " + targetDir,
		"agent: runner=opencode agent=build model=openai/gpt-4o",
		"log-progress: lines=2",
		"log-freshness: updated ",
		"log: " + logPath,
		"linked-latest-state: waiting-for-ci",
		"linked-current: waiting on 1 pending CI check(s)",
		"linked-next: wait for ci",
		"linked-blockers: pending ci",
		"linked-pr: #101",
		"linked-updated: 2026-04-28T12:05:00Z",
		"orchestrator status --issue 71 --repo owner/repo",
	} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestStatusWorkerShowsBatchSummary(t *testing.T) {
	pythonDir := t.TempDir()
	pythonPath := filepath.Join(pythonDir, "python3")
	raw := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *\"--issue 71\"*)\n" +
		"    printf 'Target: issue #71\\nLatest state: waiting-for-ci\\nCurrent: waiting on 1 pending CI check(s)\\nNext: wait for ci\\nBlockers: none\\nPR: #101\\nPR readiness: merge=clean, ci=pending, pending=1, failing=0; merge-result verification=passed (2/2 commands)\\nUpdated: 2026-04-28T12:05:00Z\\n'\n" +
		"    ;;\n" +
		"  *\"--issue 72\"*)\n" +
		"    printf 'Target: issue #72\\nLatest state: failed\\nCurrent: merge conflict while rebasing\\nNext: resolve merge conflicts\\nBlockers: merge conflict while rebasing\\nPR: #102\\nPR readiness: merge=conflicting, ci=failure, pending=0, failing=1; merge-result verification=failed (1/2 commands)\\nUpdated: 2026-04-28T12:06:00Z\\n'\n" +
		"    ;;\n" +
		"esac\n"
	if err := os.WriteFile(pythonPath, []byte(raw), 0o755); err != nil {
		t.Fatalf("WriteFile(fake python) error = %v", err)
	}
	t.Setenv("PATH", pythonDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	targetDir := t.TempDir()
	workerRoot := filepath.Join(targetDir, ".orchestrator", "workers")
	states := []detachedWorkerState{
		{
			Name:       "issue-71",
			Mode:       "run batch",
			TargetKind: "issue",
			TargetID:   "71",
			Repo:       "owner/repo",
			PID:        os.Getpid(),
			StartedAt:  "2026-04-28T12:00:00Z",
			LogPath:    filepath.Join(workerRoot, "issue-71", "worker.log"),
			StatePath:  filepath.Join(workerRoot, "issue-71", "worker.json"),
			ClonePath:  targetDir,
			WorkDir:    targetDir,
		},
		{
			Name:       "issue-72",
			Mode:       "run batch",
			TargetKind: "issue",
			TargetID:   "72",
			Repo:       "owner/repo",
			PID:        0,
			StartedAt:  "2026-04-28T12:01:00Z",
			LogPath:    filepath.Join(workerRoot, "issue-72", "worker.log"),
			StatePath:  filepath.Join(workerRoot, "issue-72", "worker.json"),
			ClonePath:  targetDir,
			WorkDir:    targetDir,
		},
	}
	states = withDetachedBatchMetadata(states, []int{71, 72})
	for _, state := range states {
		if err := os.MkdirAll(filepath.Dir(state.StatePath), 0o755); err != nil {
			t.Fatalf("MkdirAll() error = %v", err)
		}
		if err := os.WriteFile(state.LogPath, []byte("line 1\nline 2\n"), 0o644); err != nil {
			t.Fatalf("WriteFile(log) error = %v", err)
		}
	}
	if err := workers.WriteBatchStates(states); err != nil {
		t.Fatalf("workers.WriteBatchStates() error = %v", err)
	}

	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	code := app.Run([]string{"status", "--worker", "issue-71", "--worker-dir", workerRoot})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	printed := out.String()
	for _, want := range []string{
		"batch-child-workers: 2",
		"batch-active-workers: 1",
		"batch-done: issue #71: waiting-for-ci (#101)",
		"batch-current: issue #72: merge conflict while rebasing (#102)",
		"batch-next: issue #71: wait for ci; issue #72: resolve merge conflicts",
		"batch-linked-prs: issue #71 -> #101; issue #72 -> #102",
		"batch-conflicts: issue #72: merge conflict while rebasing",
		"merge-result verification=passed (2/2 commands)",
		"merge-result verification=failed (1/2 commands)",
		"batch-failures: issue #72: merge=conflicting, ci=failure, pending=0, failing=1; merge-result verification=failed (1/2 commands); issue #72: failed (#102)",
	} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestStatusWorkerJSONIncludesBatchSummary(t *testing.T) {
	pythonDir := t.TempDir()
	pythonPath := filepath.Join(pythonDir, "python3")
	raw := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *\"--issue 71\"*)\n" +
		"    printf 'Target: issue #71\\nLatest state: waiting-for-ci\\nCurrent: waiting on 1 pending CI check(s)\\nNext: wait for ci\\nBlockers: none\\nPR: #101\\nPR readiness: merge=clean, ci=pending, pending=1, failing=0; merge-result verification=passed (2/2 commands)\\nUpdated: 2026-04-28T12:05:00Z\\n'\n" +
		"    ;;\n" +
		"  *\"--issue 72\"*)\n" +
		"    printf 'Target: issue #72\\nLatest state: failed\\nCurrent: merge conflict while rebasing\\nNext: resolve merge conflicts\\nBlockers: merge conflict while rebasing\\nPR: #102\\nPR readiness: merge=conflicting, ci=failure, pending=0, failing=1; merge-result verification=failed (1/2 commands)\\nUpdated: 2026-04-28T12:06:00Z\\n'\n" +
		"    ;;\n" +
		"esac\n"
	if err := os.WriteFile(pythonPath, []byte(raw), 0o755); err != nil {
		t.Fatalf("WriteFile(fake python) error = %v", err)
	}
	t.Setenv("PATH", pythonDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	targetDir := t.TempDir()
	workerRoot := filepath.Join(targetDir, ".orchestrator", "workers")
	states := []detachedWorkerState{
		{
			Name:       "issue-71",
			Mode:       "run batch",
			TargetKind: "issue",
			TargetID:   "71",
			Repo:       "owner/repo",
			PID:        os.Getpid(),
			StartedAt:  "2026-04-28T12:00:00Z",
			LogPath:    filepath.Join(workerRoot, "issue-71", "worker.log"),
			StatePath:  filepath.Join(workerRoot, "issue-71", "worker.json"),
			ClonePath:  targetDir,
			WorkDir:    targetDir,
		},
		{
			Name:       "issue-72",
			Mode:       "run batch",
			TargetKind: "issue",
			TargetID:   "72",
			Repo:       "owner/repo",
			PID:        0,
			StartedAt:  "2026-04-28T12:01:00Z",
			LogPath:    filepath.Join(workerRoot, "issue-72", "worker.log"),
			StatePath:  filepath.Join(workerRoot, "issue-72", "worker.json"),
			ClonePath:  targetDir,
			WorkDir:    targetDir,
		},
	}
	states = withDetachedBatchMetadata(states, []int{71, 72})
	for _, state := range states {
		if err := os.MkdirAll(filepath.Dir(state.StatePath), 0o755); err != nil {
			t.Fatalf("MkdirAll() error = %v", err)
		}
		if err := os.WriteFile(state.LogPath, []byte("line 1\nline 2\n"), 0o644); err != nil {
			t.Fatalf("WriteFile(log) error = %v", err)
		}
	}
	if err := workers.WriteBatchStates(states); err != nil {
		t.Fatalf("workers.WriteBatchStates() error = %v", err)
	}

	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	code := app.Run([]string{"status", "--worker", "issue-71", "--worker-dir", workerRoot, "--json"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	var payload detachedWorkerReport
	if err := json.Unmarshal([]byte(out.String()), &payload); err != nil {
		t.Fatalf("json.Unmarshal() error = %v\n%s", err, out.String())
	}
	if payload.Worker.Name != "issue-71" {
		t.Fatalf("worker name = %q, want issue-71", payload.Worker.Name)
	}
	if payload.Batch == nil {
		t.Fatalf("batch = nil")
	}
	if len(payload.Batch.ChildWorkers) != 2 {
		t.Fatalf("child workers len = %d, want 2", len(payload.Batch.ChildWorkers))
	}
	if !reflect.DeepEqual(payload.Batch.LinkedPRs, []string{"issue #71 -> #101", "issue #72 -> #102"}) {
		t.Fatalf("linked PRs = %#v", payload.Batch.LinkedPRs)
	}
	if len(payload.Batch.Verification) != 2 {
		t.Fatalf("verification len = %d, want 2", len(payload.Batch.Verification))
	}
	if payload.Batch.ActiveWorkers != 1 {
		t.Fatalf("active workers = %d, want 1", payload.Batch.ActiveWorkers)
	}
	if len(payload.Batch.Failures) == 0 {
		t.Fatalf("failures = %#v, want non-empty", payload.Batch.Failures)
	}
}

func TestReadDetachedWorkerStateSupportsLegacyMetadataWithoutBatch(t *testing.T) {
	targetDir := t.TempDir()
	statePath := filepath.Join(targetDir, "worker.json")
	raw := []byte("{\n" +
		"  \"name\": \"issue-71\",\n" +
		"  \"mode\": \"run issue\",\n" +
		"  \"target_kind\": \"issue\",\n" +
		"  \"target_id\": \"71\",\n" +
		"  \"repo\": \"owner/repo\",\n" +
		"  \"command\": [\"python3\", \"script.py\"],\n" +
		"  \"started_at\": \"2026-04-28T12:00:00Z\",\n" +
		"  \"pid\": 4242,\n" +
		"  \"log_path\": \"/tmp/worker.log\",\n" +
		"  \"state_path\": \"/tmp/worker.json\",\n" +
		"  \"work_dir\": \"/repo\"\n" +
		"}\n")
	if err := os.WriteFile(statePath, raw, 0o644); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	state, err := workers.ReadState(statePath)
	if err != nil {
		t.Fatalf("workers.ReadState() error = %v", err)
	}
	if state.Name != "issue-71" || state.TargetID != "71" {
		t.Fatalf("state = %#v", state)
	}
	if state.Batch != nil {
		t.Fatalf("legacy state batch = %#v, want nil", state.Batch)
	}
}

func TestStatusWorkersJSONListsRegistryEntries(t *testing.T) {
	targetDir := t.TempDir()
	workerRoot := filepath.Join(targetDir, ".orchestrator", "workers")
	if err := os.MkdirAll(filepath.Join(workerRoot, "daemon"), 0o755); err != nil {
		t.Fatalf("MkdirAll() error = %v", err)
	}
	logPath := filepath.Join(workerRoot, "daemon", "worker.log")
	statePath := filepath.Join(workerRoot, "daemon", "worker.json")
	sessionPath := filepath.Join(workerRoot, "daemon", "session.json")
	if err := os.WriteFile(logPath, []byte("batch start\nbatch done\n"), 0o644); err != nil {
		t.Fatalf("WriteFile(log) error = %v", err)
	}
	if err := os.WriteFile(sessionPath, []byte("{\n  \"processed_issues\": {\"71\": {\"status\": \"ready-for-review\"}},\n  \"checkpoint\": {\n    \"phase\": \"running\",\n    \"current\": \"issue #71\",\n    \"next\": [\"issue #72\"],\n    \"counts\": {\"processed\": 1, \"failures\": 0},\n    \"updated_at\": \"2026-04-28T12:10:00Z\"\n  }\n}\n"), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	if err := workers.WriteState(detachedWorkerState{
		Name:        "daemon",
		Mode:        "run daemon",
		TargetKind:  "daemon",
		Repo:        "owner/repo",
		PID:         0,
		StartedAt:   "2026-04-28T12:00:00Z",
		LogPath:     logPath,
		SessionPath: sessionPath,
		StatePath:   statePath,
		ClonePath:   targetDir,
		WorkDir:     targetDir,
	}); err != nil {
		t.Fatalf("workers.WriteState() error = %v", err)
	}

	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	code := app.Run([]string{"status", "--workers", "--worker-dir", workerRoot, "--json"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	var payload struct {
		Workers []detachedWorkerReport `json:"workers"`
	}
	if err := json.Unmarshal([]byte(out.String()), &payload); err != nil {
		t.Fatalf("json.Unmarshal() error = %v\n%s", err, out.String())
	}
	if len(payload.Workers) != 1 {
		t.Fatalf("workers len = %d, want 1", len(payload.Workers))
	}
	worker := payload.Workers[0]
	if worker.Worker.Name != "daemon" {
		t.Fatalf("worker name = %q, want daemon", worker.Worker.Name)
	}
	if worker.Worker.Repo != "owner/repo" {
		t.Fatalf("worker repo = %q, want owner/repo", worker.Worker.Repo)
	}
	if worker.Worker.ClonePath != targetDir {
		t.Fatalf("worker clone path = %q, want %q", worker.Worker.ClonePath, targetDir)
	}
	if worker.ProcessStatus != "exited" {
		t.Fatalf("process status = %q, want exited", worker.ProcessStatus)
	}
	if worker.Log.Lines != 2 {
		t.Fatalf("log lines = %d, want 2", worker.Log.Lines)
	}
	if worker.Log.UpdatedAt == "" {
		t.Fatalf("log updated_at = %q, want non-empty", worker.Log.UpdatedAt)
	}
	if worker.Session == nil || worker.Session.Current != "issue #71" || worker.Session.Processed != 1 {
		t.Fatalf("session = %#v", worker.Session)
	}
	if worker.Session.ActiveWorkers != 0 {
		t.Fatalf("session active workers = %d, want 0", worker.Session.ActiveWorkers)
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
