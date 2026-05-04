package cli

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/agentexec"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/githublifecycle"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/workers"
)

type recordingRunner struct {
	mu    sync.Mutex
	name  string
	args  []string
	calls int
	cmds  [][]string
	err   error
}

func (r *recordingRunner) Run(_ context.Context, name string, args ...string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.name = name
	r.args = append([]string(nil), args...)
	r.calls++
	r.cmds = append(r.cmds, append([]string{name}, args...))
	return r.err
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

type fakeShellExecutor struct {
	results []shellExecutionResult
	err     error
	cmds    []string
	cwds    []string
}

type fakeIssueAgentRunner struct {
	result    *agentexec.Result
	err       error
	requests  []agentexec.Request
	labels    []string
	callCount int
}

type fakeDaemonLifecycle struct {
	issues          []githublifecycle.Issue
	issue           githublifecycle.Issue
	issuesByNumber  map[int]githublifecycle.Issue
	defaultBranch   string
	linkedPR        *githublifecycle.PullRequest
	pullRequest     githublifecycle.PullRequest
	pullRequests    []githublifecycle.PullRequest
	reviewThreads   []githublifecycle.PullRequestReviewThread
	reviewThreadSeq [][]githublifecycle.PullRequestReviewThread
	conversation    []githublifecycle.PullRequestConversationComment
	conversationSeq [][]githublifecycle.PullRequestConversationComment
	commentsByIssue map[int][]githublifecycle.IssueComment
	commentBodies   map[int][]string
	prCommentBodies map[int][]string
	createdIssues   []githublifecycle.CreateIssueRequest
	createIssue     githublifecycle.Issue
	createdPRs      []githublifecycle.CreatePullRequestRequest
	createPRURL     string
	listErr         error
	commentErr      error
	createErr       error
	listIssueLimits []int
}

func (f *fakeDaemonLifecycle) FetchIssue(_ context.Context, _ string, number int) (githublifecycle.Issue, error) {
	if f.listErr != nil {
		return githublifecycle.Issue{}, f.listErr
	}
	if issue, ok := f.issuesByNumber[number]; ok {
		return issue, nil
	}
	if f.issue.Number == 0 {
		f.issue = githublifecycle.Issue{Number: number, Title: "Fix runner", Body: "Body", URL: "https://github.com/owner/repo/issues/" + strconv.Itoa(number), Tracker: githublifecycle.TrackerGitHub}
	}
	return f.issue, nil
}

func (f *fakeDaemonLifecycle) DefaultBranch(_ context.Context, _ string) (string, error) {
	if f.listErr != nil {
		return "", f.listErr
	}
	if f.defaultBranch == "" {
		return "main", nil
	}
	return f.defaultBranch, nil
}

func (f *fakeDaemonLifecycle) FindOpenPullRequestForIssue(_ context.Context, _ string, _ githublifecycle.Issue) (*githublifecycle.PullRequest, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return f.linkedPR, nil
}

func (f *fakeDaemonLifecycle) ListIssues(_ context.Context, _ string, _ string, limit int) ([]githublifecycle.Issue, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	f.listIssueLimits = append(f.listIssueLimits, limit)
	issues := f.issues
	if limit > 0 && limit < len(issues) {
		issues = issues[:limit]
	}
	return append([]githublifecycle.Issue(nil), issues...), nil
}

func (f *fakeDaemonLifecycle) ListIssueComments(_ context.Context, _ string, number int) ([]githublifecycle.IssueComment, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return append([]githublifecycle.IssueComment(nil), f.commentsByIssue[number]...), nil
}

func (f *fakeDaemonLifecycle) CommentOnIssue(_ context.Context, _ string, number int, body string) error {
	if f.commentErr != nil {
		return f.commentErr
	}
	if f.commentBodies == nil {
		f.commentBodies = map[int][]string{}
	}
	if f.commentsByIssue == nil {
		f.commentsByIssue = map[int][]githublifecycle.IssueComment{}
	}
	f.commentBodies[number] = append(f.commentBodies[number], body)
	comments := f.commentsByIssue[number]
	comments = append(comments, githublifecycle.IssueComment{ID: int64(len(comments) + 1), Body: body, CreatedAt: "2026-05-01T12:00:00Z"})
	f.commentsByIssue[number] = comments
	return nil
}

func (f *fakeDaemonLifecycle) CreateIssue(_ context.Context, req githublifecycle.CreateIssueRequest) (githublifecycle.Issue, error) {
	f.createdIssues = append(f.createdIssues, req)
	if f.createErr != nil {
		return githublifecycle.Issue{}, f.createErr
	}
	if f.createIssue.Number == 0 {
		number := 163 + len(f.createdIssues)
		return githublifecycle.Issue{Number: number, URL: "https://github.com/owner/repo/issues/" + strconv.Itoa(number)}, nil
	}
	return f.createIssue, nil
}

func (f *fakeDaemonLifecycle) CreatePullRequest(_ context.Context, req githublifecycle.CreatePullRequestRequest) (string, error) {
	f.createdPRs = append(f.createdPRs, req)
	if f.createErr != nil {
		return "", f.createErr
	}
	if f.createPRURL == "" {
		f.createPRURL = "https://github.com/owner/repo/pull/101"
	}
	return f.createPRURL, nil
}

func (f *fakeDaemonLifecycle) FetchPullRequest(_ context.Context, _ string, number int) (githublifecycle.PullRequest, error) {
	if f.listErr != nil {
		return githublifecycle.PullRequest{}, f.listErr
	}
	if len(f.pullRequests) > 0 {
		pr := f.pullRequests[0]
		f.pullRequests = f.pullRequests[1:]
		return pr, nil
	}
	if f.pullRequest.Number == 0 {
		f.pullRequest = githublifecycle.PullRequest{Number: number, Title: "Fix review feedback", URL: "https://github.com/owner/repo/pull/" + strconv.Itoa(number), HeadRefName: "feature/pr-" + strconv.Itoa(number), BaseRefName: "main"}
	}
	return f.pullRequest, nil
}

func (f *fakeDaemonLifecycle) CommentOnPullRequest(_ context.Context, _ string, number int, body string) error {
	if f.commentErr != nil {
		return f.commentErr
	}
	if f.prCommentBodies == nil {
		f.prCommentBodies = map[int][]string{}
	}
	f.prCommentBodies[number] = append(f.prCommentBodies[number], body)
	return nil
}

func (f *fakeDaemonLifecycle) ReviewThreadsForPullRequest(_ context.Context, _ string, _ int) ([]githublifecycle.PullRequestReviewThread, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	if len(f.reviewThreadSeq) > 0 {
		threads := f.reviewThreadSeq[0]
		f.reviewThreadSeq = f.reviewThreadSeq[1:]
		return append([]githublifecycle.PullRequestReviewThread(nil), threads...), nil
	}
	return append([]githublifecycle.PullRequestReviewThread(nil), f.reviewThreads...), nil
}

func (f *fakeDaemonLifecycle) ConversationCommentsForPullRequest(_ context.Context, _ string, _ int) ([]githublifecycle.PullRequestConversationComment, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	if len(f.conversationSeq) > 0 {
		comments := f.conversationSeq[0]
		f.conversationSeq = f.conversationSeq[1:]
		return append([]githublifecycle.PullRequestConversationComment(nil), comments...), nil
	}
	return append([]githublifecycle.PullRequestConversationComment(nil), f.conversation...), nil
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
	gitDir := filepath.Join(targetDir, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		return "", err
	}
	config := "[remote \"origin\"]\n\turl = https://github.com/owner/repo.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
	if err := os.WriteFile(filepath.Join(gitDir, "config"), []byte(config), 0o644); err != nil {
		return "", err
	}
	return targetDir, nil
}

func (f *fakeShellExecutor) Run(_ context.Context, cwd, command string) (shellExecutionResult, error) {
	f.cwds = append(f.cwds, cwd)
	f.cmds = append(f.cmds, command)
	if f.err != nil {
		return shellExecutionResult{}, f.err
	}
	if len(f.results) == 0 {
		return shellExecutionResult{}, nil
	}
	result := f.results[0]
	f.results = f.results[1:]
	return result, nil
}

func (f *fakeIssueAgentRunner) Run(_ context.Context, itemLabel string, req agentexec.Request) (*agentexec.Result, error) {
	f.callCount++
	f.labels = append(f.labels, itemLabel)
	f.requests = append(f.requests, req)
	if f.err != nil {
		return nil, f.err
	}
	if f.result == nil {
		return &agentexec.Result{}, nil
	}
	return f.result, nil
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

func TestDoctorCommandRunsGoNativeChecks(t *testing.T) {
	runner := &recordingRunner{err: os.ErrNotExist}
	var out strings.Builder
	var errOut strings.Builder
	app := NewApp(&out, &errOut)
	app.SetRunner(runner)

	code := app.Run([]string{"doctor", "--repo", "owner/repo", "--dry-run"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls == 0 {
		t.Fatal("expected at least one go-native check command")
	}
	if strings.Contains(out.String(), "python") {
		t.Fatalf("doctor output should not rely on python runner: %q", out.String())
	}
}

func TestAutoDoctorCommandIncludesNextStepGuidance(t *testing.T) {
	runner := &recordingRunner{err: os.ErrNotExist}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"autodoctor", "--repo", "owner/repo"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if !strings.Contains(out.String(), "Autodoctor next steps:") {
		t.Fatalf("stdout = %q, want next-step guidance", out.String())
	}
}

func TestDoctorSmokeCheckValidatesDetachedWorkerMetadata(t *testing.T) {
	targetDir := t.TempDir()
	if err := exec.Command("git", "init", "-q", targetDir).Run(); err != nil {
		t.Fatalf("git init error = %v", err)
	}

	runner := &recordingRunner{}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"doctor", "--repo", "owner/repo", "--dir", targetDir, "--doctor-smoke-check"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0\noutput:\n%s", code, out.String())
	}
	if !strings.Contains(out.String(), "[PASS] Runner smoke check: CLI invocation enabled; detached worker metadata smoke passed") {
		t.Fatalf("stdout = %q, want detached worker smoke pass detail", out.String())
	}
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

func TestVerifyCommandRunsGoVerificationPath(t *testing.T) {
	repoDir := t.TempDir()
	if err := os.Mkdir(filepath.Join(repoDir, "tests"), 0o755); err != nil {
		t.Fatalf("Mkdir(tests) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "go.mod"), []byte("module example.com/test\n"), 0o644); err != nil {
		t.Fatalf("WriteFile(go.mod) error = %v", err)
	}
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{{Stdout: "python ok\n", ExitCode: 0}, {Stdout: "go ok\n", ExitCode: 0}}}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--tracker", "github", "--dir", repoDir})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !reflect.DeepEqual(shell.cmds, []string{"python3 -m unittest discover -s tests -q", "go test ./..."}) {
		t.Fatalf("shell cmds = %#v", shell.cmds)
	}
}

func TestVerifyCommandUsesConfiguredWorkflowCommands(t *testing.T) {
	repoDir := t.TempDir()
	projectConfigPath := filepath.Join(repoDir, "project-config.json")
	if err := os.WriteFile(projectConfigPath, []byte(`{"workflow":{"commands":{"test":"make test","build":"make build"}}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(project-config) error = %v", err)
	}
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{{ExitCode: 0}, {ExitCode: 0}}}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)

	code := app.Run([]string{"verify", "--dir", repoDir, "--project-config", projectConfigPath})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if !reflect.DeepEqual(shell.cmds, []string{"make test", "make build"}) {
		t.Fatalf("shell cmds = %#v", shell.cmds)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
}

func TestVerifyCommandCreatesFollowUpIssueFromGoRuntime(t *testing.T) {
	repoDir := t.TempDir()
	if err := os.Mkdir(filepath.Join(repoDir, "tests"), 0o755); err != nil {
		t.Fatalf("Mkdir(tests) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "go.mod"), []byte("module example.com/test\n"), 0o644); err != nil {
		t.Fatalf("WriteFile(go.mod) error = %v", err)
	}
	var out strings.Builder
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{{Stdout: "FAILED (failures=1)\nOK\n", ExitCode: 0}, {Stderr: "go test failed\n", ExitCode: 1}}}
	daemon := &fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{}}
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetDaemonLifecycle(daemon)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--tracker", "github", "--dir", repoDir, "--create-followup-issue"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if len(daemon.createdIssues) != 1 {
		t.Fatalf("created issues = %#v", daemon.createdIssues)
	}
	if !strings.Contains(daemon.createdIssues[0].Body, "go test failed") {
		t.Fatalf("issue body = %q", daemon.createdIssues[0].Body)
	}
	if !strings.Contains(out.String(), "follow-up issue #164 created") {
		t.Fatalf("stdout = %q, want created follow-up summary", out.String())
	}
}

func TestVerifyCommandCreatesOneFollowUpPerFailedCheck(t *testing.T) {
	repoDir := t.TempDir()
	projectConfigPath := filepath.Join(repoDir, "project-config.json")
	if err := os.WriteFile(projectConfigPath, []byte(`{"workflow":{"commands":{"test":"make test","lint":"make lint","build":"make build"}}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(project-config) error = %v", err)
	}
	sessionPath := filepath.Join(repoDir, "session.json")
	if err := os.WriteFile(sessionPath, []byte(`{"processed_issues":{},"checkpoint":{"batch_index":2,"total_batches":3}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	var out strings.Builder
	shell := &fakeShellExecutor{results: []shellExecutionResult{{Stderr: "test failure", ExitCode: 1}, {Stderr: "lint failure", ExitCode: 1}, {ExitCode: 0}}}
	daemon := &fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{}}
	app := NewApp(&out, &strings.Builder{})
	app.SetShellExecutor(shell)
	app.SetDaemonLifecycle(daemon)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--tracker", "github", "--dir", repoDir, "--project-config", projectConfigPath, "--autonomous-session-file", sessionPath, "--create-followup-issue"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if len(daemon.createdIssues) != 2 {
		t.Fatalf("created issues = %#v", daemon.createdIssues)
	}
	if daemon.createdIssues[0].Title != "Post-batch verification failed: test" || daemon.createdIssues[1].Title != "Post-batch verification failed: lint" {
		t.Fatalf("created issue titles = %#v", daemon.createdIssues)
	}
	for _, want := range []string{"batch: 2/3", "session: `" + sessionPath + "`", "follow-up issues #164, #165 created"} {
		if !strings.Contains(daemon.createdIssues[0].Body+out.String(), want) {
			t.Fatalf("missing %q\nbody=%s\nout=%s", want, daemon.createdIssues[0].Body, out.String())
		}
	}
	state, err := orchestration.LoadState(sessionPath)
	if err != nil {
		t.Fatalf("LoadState() error = %v", err)
	}
	if got := len(state.Checkpoint.Verification.FollowUpIssues); got != 2 {
		t.Fatalf("persisted follow-up issues = %d, want 2", got)
	}
}

func TestVerifyCommandSuppressesDuplicatePostBatchFollowUp(t *testing.T) {
	repoDir := t.TempDir()
	projectConfigPath := filepath.Join(repoDir, "project-config.json")
	if err := os.WriteFile(projectConfigPath, []byte(`{"workflow":{"commands":{"test":"make test"}}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(project-config) error = %v", err)
	}
	shell := &fakeShellExecutor{results: []shellExecutionResult{{Stderr: "test failure", ExitCode: 1}}}
	daemon := &fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{{Number: 222, Title: "Post-batch verification failed: test", Body: "existing", URL: "https://github.com/owner/repo/issues/222", State: "OPEN"}},
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetShellExecutor(shell)
	app.SetDaemonLifecycle(daemon)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--tracker", "github", "--dir", repoDir, "--project-config", projectConfigPath, "--create-followup-issue"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if len(daemon.createdIssues) != 0 {
		t.Fatalf("created issues = %#v, want none", daemon.createdIssues)
	}
}

func TestVerifyCommandPassingChecksDoNotCreateFollowUp(t *testing.T) {
	repoDir := t.TempDir()
	projectConfigPath := filepath.Join(repoDir, "project-config.json")
	if err := os.WriteFile(projectConfigPath, []byte(`{"workflow":{"commands":{"test":"make test","build":"make build"}}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(project-config) error = %v", err)
	}
	shell := &fakeShellExecutor{results: []shellExecutionResult{{ExitCode: 0}, {ExitCode: 0}}}
	daemon := &fakeDaemonLifecycle{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetShellExecutor(shell)
	app.SetDaemonLifecycle(daemon)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--tracker", "github", "--dir", repoDir, "--project-config", projectConfigPath, "--create-followup-issue"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if len(daemon.createdIssues) != 0 {
		t.Fatalf("created issues = %#v, want none", daemon.createdIssues)
	}
}

func TestVerifyCommandFollowUpUsesMissingLogsFallback(t *testing.T) {
	repoDir := t.TempDir()
	projectConfigPath := filepath.Join(repoDir, "project-config.json")
	if err := os.WriteFile(projectConfigPath, []byte(`{"workflow":{"commands":{"test":"make test"}}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(project-config) error = %v", err)
	}
	shell := &fakeShellExecutor{results: []shellExecutionResult{{ExitCode: 1}}}
	daemon := &fakeDaemonLifecycle{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetShellExecutor(shell)
	app.SetDaemonLifecycle(daemon)

	code := app.Run([]string{"verify", "--repo", "owner/repo", "--tracker", "github", "--dir", repoDir, "--project-config", projectConfigPath, "--create-followup-issue"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if len(daemon.createdIssues) != 1 {
		t.Fatalf("created issues = %#v", daemon.createdIssues)
	}
	if !strings.Contains(daemon.createdIssues[0].Body, "no log excerpt was captured") {
		t.Fatalf("issue body = %q", daemon.createdIssues[0].Body)
	}
}

func TestPersistVerificationToSessionPreservesCheckpointShape(t *testing.T) {
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	raw := []byte("{\n  \"processed_issues\": {\"71\": {\"status\": \"ready-for-review\"}},\n  \"checkpoint\": {\n    \"phase\": \"completed\",\n    \"current\": \"Idle between autonomous runs\",\n    \"counts\": {\"processed\": 1, \"failures\": 0},\n    \"updated_at\": \"2026-04-28T12:10:00Z\"\n  }\n}\n")
	if err := os.WriteFile(sessionPath, raw, 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}

	err := persistVerificationToSession(sessionPath, orchestration.VerificationResult{
		Status:     orchestration.StatusFailed,
		Summary:    "failed (1/2 passed; failed: go-test)",
		NextAction: "create_follow_up_issue_and_fix_regression",
		Commands: []orchestration.VerificationCommandResult{{
			Name:          "go-test",
			Command:       "go test ./...",
			Status:        orchestration.StatusFailed,
			ExitCode:      intPtr(1),
			StderrExcerpt: "go test failed",
		}},
		FollowUpIssue: &orchestration.VerificationFollowUpIssue{Status: "recommended"},
	})
	if err != nil {
		t.Fatalf("persistVerificationToSession() error = %v", err)
	}

	state, err := orchestration.LoadState(sessionPath)
	if err != nil {
		t.Fatalf("LoadState() error = %v", err)
	}
	if state.Checkpoint == nil || state.Checkpoint.Verification == nil {
		t.Fatal("verification checkpoint = nil")
	}
	if got := state.Checkpoint.Verification.Commands[0].ExitCode; got == nil || *got != 1 {
		t.Fatalf("exit code = %#v, want 1", got)
	}
	if got := state.Checkpoint.Verification.FollowUpIssue.Status; got != "recommended" {
		t.Fatalf("follow-up status = %q, want recommended", got)
	}
}

func TestStatusIssueCommandRunsGoNativePath(t *testing.T) {
	var out strings.Builder
	runner := &recordingRunner{}
	daemon := &fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{}}
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetIssueLifecycle(daemon)

	code := app.Run([]string{"status", "--issue", "71", "--repo", "owner/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(out.String(), "Target: issue #71") {
		t.Fatalf("stdout = %q, want issue status target", out.String())
	}
}

func TestStatusPRCommandRunsGoNativePath(t *testing.T) {
	var out strings.Builder
	runner := &recordingRunner{}
	daemon := &fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{}}
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetPRLifecycle(daemon)

	code := app.Run([]string{"status", "--pr", "72", "--repo", "owner/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(out.String(), "Target: pr #72") {
		t.Fatalf("stdout = %q, want pr status target", out.String())
	}
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
	app.SetDaemonLifecycle(nil)

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

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--post-batch-verify", "--create-followup-issue", "--allow-live-side-effects"})
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
	if !strings.Contains(joined, "--allow-live-side-effects") {
		t.Fatalf("starter args = %q, want --allow-live-side-effects", joined)
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

func TestRunIssueUsesGoNativeHappyPath(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 1},
		{ExitCode: 2},
		{},
		{},
		{Stdout: ""},
		{Stdout: "M internal/cli/command_run.go\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
		{Stdout: "new.txt\n"},
		{},
		{},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
		createPRURL:   "https://github.com/owner/repo/pull/101",
	}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{Stats: agentexec.Stats{ElapsedSeconds: 12}}}
	var out strings.Builder
	var errOut strings.Builder
	app := NewApp(&out, &errOut)
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0; stderr=%q", code, errOut.String())
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 1 {
		t.Fatalf("agent call count = %d, want 1", agent.callCount)
	}
	if len(agent.requests) != 1 {
		t.Fatalf("agent requests = %d, want 1", len(agent.requests))
	}
	if agent.requests[0].Runner != nativeIssueDefaultRunner || agent.requests[0].Agent != nativeIssueDefaultAgent || agent.requests[0].Model != nativeIssueDefaultModel {
		t.Fatalf("agent request = %#v", agent.requests[0])
	}
	if agent.requests[0].Cwd != "/repo" {
		t.Fatalf("agent cwd = %q, want /repo", agent.requests[0].Cwd)
	}
	if len(lifecycle.createdPRs) != 1 {
		t.Fatalf("created PRs = %d, want 1", len(lifecycle.createdPRs))
	}
	createdPR := lifecycle.createdPRs[0]
	if createdPR.BaseBranch != "main" || createdPR.HeadBranch != "issue-fix/71-fix-runner" || createdPR.IssueRef != "#71" {
		t.Fatalf("created PR = %#v", createdPR)
	}
	if len(lifecycle.commentBodies[71]) != 2 {
		t.Fatalf("comment bodies = %d, want 2", len(lifecycle.commentBodies[71]))
	}
	firstState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(first) error = %v", err)
	}
	if firstState.Status != orchestration.StatusInProgress || firstState.Stage != "agent_run" {
		t.Fatalf("first state = %#v", firstState)
	}
	finalState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][1])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(final) error = %v", err)
	}
	if finalState.Status != orchestration.StatusReadyForReview || finalState.Stage != "pr_ready" {
		t.Fatalf("final state = %#v", finalState)
	}
	if finalState.PR == nil || *finalState.PR != 101 {
		t.Fatalf("final PR = %#v, want 101", finalState.PR)
	}
	if !strings.Contains(out.String(), "Prepared issue #71 for review") {
		t.Fatalf("stdout = %q, want success summary", out.String())
	}
	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
		gitCommand("checkout", "main"),
		gitCommand("checkout", "-b", "issue-fix/71-fix-runner"),
		gitCommand("ls-files", "--others", "--exclude-standard"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("add", "-u"),
		gitCommand("ls-files", "--others", "--exclude-standard"),
		gitCommand("add", "--", "new.txt"),
		gitCommand("commit", "-m", "Fix issue #71: Fix runner"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("push", "-u", "origin", "issue-fix/71-fix-runner"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
}

func TestRunIssueNoopWithExplanationPersistsStructuredReason(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 1},
		{ExitCode: 2},
		{},
		{},
		{Stdout: ""},
		{Stdout: "\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
	}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{Output: strings.Join([]string{
		"Done",
		orchestration.NoOpResultMarker,
		"```json",
		`{"explanation":"Feature appears already implemented; verified by inspecting issue handler and existing tests","next_action":"Run go test ./... to verify the current behavior"}`,
		"```",
	}, "\n")}}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if len(lifecycle.commentBodies[71]) != 3 {
		t.Fatalf("comment bodies = %d, want 3 (state + explanation + final state)", len(lifecycle.commentBodies[71]))
	}
	if !strings.Contains(lifecycle.commentBodies[71][1], "Automation finished without code changes") {
		t.Fatalf("noop comment = %q, want explanation heading", lifecycle.commentBodies[71][1])
	}
	finalState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][2])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(final) error = %v", err)
	}
	if finalState.Status != orchestration.StatusBlocked || finalState.NextAction != "inspect_noop_result" {
		t.Fatalf("final state = %#v, want blocked inspect_noop_result", finalState)
	}
	if !strings.Contains(finalState.Error, "Feature appears already implemented") {
		t.Fatalf("final error = %q, want persisted explanation", finalState.Error)
	}
	if !strings.Contains(out.String(), "recorded no-op explanation") {
		t.Fatalf("stdout = %q, want no-op summary", out.String())
	}
}

func TestRunIssueNoopWithoutExplanationFailsAsAgentQualityIssue(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 1},
		{ExitCode: 2},
		{},
		{},
		{Stdout: ""},
		{Stdout: "\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
	}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{Output: "Done, no edits required."}}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if len(lifecycle.commentBodies[71]) != 2 {
		t.Fatalf("comment bodies = %d, want 2 (state + failed state)", len(lifecycle.commentBodies[71]))
	}
	finalState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][1])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(final) error = %v", err)
	}
	if finalState.Status != orchestration.StatusFailed || finalState.NextAction != "inspect_noop_explanation" {
		t.Fatalf("final state = %#v, want failed inspect_noop_explanation", finalState)
	}
	if strings.Contains(strings.ToLower(finalState.Status), "waiting-for-author") {
		t.Fatalf("final state = %#v, want hard failure not waiting-for-author", finalState)
	}
	if !strings.Contains(finalState.Error, "did not provide a no-op explanation") {
		t.Fatalf("final error = %q, want explicit no-op explanation failure", finalState.Error)
	}
	if !strings.Contains(errOut.String(), orchestration.NoOpResultMarker) {
		t.Fatalf("stderr = %q, want missing marker guidance", errOut.String())
	}
}

func TestRunIssueNativePersistsAutonomousSessionCheckpoint(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 1},
		{ExitCode: 2},
		{},
		{},
		{Stdout: ""},
		{Stdout: "M internal/cli/command_run.go\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
		{Stdout: "new.txt\n"},
		{},
		{},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
		createPRURL:   "https://github.com/owner/repo/pull/101",
	}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{Stats: agentexec.Stats{ElapsedSeconds: 12}}}
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--autonomous-session-file", sessionPath})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	state, err := orchestration.LoadState(sessionPath)
	if err != nil {
		t.Fatalf("LoadState() error = %v", err)
	}
	if state.Checkpoint == nil {
		t.Fatal("checkpoint = nil")
	}
	if got := state.Checkpoint.Phase; got != "completed" {
		t.Fatalf("checkpoint phase = %q, want completed", got)
	}
	if got := state.Checkpoint.Current; got != "Idle between autonomous runs" {
		t.Fatalf("checkpoint current = %q", got)
	}
	if got := state.Checkpoint.Counts.Processed; got != 1 {
		t.Fatalf("processed count = %d, want 1", got)
	}
	if got := state.Checkpoint.Counts.Failures; got != 0 {
		t.Fatalf("failure count = %d, want 0", got)
	}
	if !reflect.DeepEqual(state.Checkpoint.Done, []string{"issue #71 (ready-for-review)"}) {
		t.Fatalf("done = %#v", state.Checkpoint.Done)
	}
	if !reflect.DeepEqual(state.Checkpoint.Next, []string{"wait for review"}) {
		t.Fatalf("next = %#v", state.Checkpoint.Next)
	}
	if !reflect.DeepEqual(state.Checkpoint.IssuePRActions, []string{"prepared PR #101 for review"}) {
		t.Fatalf("issue/pr actions = %#v", state.Checkpoint.IssuePRActions)
	}
	raw := state.ProcessedIssues["71"]
	if len(raw) == 0 {
		t.Fatal("processed issue entry for 71 is missing")
	}
	var tracked orchestration.TrackedState
	if err := json.Unmarshal(raw, &tracked); err != nil {
		t.Fatalf("json.Unmarshal(processed issue) error = %v", err)
	}
	if tracked.Status != orchestration.StatusReadyForReview {
		t.Fatalf("processed issue status = %q, want ready-for-review", tracked.Status)
	}
}

func TestRunIssueUsesGoNativeReusedBranchSyncPreflight(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{},
		{Stdout: "def456\n"},
		{Stdout: ""},
		{Stdout: "M internal/cli/command_run.go\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
		{Stdout: ""},
		{},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
		createPRURL:   "https://github.com/owner/repo/pull/101",
	}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{Stats: agentexec.Stats{ElapsedSeconds: 12}}}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--no-skip-if-branch-exists"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 1 {
		t.Fatalf("agent call count = %d, want 1", agent.callCount)
	}
	if len(lifecycle.commentBodies[71]) != 2 {
		t.Fatalf("comment bodies = %d, want 2", len(lifecycle.commentBodies[71]))
	}
	firstState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(first) error = %v", err)
	}
	if firstState.BranchLifecycle != orchestration.BranchLifecycleReused {
		t.Fatalf("first state branch lifecycle = %q, want reused", firstState.BranchLifecycle)
	}
	if firstState.ReusedBranchSync == nil {
		t.Fatal("first state reused_branch_sync = nil, want sync verdict")
	}
	if firstState.ReusedBranchSync.Status != orchestration.BranchSyncStatusSyncedCleanly || firstState.ReusedBranchSync.AppliedStrategy != "rebase" || !firstState.ReusedBranchSync.Changed {
		t.Fatalf("first state sync verdict = %#v", firstState.ReusedBranchSync)
	}
	finalState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][1])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(final) error = %v", err)
	}
	if finalState.BranchLifecycle != orchestration.BranchLifecycleReused {
		t.Fatalf("final state branch lifecycle = %q, want reused", finalState.BranchLifecycle)
	}
	if finalState.ReusedBranchSync == nil || finalState.ReusedBranchSync.AppliedStrategy != "rebase" {
		t.Fatalf("final state sync verdict = %#v", finalState.ReusedBranchSync)
	}
	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
		gitCommand("checkout", "main"),
		gitCommand("checkout", "issue-fix/71-fix-runner"),
		gitCommand("fetch", "origin", "main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("rebase", "origin/main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("ls-files", "--others", "--exclude-standard"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("add", "-u"),
		gitCommand("ls-files", "--others", "--exclude-standard"),
		gitCommand("commit", "-m", "Fix issue #71: Fix runner"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("push", "-u", "--force-with-lease", "origin", "issue-fix/71-fix-runner"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
}

func TestRunIssuePushesSyncOnlyUpdateWhenReusedBranchChangedAndAgentNoop(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{},
		{Stdout: "def456\n"},
		{Stdout: ""},
		{Stdout: "\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
		createPRURL:   "https://github.com/owner/repo/pull/101",
	}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{Stats: agentexec.Stats{ElapsedSeconds: 7}}}
	var out strings.Builder
	var errOut strings.Builder
	app := NewApp(&out, &errOut)
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--no-skip-if-branch-exists"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0; stderr=%q cmds=%#v", code, errOut.String(), shell.cmds)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 1 {
		t.Fatalf("agent call count = %d, want 1", agent.callCount)
	}
	if len(lifecycle.createdPRs) != 1 {
		t.Fatalf("created PRs = %d, want 1", len(lifecycle.createdPRs))
	}
	if len(lifecycle.commentBodies[71]) != 2 {
		t.Fatalf("comment bodies = %d, want 2", len(lifecycle.commentBodies[71]))
	}
	finalState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][1])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(final) error = %v", err)
	}
	if finalState.Status != orchestration.StatusReadyForReview || finalState.Stage != "pr_ready" || finalState.NextAction != "wait_for_review" {
		t.Fatalf("final state = %#v", finalState)
	}
	if finalState.ReusedBranchSync == nil || !finalState.ReusedBranchSync.Changed {
		t.Fatalf("final state sync verdict = %#v, want changed sync verdict", finalState.ReusedBranchSync)
	}
	if !strings.Contains(out.String(), "No file changes from agent for issue #71; pushed sync-only branch updates") {
		t.Fatalf("stdout = %q, want sync-only message", out.String())
	}

	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
		gitCommand("checkout", "main"),
		gitCommand("checkout", "issue-fix/71-fix-runner"),
		gitCommand("fetch", "origin", "main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("rebase", "origin/main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("ls-files", "--others", "--exclude-standard"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("push", "-u", "--force-with-lease", "origin", "issue-fix/71-fix-runner"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
}

func TestRunIssueConflictRecoveryOnlySyncsAndPushesWithoutAgent(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{},
		{Stdout: "def456\n"},
		{Stdout: "issue-fix/71-fix-runner\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
	}
	agent := &fakeIssueAgentRunner{}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--conflict-recovery-only"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	if len(lifecycle.commentBodies[71]) != 1 {
		t.Fatalf("comment bodies = %d, want 1", len(lifecycle.commentBodies[71]))
	}
	state, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if state.Status != orchestration.StatusWaitingForAuthor || state.Stage != "sync_branch" || state.NextAction != "inspect_conflict_recovery_result" {
		t.Fatalf("state = %#v", state)
	}
	if state.BranchLifecycle != orchestration.BranchLifecycleReused {
		t.Fatalf("branch lifecycle = %q, want reused", state.BranchLifecycle)
	}
	if state.ReusedBranchSync == nil || state.ReusedBranchSync.Status != orchestration.BranchSyncStatusSyncedCleanly || !state.ReusedBranchSync.Changed {
		t.Fatalf("sync verdict = %#v", state.ReusedBranchSync)
	}
	if len(lifecycle.createdPRs) != 0 {
		t.Fatalf("created PRs = %d, want 0", len(lifecycle.createdPRs))
	}
	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
		gitCommand("checkout", "main"),
		gitCommand("checkout", "issue-fix/71-fix-runner"),
		gitCommand("fetch", "origin", "main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("rebase", "origin/main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("push", "-u", "--force-with-lease", "origin", "issue-fix/71-fix-runner"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
	printed := out.String()
	if !strings.Contains(printed, "Conflict recovery push result for branch 'issue-fix/71-fix-runner': pushed") || !strings.Contains(printed, "Conflict recovery result for branch 'issue-fix/71-fix-runner': synced cleanly") {
		t.Fatalf("stdout = %q, want recovery summaries", printed)
	}
}

func TestRunIssueConflictRecoveryOnlyNoopSkipsPush(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{},
		{Stdout: "abc123\n"},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
	}
	agent := &fakeIssueAgentRunner{}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--conflict-recovery-only"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	if len(lifecycle.commentBodies[71]) != 1 {
		t.Fatalf("comment bodies = %d, want 1", len(lifecycle.commentBodies[71]))
	}
	state, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if state.ReusedBranchSync == nil || state.ReusedBranchSync.Status != orchestration.BranchSyncStatusAlreadyCurrent || state.ReusedBranchSync.Changed {
		t.Fatalf("sync verdict = %#v", state.ReusedBranchSync)
	}
	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
		gitCommand("checkout", "main"),
		gitCommand("checkout", "issue-fix/71-fix-runner"),
		gitCommand("fetch", "origin", "main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("rebase", "origin/main"),
		gitCommand("rev-parse", "HEAD"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
	if strings.Contains(out.String(), "Conflict recovery push result") {
		t.Fatalf("stdout = %q, want no push summary", out.String())
	}
}

func TestRunIssueConflictRecoveryOnlyBlocksWhenDeterministicBranchMissing(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 1},
		{ExitCode: 2},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
	}
	agent := &fakeIssueAgentRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--conflict-recovery-only"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	if len(lifecycle.commentBodies[71]) != 1 {
		t.Fatalf("comment bodies = %d, want 1", len(lifecycle.commentBodies[71]))
	}
	state, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if state.Status != orchestration.StatusBlocked || state.Stage != "sync_branch" || state.NextAction != "run_normal_issue_flow_first" {
		t.Fatalf("state = %#v", state)
	}
	if !strings.Contains(state.Error, "requires an existing deterministic issue branch") {
		t.Fatalf("blocked error = %q, want missing-branch guidance", state.Error)
	}
	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
	if !strings.Contains(errOut.String(), "run the normal issue flow first") {
		t.Fatalf("stderr = %q, want explicit recovery guidance", errOut.String())
	}
}

func TestRunIssueBlocksWhenReusedBranchSyncCannotBeRecovered(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "main\n"},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{ExitCode: 1, Stderr: "merge conflict\n"},
		{Stdout: ""},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{
		issue:         githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		defaultBranch: "main",
	}
	agent := &fakeIssueAgentRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo", "--dir", "/repo", "--no-skip-if-branch-exists", "--sync-strategy", "merge"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	if len(lifecycle.commentBodies[71]) != 1 {
		t.Fatalf("comment bodies = %d, want 1", len(lifecycle.commentBodies[71]))
	}
	blockedState, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.commentBodies[71][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody(blocked) error = %v", err)
	}
	if blockedState.Status != orchestration.StatusBlocked || blockedState.Stage != "sync_branch" {
		t.Fatalf("blocked state = %#v", blockedState)
	}
	if blockedState.BranchLifecycle != orchestration.BranchLifecycleReused {
		t.Fatalf("blocked branch lifecycle = %q, want reused", blockedState.BranchLifecycle)
	}
	if !strings.Contains(blockedState.Error, "resolve conflicts manually") {
		t.Fatalf("blocked error = %q, want manual resolution hint", blockedState.Error)
	}
	wantCommands := []string{
		gitCommand("rev-parse", "--show-toplevel"),
		gitCommand("status", "--porcelain"),
		gitCommand("rev-parse", "--abbrev-ref", "HEAD"),
		gitCommand("show-ref", "--verify", "--quiet", "refs/heads/issue-fix/71-fix-runner"),
		gitCommand("ls-remote", "--exit-code", "--heads", "origin", "issue-fix/71-fix-runner"),
		gitCommand("checkout", "main"),
		gitCommand("checkout", "issue-fix/71-fix-runner"),
		gitCommand("fetch", "origin", "main"),
		gitCommand("rev-parse", "HEAD"),
		gitCommand("merge", "--no-edit", "-X", "theirs", "origin/main"),
		gitCommand("diff", "--name-only", "--diff-filter=U"),
		gitCommand("merge", "--abort"),
	}
	if !reflect.DeepEqual(shell.cmds, wantCommands) {
		t.Fatalf("shell commands = %#v, want %#v", shell.cmds, wantCommands)
	}
	if !strings.Contains(errOut.String(), "failed to prepare issue branch") {
		t.Fatalf("stderr = %q, want explicit sync preflight failure", errOut.String())
	}
}

func TestRunIssueRoutesLinkedPRToPRReviewFlow(t *testing.T) {
	runner := &recordingRunner{}
	lifecycle := &fakeDaemonLifecycle{
		issue:    githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		linkedPR: &githublifecycle.PullRequest{Number: 101, Title: "Existing fix", HeadRefName: "issue-fix/71-fix-runner"},
	}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetIssueLifecycle(lifecycle)
	app.SetPRLifecycle(nil)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "101", "--from-review-comments", "--repo", "owner/repo"})
	if !strings.Contains(errOut.String(), "routing issue #71 to pr review flow") || !strings.Contains(errOut.String(), "linked open PR #101") {
		t.Fatalf("stderr = %q, want explicit routing reason", errOut.String())
	}
}

func TestRunIssueRoutesReadyToMergeRecoveryToPRReviewFlow(t *testing.T) {
	runner := &recordingRunner{}
	stateBody, err := orchestration.BuildOrchestrationStateComment(orchestration.TrackedState{
		Status:   orchestration.StatusReadyToMerge,
		TaskType: orchestration.TaskTypePR,
		Issue:    intPtr(71),
		PR:       intPtr(101),
	})
	if err != nil {
		t.Fatalf("BuildOrchestrationStateComment() error = %v", err)
	}
	lifecycle := &fakeDaemonLifecycle{
		issue:    githublifecycle.Issue{Number: 71, Title: "Fix runner", Body: "Issue body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		linkedPR: &githublifecycle.PullRequest{Number: 101, Title: "Existing fix", HeadRefName: "issue-fix/71-fix-runner"},
		commentsByIssue: map[int][]githublifecycle.IssueComment{
			71: {{ID: 1, Body: stateBody, CreatedAt: "2026-05-01T12:00:00Z"}},
		},
	}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetIssueLifecycle(lifecycle)
	app.SetPRLifecycle(nil)

	code := app.Run([]string{"run", "issue", "--id", "71", "--repo", "owner/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "101", "--from-review-comments", "--repo", "owner/repo"})
	if !strings.Contains(errOut.String(), "ready-to-merge") {
		t.Fatalf("stderr = %q, want recovered ready-to-merge reason", errOut.String())
	}
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
	execPath := filepath.Join(targetDir, "orchestrator")
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	app.SetExecutablePath(execPath)
	app.SetIssueLifecycle(&fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{}})

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
	for _, req := range starter.reqs {
		if req.Name != execPath {
			t.Fatalf("worker command = %q, want %q", req.Name, execPath)
		}
		if len(req.Args) < 4 || !reflect.DeepEqual(req.Args[:4], []string{"run", "issue", "--id", flagValue(req.Args, "--id")}) {
			t.Fatalf("worker args = %#v, want run issue entrypoint", req.Args)
		}
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
		if state.PushRemote != "https://github.com/owner/repo.git" {
			t.Fatalf("push remote = %q, want https://github.com/owner/repo.git", state.PushRemote)
		}
	}
	printed := out.String()
	for _, want := range []string{"started detached worker issue-71", "started detached worker issue-72"} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestRunBatchDetachRoutesLinkedPRToNativePRCommand(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	execPath := filepath.Join(targetDir, "orchestrator")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	app.SetExecutablePath(execPath)
	app.SetIssueLifecycle(&fakeDaemonLifecycle{
		issue:           githublifecycle.Issue{Number: 71, Title: "Fix runtime", Body: "Body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		linkedPR:        &githublifecycle.PullRequest{Number: 101, Title: "Existing fix", HeadRefName: "feature/pr-101", BaseRefName: "main"},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "batch", "--ids", "71", "--repo", "owner/repo", "--dir", targetDir, "--detach"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 1 {
		t.Fatalf("starter calls = %d, want 1", starter.calls)
	}
	if starter.req.Name != execPath {
		t.Fatalf("worker command = %q, want %q", starter.req.Name, execPath)
	}
	if !reflect.DeepEqual(starter.req.Args[:4], []string{"run", "pr", "--id", "101"}) {
		t.Fatalf("worker args = %#v, want native pr entrypoint", starter.req.Args)
	}
	joined := strings.Join(starter.req.Args, " ")
	if !strings.Contains(joined, "--isolate-worktree") {
		t.Fatalf("worker args = %#v, want isolate worktree flag", starter.req.Args)
	}
	if strings.Contains(joined, "--allow-pr-branch-switch") {
		t.Fatalf("worker args = %#v, should not include branch switch flag", starter.req.Args)
	}
}

func TestRunPRDetachUsesNativeWorkerWithMigratedFlags(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	targetDir := t.TempDir()
	execPath := filepath.Join(targetDir, "orchestrator")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetExecutablePath(execPath)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--isolate-worktree", "--post-pr-summary"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 1 {
		t.Fatalf("starter calls = %d, want 1", starter.calls)
	}
	if starter.req.Name != execPath {
		t.Fatalf("worker command = %q, want native executable %q", starter.req.Name, execPath)
	}
	if len(starter.req.Args) < 4 || !reflect.DeepEqual(starter.req.Args[:4], []string{"run", "pr", "--id", "72"}) {
		t.Fatalf("worker args = %#v, want native pr entrypoint", starter.req.Args)
	}
	joined := strings.Join(starter.req.Args, " ")
	for _, want := range []string{"--repo owner/repo", "--isolate-worktree", "--post-pr-summary"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("worker args = %#v, want %q", starter.req.Args, want)
		}
	}
	if strings.Contains(joined, runnerScript) || strings.Contains(starter.req.Name, "python") {
		t.Fatalf("worker = %q %#v, should not use python fallback", starter.req.Name, starter.req.Args)
	}
}

func TestRunBatchDetachKeepsNativeWorkerWithProjectConfig(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	execPath := filepath.Join(targetDir, "orchestrator")
	app.SetExecutablePath(execPath)
	app.SetIssueLifecycle(&fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{}})
	projectConfig := `{"defaults":{"preset":"default","runner":"opencode","agent":"build","model":"openai/gpt-4o","max_attempts":2},"presets":{"default":{"runner":"claude","agent":"build","model":"claude-sonnet-4-5","max_attempts":3}},"budgets":{"max_attempts_per_task":2}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}

	code := app.Run([]string{"run", "batch", "--ids", "71", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--project-config", "project.json"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.req.Name != execPath {
		t.Fatalf("worker command = %q, want native executable %q", starter.req.Name, execPath)
	}
	if len(starter.req.Args) < 4 || !reflect.DeepEqual(starter.req.Args[:4], []string{"run", "issue", "--id", "71"}) {
		t.Fatalf("worker args = %#v, want native issue entrypoint", starter.req.Args)
	}
	joinedArgs := strings.Join(starter.req.Args, " ")
	for _, want := range []string{"--runner claude", "--agent build", "--model claude-sonnet-4-5", "--max-attempts 2"} {
		if !strings.Contains(joinedArgs, want) {
			t.Fatalf("worker args = %#v, want project config default %q", starter.req.Args, want)
		}
	}
	if strings.Contains(errOut.String(), "--project-config is not supported by the Go-native") {
		t.Fatalf("stderr = %q, should not include project-config fallback reason", errOut.String())
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

func TestRunIssueCommandForwardsLightweightFlag(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--id", "20", "--lightweight"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--issue", "20", "--lightweight"})
}

func TestRunIssueWithoutGroomingConfigPreservesExistingArgs(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--id", "20"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joined := strings.Join(runner.args, " ")
	if strings.Contains(joined, "--grooming-") {
		t.Fatalf("runner args = %#v, should not include grooming flags by default", runner.args)
	}
}

func TestRunIssueUsesGroomingDefaultsFromProjectConfig(t *testing.T) {
	runner := &recordingRunner{}
	targetDir := t.TempDir()
	projectConfig := `{"grooming":{"mode":"auto","require_plan_approval":true,"ask_questions":true,"auto_continue_after_plan":true,"max_questions":4,"max_rounds":2}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--id", "20", "--dir", targetDir, "--project-config", "project.json"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joined := strings.Join(runner.args, " ")
	for _, want := range []string{"--grooming-mode auto", "--grooming-require-plan-approval", "--grooming-ask-questions", "--grooming-auto-continue-after-plan", "--grooming-max-questions 4", "--grooming-max-rounds 2"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("runner args = %#v, want %q", runner.args, want)
		}
	}
}

func TestRunIssueRejectsInvalidGroomingModeInProjectConfig(t *testing.T) {
	runner := &recordingRunner{}
	targetDir := t.TempDir()
	projectConfig := `{"grooming":{"mode":"sometimes"}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--id", "20", "--dir", targetDir, "--project-config", "project.json"})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "unsupported grooming mode") {
		t.Fatalf("stderr = %q, want grooming mode validation", errOut.String())
	}
}

func TestRunIssueGroomingCLIOverridesProjectConfig(t *testing.T) {
	runner := &recordingRunner{}
	targetDir := t.TempDir()
	projectConfig := `{"grooming":{"mode":"auto","max_questions":4}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "issue", "--id", "20", "--dir", targetDir, "--project-config", "project.json", "--grooming-mode", "always", "--grooming-max-questions", "9"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if got := flagValue(runner.args, "--grooming-mode"); got != "always" {
		t.Fatalf("grooming mode = %q, want always", got)
	}
	if got := flagValue(runner.args, "--grooming-max-questions"); got != "9" {
		t.Fatalf("grooming max questions = %q, want 9", got)
	}
}

func TestRunPRCommandFallsBackWithoutRepo(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "pr", "--id", "72", "--dry-run", "--isolate-worktree"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "72", "--from-review-comments", "--dry-run", "--isolate-worktree"})
}

func TestRunPRNativeDryRunSupportsCompatibilityFlags(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "feature/pr-72\n"},
	}}
	lifecycle := &fakeDaemonLifecycle{pullRequest: githublifecycle.PullRequest{Number: 72, HeadRefName: "feature/pr-72", BaseRefName: "main"}}
	agent := &fakeIssueAgentRunner{}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetPRLifecycle(lifecycle)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dry-run", "--from-review-comments", "--isolate-worktree", "--post-pr-summary", "--pr-followup-branch-prefix", "followup"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	stdout := out.String()
	for _, want := range []string{"[dry-run] Would create isolated worktree", "[dry-run] Would create follow-up branch", "[dry-run] Native PR flow preflight succeeded"} {
		if !strings.Contains(stdout, want) {
			t.Fatalf("stdout = %q, want %q", stdout, want)
		}
	}
}

func TestRunPRNativeDryRunWithRepoSkipsPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "feature/pr-72\n"},
	}}
	lifecycle := &fakeDaemonLifecycle{pullRequest: githublifecycle.PullRequest{Number: 72, HeadRefName: "feature/pr-72", BaseRefName: "main"}}
	agent := &fakeIssueAgentRunner{}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetPRLifecycle(lifecycle)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	if !strings.Contains(out.String(), "[dry-run] Native PR flow preflight succeeded") {
		t.Fatalf("stdout = %q, want dry-run native preflight message", out.String())
	}
}

func TestRunPRConflictRecoveryOnlySyncsWithoutAgent(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{},
		{Stdout: "def456\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	lifecycle := &fakeDaemonLifecycle{pullRequest: githublifecycle.PullRequest{Number: 72, HeadRefName: "feature/pr-72", BaseRefName: "main"}}
	agent := &fakeIssueAgentRunner{}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetPRLifecycle(lifecycle)
	app.SetIssueLifecycle(lifecycle)
	app.SetIssueAgentRunner(agent)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dir", "/repo", "--conflict-recovery-only", "--sync-strategy", "rebase"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 0 {
		t.Fatalf("agent call count = %d, want 0", agent.callCount)
	}
	if len(lifecycle.prCommentBodies[72]) != 1 {
		t.Fatalf("PR comment bodies = %d, want 1", len(lifecycle.prCommentBodies[72]))
	}
	state, err := orchestration.ParseOrchestrationStateCommentBody(lifecycle.prCommentBodies[72][0])
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if state.Status != orchestration.StatusWaitingForAuthor || state.Stage != "sync_branch" || state.NextAction != "inspect_conflict_recovery_result" {
		t.Fatalf("state = %#v", state)
	}
	if state.ReusedBranchSync == nil || state.ReusedBranchSync.Status != orchestration.BranchSyncStatusSyncedCleanly || !state.ReusedBranchSync.Changed {
		t.Fatalf("sync verdict = %#v", state.ReusedBranchSync)
	}
	if !strings.Contains(out.String(), "Conflict recovery result for branch 'feature/pr-72': synced cleanly") {
		t.Fatalf("stdout = %q, want recovery summary", out.String())
	}
}

func TestRunPRDetachWithRepoUsesNativeWorkerCommand(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	targetDir := t.TempDir()
	execPath := filepath.Join(targetDir, "orchestrator")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetExecutablePath(execPath)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dir", targetDir, "--detach"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 1 {
		t.Fatalf("starter calls = %d, want 1", starter.calls)
	}
	if starter.req.Name != execPath {
		t.Fatalf("worker command = %q, want %q", starter.req.Name, execPath)
	}
	if !reflect.DeepEqual(starter.req.Args[:4], []string{"run", "pr", "--id", "72"}) {
		t.Fatalf("worker args = %#v, want native pr entrypoint", starter.req.Args)
	}
}

func TestRunDaemonCommandWiresPythonRunner(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--issue", "71", "--repo", "owner/repo", "--dry-run"})
	assertCommandContainsFlag(t, runner.args, "--autonomous-session-file")
}

func TestRunDaemonRequiresLiveSideEffectsOptIn(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--max-cycles", "1", "--poll-interval-seconds", "1"})
	if code != 2 {
		t.Fatalf("Run() code = %d, want 2", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "--allow-live-side-effects") {
		t.Fatalf("stderr = %q, want live side-effects opt-in guidance", errOut.String())
	}
}

func TestRunDaemonRoutesLinkedPRWorkerWithIsolatedWorktree(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetIssueLifecycle(&fakeDaemonLifecycle{
		issue:           githublifecycle.Issue{Number: 71, Title: "Fix runtime", Body: "Body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		linkedPR:        &githublifecycle.PullRequest{Number: 101, Title: "Existing fix", HeadRefName: "feature/pr-101", BaseRefName: "main"},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--max-cycles", "1", "--poll-interval-seconds", "0", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	joined := strings.Join(runner.args, "\n")
	if !strings.Contains(joined, "run\npr") || !strings.Contains(joined, "--id") || !strings.Contains(joined, "101") {
		t.Fatalf("runner args = %#v, want PR routing", runner.args)
	}
	if !strings.Contains(joined, "--isolate-worktree") {
		t.Fatalf("runner args = %#v, want isolate worktree flag", runner.args)
	}
	if strings.Contains(joined, "--allow-pr-branch-switch") {
		t.Fatalf("runner args = %#v, should not include branch switch flag", runner.args)
	}
}

func TestRunDaemonRoutesDirtyLinkedPRToConflictRecovery(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetIssueLifecycle(&fakeDaemonLifecycle{
		issue:           githublifecycle.Issue{Number: 71, Title: "Fix runtime", Body: "Body", URL: "https://github.com/owner/repo/issues/71", Tracker: githublifecycle.TrackerGitHub},
		linkedPR:        &githublifecycle.PullRequest{Number: 101, Title: "Existing fix", HeadRefName: "feature/pr-101", BaseRefName: "main", MergeStateStatus: "DIRTY", Mergeable: "CONFLICTING"},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues:          []githublifecycle.Issue{{Number: 71}},
		linkedPR:        &githublifecycle.PullRequest{Number: 101, Title: "Existing fix", HeadRefName: "feature/pr-101", BaseRefName: "main", MergeStateStatus: "DIRTY", Mergeable: "CONFLICTING"},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--max-cycles", "1", "--poll-interval-seconds", "0", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joined := strings.Join(runner.args, "\n")
	if !strings.Contains(joined, "run\npr") || !strings.Contains(joined, "--id") || !strings.Contains(joined, "101") || !strings.Contains(joined, "--conflict-recovery-only") {
		t.Fatalf("runner args = %#v, want PR conflict recovery routing", runner.args)
	}
	if strings.Contains(joined, "--isolate-worktree") {
		t.Fatalf("runner args = %#v, conflict recovery should use native PR branch sync", runner.args)
	}
}

func TestDaemonReviewFeedbackSignalUsesStableFeedbackIdentity(t *testing.T) {
	lifecycle := &fakeDaemonLifecycle{
		linkedPR: &githublifecycle.PullRequest{Number: 101, Author: &githublifecycle.Actor{Login: "author"}},
		reviewThreadSeq: [][]githublifecycle.PullRequestReviewThread{
			{{Comments: []githublifecycle.PullRequestReviewComment{{Body: "Please update naming", URL: "https://example/review/1", Author: &githublifecycle.Actor{Login: "reviewer"}}}}},
			{{Comments: []githublifecycle.PullRequestReviewComment{{Body: "Please update naming", URL: "https://example/review/2", Author: &githublifecycle.Actor{Login: "reviewer"}}}}},
		},
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDaemonLifecycle(lifecycle)

	first, err := app.daemonReviewFeedbackSignal(context.Background(), "owner/repo", githublifecycle.Issue{Number: 71})
	if err != nil {
		t.Fatalf("daemonReviewFeedbackSignal(first) error = %v", err)
	}
	second, err := app.daemonReviewFeedbackSignal(context.Background(), "owner/repo", githublifecycle.Issue{Number: 71})
	if err != nil {
		t.Fatalf("daemonReviewFeedbackSignal(second) error = %v", err)
	}
	if first == "" || second == "" {
		t.Fatalf("signals should not be empty: first=%q second=%q", first, second)
	}
	if first == second {
		t.Fatalf("signals should differ when actionable feedback changes: first=%q second=%q", first, second)
	}
}

func TestRunDaemonReusesAutonomousSessionFileAcrossCycles(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetDaemonLifecycle(nil)

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

func TestRunDaemonGoPolicyProcessesDistinctIssuesAcrossCycles(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues:          []githublifecycle.Issue{{Number: 71}, {Number: 72}},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "2", "--poll-interval-seconds", "0", "--max-cycles", "2", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 2 {
		t.Fatalf("runner calls = %d, want 2", runner.calls)
	}
	if got := flagValue(runner.cmds[0][1:], "--issue"); got != "71" {
		t.Fatalf("first issue = %q, want 71", got)
	}
	if got := flagValue(runner.cmds[1][1:], "--issue"); got != "72" {
		t.Fatalf("second issue = %q, want 72", got)
	}
}

func TestRunDaemonResumeAutonomousSessionSkipsProcessedAndPrintsSummary(t *testing.T) {
	runner := &recordingRunner{}
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(sessionPath, []byte(`{
  "processed_issues": {"71": {"status": "ready-for-review", "task_type": "issue", "issue": 71}},
  "checkpoint": {
    "phase": "running",
    "owner": {"repo": "owner/repo"},
    "current": "issue #72",
    "next": ["continue queued work"],
    "blockers": ["blocked by #70"]
  }
}
`), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	var out strings.Builder
	var errOut strings.Builder
	app := NewApp(&out, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues:          []githublifecycle.Issue{{Number: 71}, {Number: 72}},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "2", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run", "--resume-autonomous-session-file", sessionPath})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0\nstderr=%s", code, errOut.String())
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	if got := flagValue(runner.args, "--issue"); got != "72" {
		t.Fatalf("selected issue = %q, want 72; args=%#v", got, runner.args)
	}
	for _, want := range []string{"Resume summary:", "Resumed issue IDs: #72", "Skipped blockers: blocked by #70", "Next action: continue queued work"} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("stdout missing %q\n%s", want, out.String())
		}
	}
	if !strings.Contains(errOut.String(), "skipping issue #71: already handled") {
		t.Fatalf("stderr = %q, want processed issue skip", errOut.String())
	}
}

func TestRunDaemonResumeMissingCheckpointFails(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dry-run", "--max-cycles", "1", "--resume-autonomous-session-file", filepath.Join(t.TempDir(), "missing.json")})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "does not exist") {
		t.Fatalf("stderr = %q, want missing checkpoint error", errOut.String())
	}
}

func TestRunDaemonResumeMalformedCheckpointFails(t *testing.T) {
	runner := &recordingRunner{}
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(sessionPath, []byte("{"), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dry-run", "--max-cycles", "1", "--resume-autonomous-session-file", sessionPath})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "failed to load resume checkpoint") {
		t.Fatalf("stderr = %q, want malformed checkpoint error", errOut.String())
	}
}

func TestRunDaemonResumeCompletedCheckpointNoops(t *testing.T) {
	runner := &recordingRunner{}
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(sessionPath, []byte(`{
  "processed_issues": {"71": {"status": "ready-for-review", "task_type": "issue", "issue": 71}},
  "checkpoint": {"phase": "completed", "owner": {"repo": "owner/repo"}, "current": "Idle between autonomous runs"}
}
`), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dry-run", "--max-cycles", "1", "--resume-autonomous-session-file", sessionPath})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(out.String(), "Completed session: nothing to resume") {
		t.Fatalf("stdout = %q, want completed no-op summary", out.String())
	}
}

func TestRunDaemonResumeOwnershipMismatchFails(t *testing.T) {
	runner := &recordingRunner{}
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(sessionPath, []byte(`{
  "processed_issues": {},
  "checkpoint": {"phase": "running", "owner": {"repo": "other/repo"}}
}
`), 0o644); err != nil {
		t.Fatalf("WriteFile(session) error = %v", err)
	}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dry-run", "--max-cycles", "1", "--resume-autonomous-session-file", sessionPath})
	if code != 1 {
		t.Fatalf("Run() code = %d, want 1", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "ownership mismatch") {
		t.Fatalf("stderr = %q, want ownership mismatch", errOut.String())
	}
}

func TestRunDaemonSkipsIssueWithMixedOpenDependencies(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{
			{Number: 330, Body: "Depends on: #325, #326", Tracker: githublifecycle.TrackerGitHub},
			{Number: 331, Body: "", Tracker: githublifecycle.TrackerGitHub},
		},
		issuesByNumber: map[int]githublifecycle.Issue{
			325: {Number: 325, State: "closed"},
			326: {Number: 326, State: "open"},
		},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "2", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	if got := flagValue(runner.cmds[0][1:], "--issue"); got != "331" {
		t.Fatalf("selected issue = %q, want 331", got)
	}
	if !strings.Contains(errOut.String(), "blocked by open dependencies: #326") {
		t.Fatalf("stderr = %q, want dependency skip reason", errOut.String())
	}
}

func TestRunDaemonSkipsRetryBackoffWithoutStarvingRunnableIssue(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	targetDir := t.TempDir()
	projectConfigPath := filepath.Join(targetDir, "project-config.json")
	if err := os.WriteFile(projectConfigPath, []byte(`{"retry":{"max_attempts":2,"backoff_seconds":300}}`), 0o644); err != nil {
		t.Fatalf("WriteFile(%q) error = %v", projectConfigPath, err)
	}
	failedComment := githublifecycle.IssueComment{
		ID:        1,
		CreatedAt: "2099-05-01T11:59:00Z",
		Body:      orchestration.OrchestrationStateMarker + "\n```json\n{\"status\":\"failed\",\"attempt\":1}\n```",
	}
	lifecycle := &fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{
			{Number: 71, Labels: []githublifecycle.Label{{Name: "auto:agent-failed"}}},
			{Number: 72},
		},
		commentsByIssue: map[int][]githublifecycle.IssueComment{71: {failedComment}},
	}
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(lifecycle)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--project-config", projectConfigPath, "--limit", "1", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	if got := flagValue(runner.cmds[0][1:], "--issue"); got != "72" {
		t.Fatalf("selected issue = %q, want 72", got)
	}
	if len(lifecycle.listIssueLimits) != 1 || lifecycle.listIssueLimits[0] < 2 {
		t.Fatalf("list issue limits = %#v, want daemon to over-scan retry-limited front issue", lifecycle.listIssueLimits)
	}
	if !strings.Contains(errOut.String(), "retry backoff has not elapsed") || !strings.Contains(errOut.String(), "next eligible at") {
		t.Fatalf("stderr = %q, want retry backoff skip reason", errOut.String())
	}
}

func TestRunDaemonAllowsIssueWhenAllDependenciesClosed(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{{Number: 330, Body: "Depends on: #325, #326", Tracker: githublifecycle.TrackerGitHub}},
		issuesByNumber: map[int]githublifecycle.Issue{
			325: {Number: 325, State: "closed"},
			326: {Number: 326, State: "closed"},
		},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	if got := flagValue(runner.cmds[0][1:], "--issue"); got != "330" {
		t.Fatalf("selected issue = %q, want 330", got)
	}
}

func TestRunDaemonIgnoresMalformedDependencyReferences(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues:          []githublifecycle.Issue{{Number: 330, Body: "Depends on: #abc, not-an-issue", Tracker: githublifecycle.TrackerGitHub}},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
}

func TestRunDaemonTreatsSelfDependencyAsBlocked(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{{Number: 330, Body: "Depends on: #330", State: "open", Tracker: githublifecycle.TrackerGitHub}},
		issuesByNumber: map[int]githublifecycle.Issue{
			330: {Number: 330, State: "open"},
		},
		commentsByIssue: map[int][]githublifecycle.IssueComment{},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "blocked by open dependencies: #330") {
		t.Fatalf("stderr = %q, want self-dependency skip reason", errOut.String())
	}
}

func TestRunDaemonReportsQueueCapacityWaitingCandidates(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{{Number: 330}, {Number: 331}},
		commentsByIssue: map[int][]githublifecycle.IssueComment{
			330: {},
			331: {},
		},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "2", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run", "--max-parallel-tasks", "1"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	if !strings.Contains(errOut.String(), "daemon candidate issue #331: waiting (queue_capacity)") {
		t.Fatalf("stderr = %q, want queue-capacity waiting reason", errOut.String())
	}
}

func TestRunDaemonSkipsUnsupportedProviderCandidates(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{{Number: 330, Tracker: "linear"}},
		commentsByIssue: map[int][]githublifecycle.IssueComment{
			330: {},
		},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "unsupported_provider") {
		t.Fatalf("stderr = %q, want unsupported provider reason code", errOut.String())
	}
}

func TestRunDaemonSkipsIssueAfterAgentRetryLimitReached(t *testing.T) {
	runner := &recordingRunner{}
	var errOut strings.Builder
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{
		issues: []githublifecycle.Issue{{Number: 330}},
		commentsByIssue: map[int][]githublifecycle.IssueComment{
			330: {
				{Body: orchestration.OrchestrationStateMarker + "\n```json\n{\"status\":\"failed\",\"next_action\":\"inspect_retry_limit\",\"error\":\"retry limit reached\"}\n```"},
			},
		},
	})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "1", "--poll-interval-seconds", "0", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "agent_failed_retry_limit") {
		t.Fatalf("stderr = %q, want retry-limit reason code", errOut.String())
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
	execPath := filepath.Join(targetDir, "orchestrator")
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	app.SetExecutablePath(execPath)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--max-parallel-tasks", "2", "--allow-live-side-effects"})
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
	for _, req := range starter.reqs {
		if req.Name != execPath {
			t.Fatalf("worker command = %q, want %q", req.Name, execPath)
		}
		if len(req.Args) < 2 || !reflect.DeepEqual(req.Args[:2], []string{"run", "daemon"}) {
			t.Fatalf("worker args = %#v, want run daemon entrypoint", req.Args)
		}
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
		if state.PushRemote != "https://github.com/owner/repo.git" {
			t.Fatalf("push remote = %q, want https://github.com/owner/repo.git", state.PushRemote)
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

func TestRunDaemonDetachPropagatesProjectPresetAndBudgets(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	execPath := filepath.Join(targetDir, "orchestrator")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	app.SetExecutablePath(execPath)
	projectConfig := `{"defaults":{"preset":"default"},"presets":{"default":{"runner":"opencode","agent":"build","model":"openai/gpt-4o","track_tokens":true,"token_budget":20000}},"budgets":{"max_cost_usd":2.5}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--project-config", "project.json", "--allow-live-side-effects"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joinedArgs := strings.Join(starter.req.Args, " ")
	for _, want := range []string{"--preset default", "--runner opencode", "--agent build", "--model openai/gpt-4o", "--track-tokens", "--token-budget 20000"} {
		if !strings.Contains(joinedArgs, want) {
			t.Fatalf("worker args = %#v, want %q", starter.req.Args, want)
		}
	}
	statePath := filepath.Join(targetDir, ".orchestrator", "workers", "daemon", "worker.json")
	state, err := workers.ReadState(statePath)
	if err != nil {
		t.Fatalf("workers.ReadState(%q) error = %v", statePath, err)
	}
	if state.Preset != "default" || state.Runner != "opencode" || state.Agent != "build" || state.Model != "openai/gpt-4o" || !state.TrackTokens || state.TokenBudget != 20000 || state.CostBudget != 2.5 {
		t.Fatalf("worker policy metadata = %#v", state)
	}
}

func TestRunDaemonDetachKeepsCLIOverridesOverProjectPreset(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	execPath := filepath.Join(targetDir, "orchestrator")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	app.SetExecutablePath(execPath)
	projectConfig := `{"defaults":{"preset":"default"},"presets":{"default":{"runner":"claude","model":"claude-sonnet-4-5","token_budget":20000},"hard":{"runner":"claude","model":"claude-opus-4-1","token_budget":40000}}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--project-config", "project.json", "--preset", "hard", "--runner", "opencode", "--allow-live-side-effects"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joinedArgs := strings.Join(starter.req.Args, " ")
	for _, want := range []string{"--preset hard", "--runner opencode", "--model claude-opus-4-1", "--token-budget 40000"} {
		if !strings.Contains(joinedArgs, want) {
			t.Fatalf("worker args = %#v, want %q", starter.req.Args, want)
		}
	}
	if strings.Contains(joinedArgs, "--runner claude") {
		t.Fatalf("worker args = %#v, should keep explicit runner override", starter.req.Args)
	}
}

func TestRunDaemonAppliesRoutedPresetPerSelectedIssue(t *testing.T) {
	runner := &recordingRunner{}
	targetDir := t.TempDir()
	projectConfig := `{"routing":{"default_preset":"hard","rules":[{"when":{"labels":["docs"],"task_types":["issue"]},"preset":"cheap"}]},"presets":{"cheap":{"runner":"opencode","agent":"build","model":"openai/gpt-4o-mini","token_budget":8000},"hard":{"runner":"claude","agent":"build","model":"claude-opus-4-1","token_budget":40000}}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetBatchClonePreparer(&recordingBatchClonePreparer{})
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71, Labels: []githublifecycle.Label{{Name: "docs"}}}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--project-config", "project.json", "--limit", "1", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 1 {
		t.Fatalf("runner calls = %d, want 1", runner.calls)
	}
	joinedArgs := strings.Join(runner.args, " ")
	for _, want := range []string{"--preset cheap", "--runner opencode", "--model openai/gpt-4o-mini", "--token-budget 8000"} {
		if !strings.Contains(joinedArgs, want) {
			t.Fatalf("runner args = %#v, want routed preset %q", runner.args, want)
		}
	}
}

func TestRunDaemonReportsMissingRoutedPreset(t *testing.T) {
	runner := &recordingRunner{}
	targetDir := t.TempDir()
	var errOut strings.Builder
	projectConfig := `{"routing":{"default_preset":"missing"},"presets":{"cheap":{"runner":"opencode"}}}`
	if err := os.WriteFile(filepath.Join(targetDir, "project.json"), []byte(projectConfig), 0o644); err != nil {
		t.Fatalf("WriteFile(project.json) error = %v", err)
	}
	app := NewApp(&strings.Builder{}, &errOut)
	app.SetRunner(runner)
	app.SetBatchClonePreparer(&recordingBatchClonePreparer{})
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--project-config", "project.json", "--limit", "1", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run"})
	if code == 0 {
		t.Fatalf("Run() code = %d, want failure", code)
	}
	if runner.calls != 0 {
		t.Fatalf("runner calls = %d, want 0", runner.calls)
	}
	if !strings.Contains(errOut.String(), "unknown preset \"missing\"") {
		t.Fatalf("stderr = %q, want missing preset validation", errOut.String())
	}
}

func TestRunDaemonDetachStartsThreeWorkersWhenRequested(t *testing.T) {
	starter := &recordingDetachedStarter{pid: 31337}
	cloner := &recordingBatchClonePreparer{}
	targetDir := t.TempDir()
	execPath := filepath.Join(targetDir, "orchestrator")
	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetDetachedStarter(starter)
	app.SetBatchClonePreparer(cloner)
	app.SetExecutablePath(execPath)

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--dir", targetDir, "--detach", "--max-parallel-tasks", "3", "--allow-live-side-effects"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if starter.calls != 3 {
		t.Fatalf("starter calls = %d, want 3", starter.calls)
	}
	if len(cloner.targetDirs) != 3 {
		t.Fatalf("clone preparations = %d, want 3", len(cloner.targetDirs))
	}
	for _, req := range starter.reqs {
		if req.Name != execPath {
			t.Fatalf("worker command = %q, want %q", req.Name, execPath)
		}
		if len(req.Args) < 2 || !reflect.DeepEqual(req.Args[:2], []string{"run", "daemon"}) {
			t.Fatalf("worker args = %#v, want run daemon entrypoint", req.Args)
		}
	}
	for _, workerID := range []string{"1", "2", "3"} {
		workerDir := filepath.Join(targetDir, ".orchestrator", "workers", "daemon-"+workerID)
		statePath := filepath.Join(workerDir, "worker.json")
		state, err := workers.ReadState(statePath)
		if err != nil {
			t.Fatalf("workers.ReadState(%q) error = %v", statePath, err)
		}
		if state.Name != "daemon-"+workerID {
			t.Fatalf("worker name = %q, want daemon-%s", state.Name, workerID)
		}
	}
	printed := out.String()
	for _, want := range []string{"started detached worker daemon-1", "started detached worker daemon-2", "started detached worker daemon-3"} {
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
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71}, {Number: 72}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

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
	seenIssues := map[string]bool{}
	seenDirs := map[string]bool{}
	for _, cmd := range runner.cmds {
		if got := flagValue(cmd[1:], "--issue"); got == "" {
			t.Fatalf("daemon worker missing --issue in %#v", cmd)
		} else {
			seenIssues[got] = true
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
	if !seenIssues["71"] || !seenIssues["72"] {
		t.Fatalf("seen issues = %#v, want 71 and 72", seenIssues)
	}
}

func TestRunDaemonParallelRunsVerificationOnceAfterWorkers(t *testing.T) {
	runner := &recordingRunner{}
	cloner := &recordingBatchClonePreparer{}
	shell := &fakeShellExecutor{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetBatchClonePreparer(cloner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71}, {Number: 72}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--max-parallel-tasks", "2", "--poll-interval-seconds", "1", "--max-cycles", "1", "--dry-run", "--post-batch-verify", "--create-followup-issue"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 2 {
		t.Fatalf("runner calls = %d, want 2", runner.calls)
	}
	workerCalls := 0
	for _, cmd := range runner.cmds {
		workerCalls++
		if got := flagValue(cmd[1:], "--issue"); got == "" {
			t.Fatalf("daemon worker missing --issue in %#v", cmd)
		}
	}
	if workerCalls != 2 {
		t.Fatalf("worker calls = %d, want 2", workerCalls)
	}
	if len(shell.cmds) != 0 {
		t.Fatalf("shell cmds = %#v, want none during dry-run verification", shell.cmds)
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

func TestBuildNativePRReviewPromptRequestsHyphenatedOutcomeStatuses(t *testing.T) {
	prompt := buildNativePRReviewPrompt(
		githublifecycle.PullRequest{Number: 72, Title: "Fix review feedback", URL: "https://github.com/owner/repo/pull/72", Body: "PR body"},
		[]orchestration.ReviewFeedbackItem{{Type: "review_comment", Author: "reviewer", Body: "Please add tests", Path: "app.go", Line: 12}},
		nil,
		false,
	)
	if !strings.Contains(prompt, "fixed|not-fixed|needs-human-follow-up") {
		t.Fatalf("prompt missing expected outcome statuses: %q", prompt)
	}
	if strings.Contains(prompt, "not_fixed|blocked") {
		t.Fatalf("prompt contains legacy outcome statuses: %q", prompt)
	}
}

func TestBuildPRReviewOutcomeSummaryFallbackUsesNotFixedStatus(t *testing.T) {
	summary := buildPRReviewOutcomeSummary(&agentexec.Result{Output: "no structured result"}, []orchestration.ReviewFeedbackItem{{Body: "Please add tests"}})
	if summary == nil || len(summary.Items) != 1 {
		t.Fatalf("summary = %#v, want one fallback item", summary)
	}
	if summary.Items[0].Status != "not-fixed" {
		t.Fatalf("status = %q, want not-fixed", summary.Items[0].Status)
	}
	if summary.Items[0].NextAction != "manual_review_follow_up_required" {
		t.Fatalf("next_action = %q, want manual_review_follow_up_required", summary.Items[0].NextAction)
	}
}

func TestBuildPRReviewFailureOutcomeMarksItemsForHumanFollowUp(t *testing.T) {
	summary := buildPRReviewFailureOutcome([]orchestration.ReviewFeedbackItem{{Body: "Please add tests"}, {Body: "Please rename this"}}, "agent failed", "inspect_agent_failure")
	if summary == nil || len(summary.Items) != 2 {
		t.Fatalf("summary = %#v, want two failed items", summary)
	}
	for _, item := range summary.Items {
		if item.Status != "needs-human-follow-up" {
			t.Fatalf("status = %q, want needs-human-follow-up", item.Status)
		}
		if item.Summary != "agent failed" || item.NextAction != "inspect_agent_failure" {
			t.Fatalf("item = %#v, want failure details", item)
		}
	}
}

func TestRunPRUsesNativeRuntimeLoopWhenRepoExplicit(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "feature/pr-72\n"},
		{Stdout: ""},
		{Stdout: " M internal/cli/pr_native.go\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: ""},
		{Stdout: "[feature/pr-72 abc123] Address review comments for PR #72\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: "pushed\n"},
	}}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{ExitCode: 0, Stats: agentexec.Stats{ElapsedSeconds: 5}}}
	lifecycle := &fakeDaemonLifecycle{
		issue: githublifecycle.Issue{Number: 243, Title: "Parent issue", Body: "Parent issue body", URL: "https://github.com/owner/repo/issues/243", Tracker: githublifecycle.TrackerGitHub},
		pullRequests: []githublifecycle.PullRequest{
			{
				Number:                  72,
				Title:                   "Move PR review runtime loop into Go",
				Body:                    "PR description",
				URL:                     "https://github.com/owner/repo/pull/72",
				HeadRefName:             "feature/pr-72",
				BaseRefName:             "main",
				Author:                  &githublifecycle.Actor{Login: "author"},
				ClosingIssuesReferences: []githublifecycle.IssueReference{{Number: 243}},
			},
			{
				Number:                  72,
				Title:                   "Move PR review runtime loop into Go",
				Body:                    "PR description",
				URL:                     "https://github.com/owner/repo/pull/72",
				HeadRefName:             "feature/pr-72",
				BaseRefName:             "main",
				Author:                  &githublifecycle.Actor{Login: "author"},
				ClosingIssuesReferences: []githublifecycle.IssueReference{{Number: 243}},
			},
		},
		reviewThreadSeq: [][]githublifecycle.PullRequestReviewThread{
			{{
				Comments: []githublifecycle.PullRequestReviewComment{{
					Body:   "Please add a regression test",
					Path:   "internal/cli/pr_native.go",
					Line:   12,
					Author: &githublifecycle.Actor{Login: "reviewer"},
				}},
			}},
			{},
		},
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueAgentRunner(agent)
	app.SetIssueLifecycle(lifecycle)
	app.SetPRLifecycle(lifecycle)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--post-pr-summary"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if runner.calls != 0 {
		t.Fatalf("python runner calls = %d, want 0", runner.calls)
	}
	if agent.callCount != 1 {
		t.Fatalf("agent call count = %d, want 1", agent.callCount)
	}
	if got := agent.requests[0].Prompt; !strings.Contains(got, "review_comment") || !strings.Contains(got, "Issue #243") {
		t.Fatalf("native PR prompt missing review or issue context: %q", got)
	}
	if len(lifecycle.prCommentBodies[72]) < 2 {
		t.Fatalf("PR comments posted = %d, want at least 2", len(lifecycle.prCommentBodies[72]))
	}
	var state *orchestration.TrackedState
	for _, body := range lifecycle.prCommentBodies[72] {
		parsed, err := orchestration.ParseOrchestrationStateCommentBody(body)
		if err != nil {
			t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
		}
		if parsed != nil {
			state = parsed
		}
	}
	if state == nil || state.Status != orchestration.StatusWaitingForCI {
		t.Fatalf("final PR state = %#v, want waiting-for-ci", state)
	}
	foundSummary := false
	for _, body := range lifecycle.prCommentBodies[72] {
		if strings.Contains(body, "Automated follow-up completed") && strings.Contains(body, "Addressed review feedback items: 1") {
			foundSummary = true
			break
		}
	}
	if !foundSummary {
		t.Fatalf("PR comments = %#v, want post-pr-summary comment", lifecycle.prCommentBodies[72])
	}
	joinedShell := strings.Join(shell.cmds, "\n")
	if !strings.Contains(joinedShell, "git 'commit' '-m' 'Address review comments for PR #72'") {
		t.Fatalf("shell commands missing PR commit: %s", joinedShell)
	}
	if !strings.Contains(joinedShell, "git 'push' '-u' 'origin' 'feature/pr-72'") {
		t.Fatalf("shell commands missing PR push: %s", joinedShell)
	}
}

func TestRunPRNativeRunsConflictRecoveryWhenReviewUpdateLeavesPRDirty(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "feature/pr-72\n"},
		{Stdout: ""},
		{Stdout: " M internal/cli/pr_native.go\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: ""},
		{Stdout: "[feature/pr-72 abc123] Address review comments for PR #72\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: "pushed\n"},
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{ExitCode: 0},
		{ExitCode: 0},
		{},
		{},
		{},
		{Stdout: "abc123\n"},
		{},
		{Stdout: "def456\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{},
	}}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{ExitCode: 0}}
	lifecycle := &fakeDaemonLifecycle{
		pullRequests: []githublifecycle.PullRequest{
			{Number: 72, Title: "Fix review feedback", URL: "https://github.com/owner/repo/pull/72", HeadRefName: "feature/pr-72", BaseRefName: "main", Author: &githublifecycle.Actor{Login: "author"}},
			{Number: 72, Title: "Fix review feedback", URL: "https://github.com/owner/repo/pull/72", HeadRefName: "feature/pr-72", BaseRefName: "main", MergeStateStatus: "DIRTY", Mergeable: "CONFLICTING", Author: &githublifecycle.Actor{Login: "author"}},
		},
		reviewThreadSeq: [][]githublifecycle.PullRequestReviewThread{{{
			Comments: []githublifecycle.PullRequestReviewComment{{Body: "Please add a regression test", Path: "internal/cli/pr_native.go", Line: 12, Author: &githublifecycle.Actor{Login: "reviewer"}}},
		}}},
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueAgentRunner(agent)
	app.SetIssueLifecycle(lifecycle)
	app.SetPRLifecycle(lifecycle)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dir", "/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	if agent.callCount != 1 {
		t.Fatalf("agent call count = %d, want 1", agent.callCount)
	}
	lastComment := lifecycle.prCommentBodies[72][len(lifecycle.prCommentBodies[72])-1]
	state, err := orchestration.ParseOrchestrationStateCommentBody(lastComment)
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if state == nil || state.Stage != "sync_branch" || state.ReusedBranchSync == nil || state.ReusedBranchSync.Status != orchestration.BranchSyncStatusSyncedCleanly {
		t.Fatalf("final PR state = %#v, want conflict recovery sync state", state)
	}
}

func TestRunPRNativeRebasesAndRetriesRejectedPush(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "feature/pr-72\n"},
		{Stdout: ""},
		{Stdout: " M internal/cli/pr_native.go\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: ""},
		{Stdout: "[feature/pr-72 abc123] Address review comments for PR #72\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{ExitCode: 1, Stderr: "! [rejected] feature/pr-72 -> feature/pr-72 (fetch first)\nerror: failed to push some refs\n"},
		{Stdout: "abc123\n"},
		{},
		{},
		{Stdout: "def456\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: "pushed\n"},
	}}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{ExitCode: 0}}
	lifecycle := &fakeDaemonLifecycle{
		pullRequests: []githublifecycle.PullRequest{
			{Number: 72, Title: "Fix review feedback", URL: "https://github.com/owner/repo/pull/72", HeadRefName: "feature/pr-72", BaseRefName: "main", Author: &githublifecycle.Actor{Login: "author"}},
			{Number: 72, Title: "Fix review feedback", URL: "https://github.com/owner/repo/pull/72", HeadRefName: "feature/pr-72", BaseRefName: "main", MergeStateStatus: "CLEAN", Mergeable: "MERGEABLE", Author: &githublifecycle.Actor{Login: "author"}},
		},
		reviewThreadSeq: [][]githublifecycle.PullRequestReviewThread{
			{{Comments: []githublifecycle.PullRequestReviewComment{{Body: "Please add a regression test", Path: "internal/cli/pr_native.go", Line: 12, Author: &githublifecycle.Actor{Login: "reviewer"}}}}},
			{},
		},
	}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueAgentRunner(agent)
	app.SetIssueLifecycle(lifecycle)
	app.SetPRLifecycle(lifecycle)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--dir", "/repo"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	joinedShell := strings.Join(shell.cmds, "\n")
	if !strings.Contains(joinedShell, "git 'fetch' 'origin' 'feature/pr-72'") || !strings.Contains(joinedShell, "git 'rebase' 'origin/feature/pr-72'") {
		t.Fatalf("shell commands missing rejected-push rebase recovery: %s", joinedShell)
	}
	lastComment := lifecycle.prCommentBodies[72][len(lifecycle.prCommentBodies[72])-1]
	state, err := orchestration.ParseOrchestrationStateCommentBody(lastComment)
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if state == nil || state.ReusedBranchSync == nil || state.ReusedBranchSync.RemoteBaseRef != "origin/feature/pr-72" {
		t.Fatalf("final PR state = %#v, want push rejection rebase verdict", state)
	}
}

func TestRunPRNativePersistsAutonomousSessionCheckpoint(t *testing.T) {
	runner := &recordingRunner{}
	shell := &fakeShellExecutor{results: []shellExecutionResult{
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: "feature/pr-72\n"},
		{Stdout: ""},
		{Stdout: " M internal/cli/pr_native.go\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: ""},
		{Stdout: ""},
		{Stdout: "[feature/pr-72 abc123] Address review comments for PR #72\n"},
		{Stdout: "feature/pr-72\n"},
		{Stdout: "/repo\n"},
		{Stdout: "pushed\n"},
	}}
	agent := &fakeIssueAgentRunner{result: &agentexec.Result{ExitCode: 0, Stats: agentexec.Stats{ElapsedSeconds: 5}}}
	lifecycle := &fakeDaemonLifecycle{
		issue: githublifecycle.Issue{Number: 243, Title: "Parent issue", Body: "Parent issue body", URL: "https://github.com/owner/repo/issues/243", Tracker: githublifecycle.TrackerGitHub},
		pullRequests: []githublifecycle.PullRequest{
			{
				Number:                  72,
				Title:                   "Move PR review runtime loop into Go",
				Body:                    "PR description",
				URL:                     "https://github.com/owner/repo/pull/72",
				HeadRefName:             "feature/pr-72",
				BaseRefName:             "main",
				Author:                  &githublifecycle.Actor{Login: "author"},
				ClosingIssuesReferences: []githublifecycle.IssueReference{{Number: 243}},
			},
			{
				Number:                  72,
				Title:                   "Move PR review runtime loop into Go",
				Body:                    "PR description",
				URL:                     "https://github.com/owner/repo/pull/72",
				HeadRefName:             "feature/pr-72",
				BaseRefName:             "main",
				Author:                  &githublifecycle.Actor{Login: "author"},
				ClosingIssuesReferences: []githublifecycle.IssueReference{{Number: 243}},
			},
		},
		reviewThreadSeq: [][]githublifecycle.PullRequestReviewThread{
			{{
				Comments: []githublifecycle.PullRequestReviewComment{{
					Body:   "Please add a regression test",
					Path:   "internal/cli/pr_native.go",
					Line:   12,
					Author: &githublifecycle.Actor{Login: "reviewer"},
				}},
			}},
			{},
		},
	}
	sessionPath := filepath.Join(t.TempDir(), "session.json")
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetShellExecutor(shell)
	app.SetIssueAgentRunner(agent)
	app.SetIssueLifecycle(lifecycle)
	app.SetPRLifecycle(lifecycle)

	code := app.Run([]string{"run", "pr", "--id", "72", "--repo", "owner/repo", "--autonomous-session-file", sessionPath})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	state, err := orchestration.LoadState(sessionPath)
	if err != nil {
		t.Fatalf("LoadState() error = %v", err)
	}
	if state.Checkpoint == nil {
		t.Fatal("checkpoint = nil")
	}
	if got := state.Checkpoint.Phase; got != "completed" {
		t.Fatalf("checkpoint phase = %q, want completed", got)
	}
	if got := state.Checkpoint.Counts.Processed; got != 1 {
		t.Fatalf("processed count = %d, want 1", got)
	}
	if got := state.Checkpoint.Counts.Failures; got != 0 {
		t.Fatalf("failure count = %d, want 0", got)
	}
	if !reflect.DeepEqual(state.Checkpoint.Done, []string{"PR #72 (waiting-for-ci)"}) {
		t.Fatalf("done = %#v", state.Checkpoint.Done)
	}
	if !reflect.DeepEqual(state.Checkpoint.Next, []string{"wait for ci"}) {
		t.Fatalf("next = %#v", state.Checkpoint.Next)
	}
	if !reflect.DeepEqual(state.Checkpoint.IssuePRActions, []string{"pushed PR updates and waiting for CI"}) {
		t.Fatalf("issue/pr actions = %#v", state.Checkpoint.IssuePRActions)
	}
	raw := state.ProcessedIssues["72"]
	if len(raw) == 0 {
		t.Fatal("processed PR entry for 72 is missing")
	}
	var tracked orchestration.TrackedState
	if err := json.Unmarshal(raw, &tracked); err != nil {
		t.Fatalf("json.Unmarshal(processed PR) error = %v", err)
	}
	if tracked.Status != orchestration.StatusWaitingForCI {
		t.Fatalf("processed PR status = %q, want waiting-for-ci", tracked.Status)
	}
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

func TestRunPRCommandForwardsModeLightweight(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)

	code := app.Run([]string{"run", "pr", "--id", "72", "--mode", "lightweight"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--pr", "72", "--from-review-comments", "--mode", "lightweight"})
}

func TestRunDaemonCommandSupportsAllState(t *testing.T) {
	runner := &recordingRunner{}
	app := NewApp(&strings.Builder{}, &strings.Builder{})
	app.SetRunner(runner)
	app.SetDaemonLifecycle(&fakeDaemonLifecycle{issues: []githublifecycle.Issue{{Number: 71}}, commentsByIssue: map[int][]githublifecycle.IssueComment{}})

	code := app.Run([]string{"run", "daemon", "--repo", "owner/repo", "--limit", "3", "--state", "all", "--dry-run", "--poll-interval-seconds", "1"})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	assertCommand(t, runner, []string{runnerScript, "--issue", "71", "--repo", "owner/repo", "--dry-run"})
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
		PushRemote: "https://github.com/owner/repo.git",
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
		"push-remote: https://github.com/owner/repo.git",
		"agent: runner=opencode agent=build model=openai/gpt-4o",
		"log-progress: lines=2",
		"log-freshness: updated ",
		"log: " + logPath,
		"orchestrator status --issue 71 --repo owner/repo",
	} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestStatusWorkerShowsBatchSummary(t *testing.T) {
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
		"batch-done: none",
		"batch-current: issue #71: running; issue #72: exited",
		"batch-next: issue #71: tail -f ",
	} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestStatusWorkerBatchSummaryUsesLinkedIssueState(t *testing.T) {
	targetDir := t.TempDir()
	workerRoot := filepath.Join(targetDir, ".orchestrator", "workers")
	states := []detachedWorkerState{
		{
			Name:       "issue-71",
			Mode:       "run batch",
			TargetKind: "issue",
			TargetID:   "71",
			Repo:       "owner/repo",
			PID:        0,
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
		if err := os.WriteFile(state.LogPath, []byte("line 1\n"), 0o644); err != nil {
			t.Fatalf("WriteFile(log) error = %v", err)
		}
	}
	if err := workers.WriteBatchStates(states); err != nil {
		t.Fatalf("workers.WriteBatchStates() error = %v", err)
	}
	issue71State, err := orchestration.BuildOrchestrationStateComment(orchestration.TrackedState{
		Status:     orchestration.StatusBlocked,
		TaskType:   "issue",
		Issue:      intPtr(71),
		PR:         intPtr(101),
		Stage:      "review_feedback",
		NextAction: "resolve_review_comments",
		Error:      "merge conflict in README.md",
		Timestamp:  "2026-04-28T12:05:00Z",
	})
	if err != nil {
		t.Fatalf("BuildOrchestrationStateComment(issue71) error = %v", err)
	}
	issue72State, err := orchestration.BuildOrchestrationStateComment(orchestration.TrackedState{
		Status:     orchestration.StatusReadyToMerge,
		TaskType:   "issue",
		Issue:      intPtr(72),
		PR:         intPtr(102),
		Stage:      "merge_gate",
		NextAction: "ready_for_merge",
		MergeReadiness: &orchestration.PRMergeReadiness{
			Status:     orchestration.StatusReadyToMerge,
			NextAction: "ready_for_merge",
		},
		Timestamp: "2026-04-28T12:06:00Z",
	})
	if err != nil {
		t.Fatalf("BuildOrchestrationStateComment(issue72) error = %v", err)
	}
	lifecycle := &fakeDaemonLifecycle{commentsByIssue: map[int][]githublifecycle.IssueComment{
		71: {
			{ID: 1, Body: issue71State, CreatedAt: "2026-04-28T12:05:00Z"},
			{ID: 3, Body: orchestration.OrchestrationStateMarker + "\n```json\n{not-json}\n```", CreatedAt: "2026-04-28T12:07:00Z"},
		},
		72: {{ID: 2, Body: issue72State, CreatedAt: "2026-04-28T12:06:00Z"}},
	}}

	var out strings.Builder
	app := NewApp(&out, &strings.Builder{})
	app.SetIssueLifecycle(lifecycle)
	code := app.Run([]string{"status", "--worker", "issue-71", "--worker-dir", workerRoot})
	if code != 0 {
		t.Fatalf("Run() code = %d, want 0", code)
	}
	printed := out.String()
	for _, want := range []string{
		"linked-latest-state: blocked",
		"linked-current: review_feedback",
		"linked-next: resolve_review_comments",
		"linked-blockers: merge conflict in README.md",
		"linked-pr: #101",
		"batch-done: issue #72: ready-to-merge (#102)",
		"batch-linked-prs: issue #71 -> #101; issue #72 -> #102",
		"batch-conflicts: issue #71: merge conflict in README.md",
		"batch-verification: issue #72: ready-to-merge",
		"batch-failures: none",
	} {
		if !strings.Contains(printed, want) {
			t.Fatalf("stdout = %q, want %q", printed, want)
		}
	}
}

func TestStatusWorkerJSONIncludesBatchSummary(t *testing.T) {
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
	if payload.Linked == nil || payload.Linked.Target != "issue #71" {
		t.Fatalf("linked payload = %#v, want issue target", payload.Linked)
	}
	if len(payload.Batch.ChildWorkers) != 2 {
		t.Fatalf("child workers len = %d, want 2", len(payload.Batch.ChildWorkers))
	}
	if len(payload.Batch.LinkedPRs) != 0 {
		t.Fatalf("linked PRs = %#v", payload.Batch.LinkedPRs)
	}
	if len(payload.Batch.Verification) != 0 {
		t.Fatalf("verification len = %d, want 0", len(payload.Batch.Verification))
	}
	if payload.Batch.ActiveWorkers != 1 {
		t.Fatalf("active workers = %d, want 1", payload.Batch.ActiveWorkers)
	}
	if len(payload.Batch.Failures) != 0 {
		t.Fatalf("failures = %#v, want empty", payload.Batch.Failures)
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
		PushRemote:  "https://github.com/owner/repo.git",
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
	if worker.Worker.PushRemote != "https://github.com/owner/repo.git" {
		t.Fatalf("worker push remote = %q, want https://github.com/owner/repo.git", worker.Worker.PushRemote)
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
