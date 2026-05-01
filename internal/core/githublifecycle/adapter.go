package githublifecycle

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

const TrackerGitHub = "github"

const (
	checkStatePending = "pending"
	checkStateFailure = "failure"
	checkStateSuccess = "success"
)

var (
	pendingCheckRunStatuses           = map[string]struct{}{"queued": {}, "in_progress": {}, "requested": {}, "waiting": {}, "pending": {}}
	failureCheckConclusions           = map[string]struct{}{"action_required": {}, "cancelled": {}, "failure": {}, "neutral": {}, "skipped": {}, "stale": {}, "startup_failure": {}, "timed_out": {}}
	successCheckConclusions           = map[string]struct{}{"success": {}}
	failureCommitStates               = map[string]struct{}{"error": {}, "failure": {}}
	_                       Lifecycle = (*Adapter)(nil)
)

// Lifecycle keeps the current GitHub-backed issue and PR operations behind an
// explicit Go boundary so the orchestration core can depend on interfaces.
type Lifecycle interface {
	IssueLifecycle
	PullRequestLifecycle
}

type IssueLifecycle interface {
	FetchIssue(ctx context.Context, repo string, number int) (Issue, error)
	ListIssues(ctx context.Context, repo, state string, limit int) ([]Issue, error)
	CommentOnIssue(ctx context.Context, repo string, number int, body string) error
}

type PullRequestLifecycle interface {
	FetchPullRequest(ctx context.Context, repo string, number int) (PullRequest, error)
	CreatePullRequest(ctx context.Context, req CreatePullRequestRequest) (string, error)
	CommentOnPullRequest(ctx context.Context, repo string, number int, body string) error
	ReadinessForPullRequest(ctx context.Context, repo string, pr PullRequest) (PullRequestReadiness, error)
}

type GHCLI interface {
	Capture(ctx context.Context, args ...string) (string, error)
	Run(ctx context.Context, args ...string) error
}

type ExecGHCLI struct{}

func (ExecGHCLI) Capture(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "gh", args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("gh %s failed: %w%s", strings.Join(args, " "), err, formatCommandOutput(output))
	}
	return string(output), nil
}

func (ExecGHCLI) Run(ctx context.Context, args ...string) error {
	cmd := exec.CommandContext(ctx, "gh", args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("gh %s failed: %w%s", strings.Join(args, " "), err, formatCommandOutput(output))
	}
	return nil
}

type Adapter struct {
	gh GHCLI
}

func NewAdapter(gh GHCLI) *Adapter {
	if gh == nil {
		gh = ExecGHCLI{}
	}
	return &Adapter{gh: gh}
}

type Actor struct {
	Login string `json:"login,omitempty"`
}

type Label struct {
	Name string `json:"name,omitempty"`
}

type Issue struct {
	Number    int     `json:"number,omitempty"`
	Title     string  `json:"title,omitempty"`
	Body      string  `json:"body,omitempty"`
	URL       string  `json:"url,omitempty"`
	State     string  `json:"state,omitempty"`
	Tracker   string  `json:"tracker,omitempty"`
	Labels    []Label `json:"labels,omitempty"`
	Author    *Actor  `json:"author,omitempty"`
	Assignees []Actor `json:"assignees,omitempty"`
	CreatedAt string  `json:"createdAt,omitempty"`
	UpdatedAt string  `json:"updatedAt,omitempty"`
}

type PullRequest struct {
	Number                  int                      `json:"number,omitempty"`
	Title                   string                   `json:"title,omitempty"`
	Body                    string                   `json:"body,omitempty"`
	URL                     string                   `json:"url,omitempty"`
	State                   string                   `json:"state,omitempty"`
	MergeStateStatus        string                   `json:"mergeStateStatus,omitempty"`
	Mergeable               string                   `json:"mergeable,omitempty"`
	IsDraft                 bool                     `json:"isDraft,omitempty"`
	ReviewDecision          string                   `json:"reviewDecision,omitempty"`
	HeadRefName             string                   `json:"headRefName,omitempty"`
	HeadRefOID              string                   `json:"headRefOid,omitempty"`
	BaseRefName             string                   `json:"baseRefName,omitempty"`
	Author                  *Actor                   `json:"author,omitempty"`
	ClosingIssuesReferences []IssueReference         `json:"closingIssuesReferences,omitempty"`
	Reviews                 []PullRequestReview      `json:"reviews,omitempty"`
	Files                   []PullRequestChangedFile `json:"files,omitempty"`
}

type IssueReference struct {
	Number int `json:"number,omitempty"`
}

type PullRequestReview struct {
	Author      *Actor `json:"author,omitempty"`
	AuthorLogin string `json:"authorLogin,omitempty"`
	State       string `json:"state,omitempty"`
	SubmittedAt string `json:"submittedAt,omitempty"`
}

type PullRequestChangedFile struct {
	Path string `json:"path,omitempty"`
}

type CreatePullRequestRequest struct {
	Repo               string
	BaseBranch         string
	HeadBranch         string
	Title              string
	IssueRef           string
	IssueURL           string
	CloseLinkedIssue   bool
	StackedBaseContext string
	DryRun             bool
}

type PullRequestCheck struct {
	Source     string `json:"source,omitempty"`
	ID         any    `json:"id,omitempty"`
	Name       string `json:"name,omitempty"`
	URL        string `json:"url,omitempty"`
	HTMLURL    string `json:"html_url,omitempty"`
	Status     string `json:"status,omitempty"`
	Conclusion string `json:"conclusion,omitempty"`
	State      string `json:"state,omitempty"`
}

type PullRequestReadiness struct {
	HeadSHA       string             `json:"head_sha,omitempty"`
	Overall       string             `json:"overall,omitempty"`
	HasChecks     bool               `json:"has_checks,omitempty"`
	Checks        []PullRequestCheck `json:"checks,omitempty"`
	PendingChecks []PullRequestCheck `json:"pending_checks,omitempty"`
	FailingChecks []PullRequestCheck `json:"failing_checks,omitempty"`
}

func (a *Adapter) FetchIssue(ctx context.Context, repo string, number int) (Issue, error) {
	output, err := a.gh.Capture(ctx,
		"issue", "view", fmt.Sprintf("%d", number),
		"--repo", repo,
		"--json", "number,title,body,url,state,labels,author,assignees,createdAt,updatedAt",
	)
	if err != nil {
		return Issue{}, err
	}
	var issue Issue
	if err := decodeJSONObject(output, &issue); err != nil {
		return Issue{}, fmt.Errorf("unexpected response fetching issue #%d: %w", number, err)
	}
	issue.Tracker = TrackerGitHub
	return issue, nil
}

func (a *Adapter) ListIssues(ctx context.Context, repo, state string, limit int) ([]Issue, error) {
	output, err := a.gh.Capture(ctx,
		"issue", "list",
		"--repo", repo,
		"--state", state,
		"--limit", fmt.Sprintf("%d", limit),
		"--json", "number,title,body,url,state,labels,author,assignees,createdAt,updatedAt",
	)
	if err != nil {
		return nil, err
	}
	var issues []Issue
	if err := decodeJSONArray(output, &issues); err != nil {
		return nil, fmt.Errorf("unexpected response from gh issue list: %w", err)
	}
	for i := range issues {
		issues[i].Tracker = TrackerGitHub
	}
	return issues, nil
}

func (a *Adapter) CommentOnIssue(ctx context.Context, repo string, number int, body string) error {
	return a.gh.Run(ctx,
		"issue", "comment", fmt.Sprintf("%d", number),
		"--repo", repo,
		"--body", body,
	)
}

func (a *Adapter) FetchPullRequest(ctx context.Context, repo string, number int) (PullRequest, error) {
	output, err := a.gh.Capture(ctx,
		"pr", "view", fmt.Sprintf("%d", number),
		"--repo", repo,
		"--json", "number,title,body,url,state,mergeStateStatus,mergeable,isDraft,reviewDecision,headRefName,headRefOid,baseRefName,author,closingIssuesReferences,reviews,files",
	)
	if err != nil {
		return PullRequest{}, err
	}
	var pr PullRequest
	if err := decodeJSONObject(output, &pr); err != nil {
		return PullRequest{}, fmt.Errorf("unexpected response fetching PR #%d: %w", number, err)
	}
	return pr, nil
}

func (a *Adapter) CreatePullRequest(ctx context.Context, req CreatePullRequestRequest) (string, error) {
	if strings.TrimSpace(req.Repo) == "" {
		return "", errors.New("create pull request requires repo")
	}
	if strings.TrimSpace(req.BaseBranch) == "" {
		return "", errors.New("create pull request requires base branch")
	}
	if strings.TrimSpace(req.HeadBranch) == "" {
		return "", errors.New("create pull request requires head branch")
	}
	if strings.TrimSpace(req.Title) == "" {
		return "", errors.New("create pull request requires title")
	}
	if strings.TrimSpace(req.IssueRef) == "" {
		return "", errors.New("create pull request requires issue ref")
	}
	if strings.TrimSpace(req.IssueURL) == "" {
		return "", errors.New("create pull request requires issue url")
	}

	body := buildPullRequestBody(req)
	if req.DryRun {
		return "", nil
	}

	output, err := a.gh.Capture(ctx,
		"pr", "create",
		"--repo", req.Repo,
		"--base", req.BaseBranch,
		"--head", req.HeadBranch,
		"--title", req.Title,
		"--body", body,
	)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(output), nil
}

func (a *Adapter) CommentOnPullRequest(ctx context.Context, repo string, number int, body string) error {
	return a.gh.Run(ctx,
		"pr", "comment", fmt.Sprintf("%d", number),
		"--repo", repo,
		"--body", body,
	)
}

func (a *Adapter) ReadinessForPullRequest(ctx context.Context, repo string, pr PullRequest) (PullRequestReadiness, error) {
	headSHA := strings.TrimSpace(pr.HeadRefOID)
	if headSHA == "" {
		return PullRequestReadiness{}, fmt.Errorf("unable to read CI status for PR #%d: missing headRefOid in PR payload", pr.Number)
	}

	checkRuns, err := a.fetchCommitCheckRuns(ctx, repo, headSHA)
	if err != nil {
		return PullRequestReadiness{}, err
	}
	statusContexts, err := a.fetchCommitStatusContexts(ctx, repo, headSHA)
	if err != nil {
		return PullRequestReadiness{}, err
	}

	checks := normalizeCheckRuns(checkRuns)
	checks = append(checks, normalizeStatusContexts(statusContexts)...)

	pendingChecks := filterChecksByState(checks, checkStatePending)
	failingChecks := filterChecksByState(checks, checkStateFailure)

	overall := checkStateSuccess
	switch {
	case len(checks) == 0:
		overall = checkStateSuccess
	case len(failingChecks) > 0:
		overall = checkStateFailure
	case len(pendingChecks) > 0:
		overall = checkStatePending
	}

	return PullRequestReadiness{
		HeadSHA:       headSHA,
		Overall:       overall,
		HasChecks:     len(checks) > 0,
		Checks:        checks,
		PendingChecks: pendingChecks,
		FailingChecks: failingChecks,
	}, nil
}

type commitCheckRunsPayload struct {
	CheckRuns []struct {
		ID         any    `json:"id"`
		Name       string `json:"name"`
		Status     string `json:"status"`
		Conclusion string `json:"conclusion"`
		DetailsURL string `json:"details_url"`
		HTMLURL    string `json:"html_url"`
	} `json:"check_runs"`
}

type commitStatusPayload struct {
	Statuses []struct {
		Context   string `json:"context"`
		State     string `json:"state"`
		TargetURL string `json:"target_url"`
	} `json:"statuses"`
}

func (a *Adapter) fetchCommitCheckRuns(ctx context.Context, repo, headSHA string) ([]PullRequestCheck, error) {
	output, err := a.gh.Capture(ctx,
		"api", fmt.Sprintf("repos/%s/commits/%s/check-runs", repo, headSHA),
		"--method", "GET",
		"-H", "Accept: application/vnd.github+json",
		"-f", "per_page=100",
	)
	if err != nil {
		return nil, err
	}
	var payload commitCheckRunsPayload
	if err := decodeJSONObject(output, &payload); err != nil {
		return nil, fmt.Errorf("unexpected response from gh api while fetching commit check runs: %w", err)
	}
	return normalizeCheckRunsPayload(payload), nil
}

func (a *Adapter) fetchCommitStatusContexts(ctx context.Context, repo, headSHA string) ([]PullRequestCheck, error) {
	output, err := a.gh.Capture(ctx,
		"api", fmt.Sprintf("repos/%s/commits/%s/status", repo, headSHA),
		"--method", "GET",
		"-H", "Accept: application/vnd.github+json",
	)
	if err != nil {
		return nil, err
	}
	var payload commitStatusPayload
	if err := decodeJSONObject(output, &payload); err != nil {
		return nil, fmt.Errorf("unexpected response from gh api while fetching commit status: %w", err)
	}
	return normalizeStatusContextsPayload(payload), nil
}

func normalizeCheckRuns(checks []PullRequestCheck) []PullRequestCheck {
	return append([]PullRequestCheck(nil), checks...)
}

func normalizeStatusContexts(checks []PullRequestCheck) []PullRequestCheck {
	return append([]PullRequestCheck(nil), checks...)
}

func normalizeCheckRunsPayload(payload commitCheckRunsPayload) []PullRequestCheck {
	normalized := make([]PullRequestCheck, 0, len(payload.CheckRuns))
	for _, checkRun := range payload.CheckRuns {
		status := strings.ToLower(strings.TrimSpace(checkRun.Status))
		conclusion := strings.ToLower(strings.TrimSpace(checkRun.Conclusion))
		state := checkStateFailure
		if _, ok := pendingCheckRunStatuses[status]; ok || status != "completed" {
			state = checkStatePending
		} else if _, ok := failureCheckConclusions[conclusion]; ok {
			state = checkStateFailure
		} else if _, ok := successCheckConclusions[conclusion]; ok {
			state = checkStateSuccess
		}

		url := strings.TrimSpace(checkRun.DetailsURL)
		if url == "" {
			url = strings.TrimSpace(checkRun.HTMLURL)
		}

		normalized = append(normalized, PullRequestCheck{
			Source:     "check-run",
			ID:         checkRun.ID,
			Name:       fallbackString(strings.TrimSpace(checkRun.Name), "check-run"),
			URL:        url,
			HTMLURL:    strings.TrimSpace(checkRun.HTMLURL),
			Status:     status,
			Conclusion: zeroIfEmpty(conclusion),
			State:      state,
		})
	}
	return normalized
}

func normalizeStatusContextsPayload(payload commitStatusPayload) []PullRequestCheck {
	normalized := make([]PullRequestCheck, 0, len(payload.Statuses))
	for _, context := range payload.Statuses {
		status := strings.ToLower(strings.TrimSpace(context.State))
		state := checkStatePending
		if status == checkStatePending {
			state = checkStatePending
		} else if _, ok := failureCommitStates[status]; ok {
			state = checkStateFailure
		} else if status == checkStateSuccess {
			state = checkStateSuccess
		}

		normalized = append(normalized, PullRequestCheck{
			Source: "status-context",
			Name:   fallbackString(strings.TrimSpace(context.Context), "status-context"),
			URL:    strings.TrimSpace(context.TargetURL),
			Status: status,
			State:  state,
		})
	}
	return normalized
}

func filterChecksByState(checks []PullRequestCheck, state string) []PullRequestCheck {
	filtered := make([]PullRequestCheck, 0, len(checks))
	for _, check := range checks {
		if check.State == state {
			filtered = append(filtered, check)
		}
	}
	return filtered
}

func buildPullRequestBody(req CreatePullRequestRequest) string {
	body := strings.Builder{}
	body.WriteString("## Summary\n")
	body.WriteString("- Implements fix for ")
	body.WriteString(req.IssueRef)
	body.WriteString("\n")
	body.WriteString("- Source issue: ")
	body.WriteString(req.IssueURL)
	body.WriteString("\n\n")
	if req.CloseLinkedIssue {
		body.WriteString("Closes ")
		body.WriteString(req.IssueRef)
		body.WriteString("\n")
	}
	if strings.TrimSpace(req.StackedBaseContext) != "" {
		body.WriteString("\n## Stack Context\n")
		body.WriteString("- Stacked on current branch: `")
		body.WriteString(req.StackedBaseContext)
		body.WriteString("`\n")
		body.WriteString("- Base for this PR is `")
		body.WriteString(req.StackedBaseContext)
		body.WriteString("` (not repository default branch)\n")
	}
	return body.String()
}

func decodeJSONObject(output string, target any) error {
	data := []byte(strings.TrimSpace(output))
	if len(data) == 0 || data[0] != '{' {
		return errors.New("expected JSON object")
	}
	if err := json.Unmarshal(data, target); err != nil {
		return err
	}
	return nil
}

func decodeJSONArray(output string, target any) error {
	data := []byte(strings.TrimSpace(output))
	if len(data) == 0 || data[0] != '[' {
		return errors.New("expected JSON array")
	}
	if err := json.Unmarshal(data, target); err != nil {
		return err
	}
	return nil
}

func formatCommandOutput(output []byte) string {
	trimmed := strings.TrimSpace(string(output))
	if trimmed == "" {
		return ""
	}
	return ": " + trimmed
}

func fallbackString(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func zeroIfEmpty(value string) string {
	if value == "" {
		return ""
	}
	return value
}
