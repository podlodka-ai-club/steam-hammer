package githublifecycle

import (
	"context"
	"errors"
	"reflect"
	"testing"
)

type fakeGHCLI struct {
	captureCalls [][]string
	runCalls     [][]string
	outputs      []string
	captureErr   error
	runErr       error
}

func (f *fakeGHCLI) Capture(_ context.Context, args ...string) (string, error) {
	f.captureCalls = append(f.captureCalls, append([]string(nil), args...))
	if f.captureErr != nil {
		return "", f.captureErr
	}
	if len(f.outputs) == 0 {
		return "", nil
	}
	output := f.outputs[0]
	f.outputs = f.outputs[1:]
	return output, nil
}

func (f *fakeGHCLI) Run(_ context.Context, args ...string) error {
	f.runCalls = append(f.runCalls, append([]string(nil), args...))
	return f.runErr
}

func TestFetchIssuePreservesGitHubTrackerBoundary(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`{"number":71,"title":"Fix runner","body":"Body","url":"https://example.test/issues/71","state":"OPEN"}`}}
	adapter := NewAdapter(gh)

	issue, err := adapter.FetchIssue(context.Background(), "owner/repo", 71)
	if err != nil {
		t.Fatalf("FetchIssue() error = %v", err)
	}
	if issue.Tracker != TrackerGitHub {
		t.Fatalf("Issue.Tracker = %q, want %q", issue.Tracker, TrackerGitHub)
	}
	if issue.Number != 71 || issue.Title != "Fix runner" {
		t.Fatalf("FetchIssue() = %#v", issue)
	}
	want := []string{"issue", "view", "71", "--repo", "owner/repo", "--json", "number,title,body,url,state,labels,author,assignees,createdAt,updatedAt"}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("FetchIssue command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestDefaultBranchUsesCurrentGitHubRepoView(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`{"defaultBranchRef":{"name":"main"}}`}}
	adapter := NewAdapter(gh)

	branch, err := adapter.DefaultBranch(context.Background(), "owner/repo")
	if err != nil {
		t.Fatalf("DefaultBranch() error = %v", err)
	}
	if branch != "main" {
		t.Fatalf("DefaultBranch() = %q, want main", branch)
	}
	want := []string{"repo", "view", "owner/repo", "--json", "defaultBranchRef"}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("DefaultBranch command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestListIssuesPreservesGitHubTrackerBoundary(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`[{"number":71,"title":"Fix runner"},{"number":72,"title":"Fix CI"}]`}}
	adapter := NewAdapter(gh)

	issues, err := adapter.ListIssues(context.Background(), "owner/repo", "open", 2)
	if err != nil {
		t.Fatalf("ListIssues() error = %v", err)
	}
	if len(issues) != 2 {
		t.Fatalf("len(ListIssues()) = %d, want 2", len(issues))
	}
	for _, issue := range issues {
		if issue.Tracker != TrackerGitHub {
			t.Fatalf("Issue.Tracker = %q, want %q", issue.Tracker, TrackerGitHub)
		}
	}
}

func TestCommentMethodsUseCurrentGitHubCommands(t *testing.T) {
	gh := &fakeGHCLI{}
	adapter := NewAdapter(gh)

	if err := adapter.CommentOnIssue(context.Background(), "owner/repo", 71, "issue comment"); err != nil {
		t.Fatalf("CommentOnIssue() error = %v", err)
	}
	if err := adapter.CommentOnPullRequest(context.Background(), "owner/repo", 101, "pr comment"); err != nil {
		t.Fatalf("CommentOnPullRequest() error = %v", err)
	}

	want := [][]string{
		{"issue", "comment", "71", "--repo", "owner/repo", "--body", "issue comment"},
		{"pr", "comment", "101", "--repo", "owner/repo", "--body", "pr comment"},
	}
	if !reflect.DeepEqual(gh.runCalls, want) {
		t.Fatalf("Run calls = %#v, want %#v", gh.runCalls, want)
	}
}

func TestCreateIssueUsesCurrentGitHubAPI(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`{"number":164,"title":"Verification failed","body":"Please fix it.","url":"https://github.com/owner/repo/issues/164","state":"OPEN"}`}}
	adapter := NewAdapter(gh)

	issue, err := adapter.CreateIssue(context.Background(), CreateIssueRequest{
		Repo:  "owner/repo",
		Title: "Verification failed",
		Body:  "Please fix it.",
	})
	if err != nil {
		t.Fatalf("CreateIssue() error = %v", err)
	}
	if issue.Number != 164 || issue.URL != "https://github.com/owner/repo/issues/164" {
		t.Fatalf("CreateIssue() = %#v", issue)
	}
	if issue.Tracker != TrackerGitHub {
		t.Fatalf("Issue.Tracker = %q, want %q", issue.Tracker, TrackerGitHub)
	}
	want := []string{"api", "repos/owner/repo/issues", "--method", "POST", "-H", "Accept: application/vnd.github+json", "-f", "title=Verification failed", "-f", "body=Please fix it."}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("CreateIssue command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestListIssueCommentsUsesCurrentGitHubAPI(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`[{"id":1,"body":"comment","html_url":"https://example.test/comment/1","created_at":"2026-05-01T10:00:00Z","user":{"login":"reviewer"}}]`}}
	adapter := NewAdapter(gh)

	comments, err := adapter.ListIssueComments(context.Background(), "owner/repo", 71)
	if err != nil {
		t.Fatalf("ListIssueComments() error = %v", err)
	}
	if len(comments) != 1 {
		t.Fatalf("len(ListIssueComments()) = %d, want 1", len(comments))
	}
	if comments[0].ID != 1 || comments[0].Body != "comment" {
		t.Fatalf("ListIssueComments() = %#v", comments)
	}
	if comments[0].User.Login != "reviewer" {
		t.Fatalf("ListIssueComments() user = %#v", comments[0].User)
	}
	want := []string{"api", "repos/owner/repo/issues/71/comments", "--method", "GET", "-H", "Accept: application/vnd.github+json", "-f", "per_page=100"}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("ListIssueComments command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestReviewThreadsForPullRequestUsesCurrentGitHubGraphQL(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"comments":{"nodes":[{"body":"Please update this","path":"internal/core/orchestration/session.go","line":42,"outdated":false,"url":"https://example.test/comment/7","author":{"login":"reviewer"}}]}}]}}}}}`}}
	adapter := NewAdapter(gh)

	threads, err := adapter.ReviewThreadsForPullRequest(context.Background(), "owner/repo", 101)
	if err != nil {
		t.Fatalf("ReviewThreadsForPullRequest() error = %v", err)
	}
	if len(threads) != 1 || len(threads[0].Comments) != 1 {
		t.Fatalf("threads = %#v", threads)
	}
	comment := threads[0].Comments[0]
	if comment.Author == nil || comment.Author.Login != "reviewer" || comment.Path != "internal/core/orchestration/session.go" || comment.Line != 42 {
		t.Fatalf("comment = %#v", comment)
	}
	want := []string{"api", "graphql"}
	if len(gh.captureCalls) != 1 || len(gh.captureCalls[0]) < len(want) || !reflect.DeepEqual(gh.captureCalls[0][:len(want)], want) {
		t.Fatalf("Capture call prefix = %#v, want %#v", gh.captureCalls, want)
	}
}

func TestConversationCommentsForPullRequestNormalizesIssueComments(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`[{"id":1,"body":"Please follow up","html_url":"https://example.test/comment/1","created_at":"2026-05-01T10:00:00Z","user":{"login":"reviewer"}}]`}}
	adapter := NewAdapter(gh)

	comments, err := adapter.ConversationCommentsForPullRequest(context.Background(), "owner/repo", 101)
	if err != nil {
		t.Fatalf("ConversationCommentsForPullRequest() error = %v", err)
	}
	if len(comments) != 1 || comments[0].Author != "reviewer" || comments[0].Body != "Please follow up" {
		t.Fatalf("comments = %#v", comments)
	}
}

func TestFetchPullRequestUsesCurrentGitHubFields(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`{"number":101,"title":"Fix runner","headRefOid":"abc123","mergeStateStatus":"CLEAN"}`}}
	adapter := NewAdapter(gh)

	pr, err := adapter.FetchPullRequest(context.Background(), "owner/repo", 101)
	if err != nil {
		t.Fatalf("FetchPullRequest() error = %v", err)
	}
	if pr.Number != 101 || pr.HeadRefOID != "abc123" {
		t.Fatalf("FetchPullRequest() = %#v", pr)
	}
	want := []string{"pr", "view", "101", "--repo", "owner/repo", "--json", "number,title,body,url,state,mergeStateStatus,mergeable,isDraft,reviewDecision,headRefName,headRefOid,baseRefName,author,closingIssuesReferences,reviews,files"}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("FetchPullRequest command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestFindOpenPullRequestForIssueUsesCurrentGitHubFields(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{`[{"number":101,"title":"Fix issue #71","url":"https://github.com/owner/repo/pull/101","headRefName":"issue-fix/71-fix-runner","closingIssuesReferences":[{"number":71}]}]`}}
	adapter := NewAdapter(gh)

	pr, err := adapter.FindOpenPullRequestForIssue(context.Background(), "owner/repo", Issue{Number: 71, Title: "Fix runner"})
	if err != nil {
		t.Fatalf("FindOpenPullRequestForIssue() error = %v", err)
	}
	if pr == nil || pr.Number != 101 {
		t.Fatalf("FindOpenPullRequestForIssue() = %#v, want PR #101", pr)
	}
	want := []string{"pr", "list", "--repo", "owner/repo", "--state", "open", "--limit", "100", "--json", "number,title,url,body,headRefName,baseRefName,closingIssuesReferences"}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("FindOpenPullRequestForIssue command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestCreatePullRequestBuildsCurrentGitHubBody(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{"https://github.com/owner/repo/pull/101\n"}}
	adapter := NewAdapter(gh)

	url, err := adapter.CreatePullRequest(context.Background(), CreatePullRequestRequest{
		Repo:               "owner/repo",
		BaseBranch:         "feature/stack-parent",
		HeadBranch:         "issue-fix/42-runner-refactor",
		Title:              "Fix runner",
		IssueRef:           "#42",
		IssueURL:           "https://example.test/issues/42",
		CloseLinkedIssue:   true,
		StackedBaseContext: "feature/stack-parent",
	})
	if err != nil {
		t.Fatalf("CreatePullRequest() error = %v", err)
	}
	if url != "https://github.com/owner/repo/pull/101" {
		t.Fatalf("CreatePullRequest() url = %q", url)
	}
	wantBody := "## Summary\n- Implements fix for #42\n- Source issue: https://example.test/issues/42\n\nCloses #42\n\n## Stack Context\n- Stacked on current branch: `feature/stack-parent`\n- Base for this PR is `feature/stack-parent` (not repository default branch)\n"
	want := []string{"pr", "create", "--repo", "owner/repo", "--base", "feature/stack-parent", "--head", "issue-fix/42-runner-refactor", "--title", "Fix runner", "--body", wantBody}
	if !reflect.DeepEqual(gh.captureCalls[0], want) {
		t.Fatalf("CreatePullRequest command = %#v, want %#v", gh.captureCalls[0], want)
	}
}

func TestCreatePullRequestDryRunSkipsGitHubCall(t *testing.T) {
	gh := &fakeGHCLI{}
	adapter := NewAdapter(gh)

	url, err := adapter.CreatePullRequest(context.Background(), CreatePullRequestRequest{
		Repo:             "owner/repo",
		BaseBranch:       "main",
		HeadBranch:       "issue-fix/42-runner-refactor",
		Title:            "Fix runner",
		IssueRef:         "#42",
		IssueURL:         "https://example.test/issues/42",
		CloseLinkedIssue: true,
		DryRun:           true,
	})
	if err != nil {
		t.Fatalf("CreatePullRequest() error = %v", err)
	}
	if url != "" {
		t.Fatalf("CreatePullRequest() url = %q, want empty", url)
	}
	if len(gh.captureCalls) != 0 {
		t.Fatalf("Capture calls = %#v, want none", gh.captureCalls)
	}
}

func TestReadinessForPullRequestNormalizesCurrentGitHubChecks(t *testing.T) {
	gh := &fakeGHCLI{outputs: []string{
		`{"check_runs":[{"id":1,"name":"ci / test","status":"completed","conclusion":"success","details_url":"https://example.test/check/1"},{"id":2,"name":"ci / lint","status":"queued","conclusion":"","html_url":"https://example.test/check/2"}]}`,
		`{"statuses":[{"context":"coverage","state":"failure","target_url":"https://example.test/status/1"}]}`,
	}}
	adapter := NewAdapter(gh)

	readiness, err := adapter.ReadinessForPullRequest(context.Background(), "owner/repo", PullRequest{Number: 101, HeadRefOID: "abc123"})
	if err != nil {
		t.Fatalf("ReadinessForPullRequest() error = %v", err)
	}
	if readiness.Overall != checkStateFailure {
		t.Fatalf("Overall = %q, want %q", readiness.Overall, checkStateFailure)
	}
	if !readiness.HasChecks {
		t.Fatalf("HasChecks = false, want true")
	}
	if len(readiness.PendingChecks) != 1 || readiness.PendingChecks[0].Name != "ci / lint" {
		t.Fatalf("PendingChecks = %#v", readiness.PendingChecks)
	}
	if len(readiness.FailingChecks) != 1 || readiness.FailingChecks[0].Name != "coverage" {
		t.Fatalf("FailingChecks = %#v", readiness.FailingChecks)
	}
	want := [][]string{
		{"api", "repos/owner/repo/commits/abc123/check-runs", "--method", "GET", "-H", "Accept: application/vnd.github+json", "-f", "per_page=100"},
		{"api", "repos/owner/repo/commits/abc123/status", "--method", "GET", "-H", "Accept: application/vnd.github+json"},
	}
	if !reflect.DeepEqual(gh.captureCalls, want) {
		t.Fatalf("Capture calls = %#v, want %#v", gh.captureCalls, want)
	}
}

func TestReadinessForPullRequestRequiresHeadSHA(t *testing.T) {
	adapter := NewAdapter(&fakeGHCLI{})

	_, err := adapter.ReadinessForPullRequest(context.Background(), "owner/repo", PullRequest{Number: 101})
	if err == nil {
		t.Fatal("ReadinessForPullRequest() error = nil, want error")
	}
	if got := err.Error(); got != "unable to read CI status for PR #101: missing headRefOid in PR payload" {
		t.Fatalf("ReadinessForPullRequest() error = %q", got)
	}
}

func TestAdapterPropagatesCLIError(t *testing.T) {
	wantErr := errors.New("boom")
	gh := &fakeGHCLI{captureErr: wantErr}
	adapter := NewAdapter(gh)

	_, err := adapter.FetchIssue(context.Background(), "owner/repo", 71)
	if !errors.Is(err, wantErr) {
		t.Fatalf("FetchIssue() error = %v, want wrapped %v", err, wantErr)
	}
}
