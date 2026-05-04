package cli

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

type doctorCheck struct {
	Status string
	Name   string
	Detail string
}

type trackedStatusReport struct {
	Target      string `json:"target"`
	Repo        string `json:"repo"`
	LatestState string `json:"latest_state"`
	Branch      string `json:"branch,omitempty"`
	BaseBranch  string `json:"base_branch,omitempty"`
	Current     string `json:"current,omitempty"`
	Next        string `json:"next,omitempty"`
	Blockers    string `json:"blockers,omitempty"`
	PR          string `json:"pr,omitempty"`
	PRReadiness string `json:"pr_readiness,omitempty"`
	Updated     string `json:"updated,omitempty"`
}

type mergeQueuePlanItem struct {
	IssueNumber int    `json:"issue_number"`
	PRNumber    int    `json:"pr_number"`
	Branch      string `json:"branch,omitempty"`
	Reason      string `json:"reason"`
}

type mergeQueuePlanReport struct {
	Repo      string               `json:"repo"`
	DryRun    bool                 `json:"dry_run"`
	Eligible  []mergeQueuePlanItem `json:"eligible"`
	Skipped   []mergeQueuePlanItem `json:"skipped"`
	Generated string               `json:"generated_at"`
}

func (a *App) runDoctor(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedDoctorFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("doctor", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	_ = fs.Bool("doctor", false, "compatibility no-op; doctor mode is selected by the command")
	doctorSmokeCheck := fs.Bool("doctor-smoke-check", false, "run a lightweight runner CLI smoke check")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected doctor argument: %s\n", fs.Arg(0))
		return 2
	}

	checks := make([]doctorCheck, 0, 8)
	dir := defaultSourceDir(*opts.dir)
	repo := strings.TrimSpace(*opts.repo)
	runner := fallbackString(strings.TrimSpace(*opts.runner), "opencode")

	_, _ = fmt.Fprintln(a.out, "Doctor diagnostics")
	_, _ = fmt.Fprintf(a.out, "- Directory: %s\n", dir)
	_, _ = fmt.Fprintf(a.out, "- Runner: %s\n", runner)

	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		checks = append(checks, doctorCheck{Status: "FAIL", Name: "Repository directory", Detail: fmt.Sprintf("directory does not exist: %s", dir)})
	} else {
		if _, err := a.runGit(ctx, dir, "rev-parse", "--is-inside-work-tree"); err != nil {
			checks = append(checks, doctorCheck{Status: "FAIL", Name: "Git repository", Detail: err.Error()})
		} else {
			checks = append(checks, doctorCheck{Status: "PASS", Name: "Git repository", Detail: "inside a git work tree"})
		}
		if dirty, err := a.gitHasChanges(ctx, dir); err != nil {
			checks = append(checks, doctorCheck{Status: "FAIL", Name: "Clean worktree", Detail: err.Error()})
		} else if dirty {
			checks = append(checks, doctorCheck{Status: "FAIL", Name: "Clean worktree", Detail: "working tree has uncommitted changes"})
		} else {
			checks = append(checks, doctorCheck{Status: "PASS", Name: "Clean worktree", Detail: "working tree is clean"})
		}
	}

	if path, err := exec.LookPath("gh"); err != nil {
		checks = append(checks, doctorCheck{Status: "FAIL", Name: "GitHub CLI", Detail: "gh is not installed or not in PATH"})
	} else {
		checks = append(checks, doctorCheck{Status: "PASS", Name: "GitHub CLI", Detail: fmt.Sprintf("found at %s", path)})
		if err := a.runner.Run(ctx, "gh", "auth", "status"); err != nil {
			checks = append(checks, doctorCheck{Status: "FAIL", Name: "gh auth", Detail: "not authenticated (run gh auth login)"})
		} else {
			checks = append(checks, doctorCheck{Status: "PASS", Name: "gh auth", Detail: "authenticated"})
		}
		if repo != "" {
			if err := a.runner.Run(ctx, "gh", "repo", "view", repo); err != nil {
				checks = append(checks, doctorCheck{Status: "FAIL", Name: "Repository access", Detail: fmt.Sprintf("cannot access %s (check repo name and permissions)", repo)})
			} else {
				checks = append(checks, doctorCheck{Status: "PASS", Name: "Repository access", Detail: fmt.Sprintf("verified access to %s", repo)})
			}
		}
	}

	smokeStatus := "WARN"
	smokeDetail := "skipped (use --doctor-smoke-check to enable)"
	if *doctorSmokeCheck {
		if _, err := exec.LookPath(runner); err != nil {
			smokeStatus = "FAIL"
			smokeDetail = fmt.Sprintf("%s CLI not found", runner)
		} else {
			if err := a.runDetachedWorkerSmokeValidation(ctx, dir); err != nil {
				smokeStatus = "FAIL"
				smokeDetail = err.Error()
			} else {
				smokeStatus = "PASS"
				smokeDetail = "CLI invocation enabled; detached worker metadata smoke passed"
			}
		}
	}
	checks = append(checks, doctorCheck{Status: smokeStatus, Name: "Runner smoke check", Detail: smokeDetail})

	_, _ = fmt.Fprintln(a.out)
	passCount, warnCount, failCount := 0, 0, 0
	for _, check := range checks {
		switch check.Status {
		case "PASS":
			passCount++
		case "WARN":
			warnCount++
		case "FAIL":
			failCount++
		}
		_, _ = fmt.Fprintf(a.out, "[%s] %s: %s\n", check.Status, check.Name, check.Detail)
	}
	_, _ = fmt.Fprintln(a.out)
	_, _ = fmt.Fprintf(a.out, "Doctor summary: %d pass, %d warn, %d fail\n", passCount, warnCount, failCount)
	if failCount > 0 {
		return 1
	}
	return 0
}

func (a *App) runDetachedWorkerSmokeValidation(ctx context.Context, sourceDir string) error {
	tempRoot, err := os.MkdirTemp("", "orchestrator-doctor-smoke-")
	if err != nil {
		return fmt.Errorf("detached worker smoke setup failed: %w", err)
	}
	defer os.RemoveAll(tempRoot)

	workDir := filepath.Join(tempRoot, "repo")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return fmt.Errorf("detached worker smoke setup failed: %w", err)
	}
	if strings.TrimSpace(sourceDir) != "" {
		workDir = sourceDir
	}

	paths, err := resolveDetachedWorkerPaths(tempRoot, workDir, "issue", "1")
	if err != nil {
		return fmt.Errorf("detached worker smoke path resolution failed: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(paths.StatePath), 0o755); err != nil {
		return fmt.Errorf("detached worker smoke setup failed: %w", err)
	}
	logBody := []byte("smoke log line\n")
	if err := os.WriteFile(paths.LogPath, logBody, 0o644); err != nil {
		return fmt.Errorf("detached worker smoke setup failed: %w", err)
	}

	state := detachedWorkerState{
		Name:       "issue-1",
		Mode:       "issue",
		TargetKind: "issue",
		TargetID:   "1",
		Command:    []string{"orchestrator", "run", "issue", "--id", "1"},
		StartedAt:  "2026-01-01T00:00:00Z",
		PID:        os.Getpid(),
		LogPath:    paths.LogPath,
		StatePath:  paths.StatePath,
		WorkDir:    workDir,
	}
	if err := writeDetachedWorkerState(state); err != nil {
		return fmt.Errorf("detached worker smoke setup failed: %w", err)
	}

	report, err := a.detachedWorkerReportFromStateFileWithBatch(ctx, paths.StatePath, false)
	if err != nil {
		return fmt.Errorf("detached worker smoke status read failed: %w", err)
	}
	if report.Worker.Name != state.Name || report.Worker.TargetKind != state.TargetKind || report.Worker.TargetID != state.TargetID {
		return errors.New("detached worker smoke ownership metadata mismatch")
	}
	if strings.TrimSpace(report.ProcessStatus) == "" {
		return errors.New("detached worker smoke missing process status metadata")
	}
	if report.Log.Lines < 1 {
		return errors.New("detached worker smoke missing log status metadata")
	}
	if strings.TrimSpace(report.Next) == "" {
		return errors.New("detached worker smoke missing next action metadata")
	}
	return nil
}

func (a *App) runAutoDoctor(ctx context.Context, args []string) int {
	code := a.runDoctor(ctx, args)
	_, _ = fmt.Fprintln(a.out, "Autodoctor next steps:")
	if code == 0 {
		_, _ = fmt.Fprintln(a.out, "- Environment checks passed; run orchestrator status --issue N --repo owner/repo or orchestrator run issue --id N --repo owner/repo.")
		return 0
	}
	_, _ = fmt.Fprintln(a.out, "- Resolve FAIL checks above, then rerun: orchestrator autodoctor --repo owner/repo")
	_, _ = fmt.Fprintln(a.out, "- Common fixes: install gh CLI, run gh auth login, and clean the git worktree.")
	return code
}

func (a *App) runVerify(ctx context.Context, args []string) int {
	fs := newFlagSet("verify", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	createFollowupIssue := fs.Bool("create-followup-issue", false, a.runtime.FollowUpIssueFlagDescription("verification"))
	autonomousSessionFile := fs.String("autonomous-session-file", "", "read/write daemon batch verification status from a session checkpoint file")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected verify argument: %s\n", fs.Arg(0))
		return 2
	}

	verification, err := a.runPostBatchVerification(ctx, opts, *createFollowupIssue, strings.TrimSpace(*autonomousSessionFile))
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			_, _ = fmt.Fprintln(a.err, "orchestrator: verification timed out")
			return 124
		}
		if errors.Is(err, context.Canceled) {
			_, _ = fmt.Fprintln(a.err, "orchestrator: verification canceled")
			return 130
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: verification failed: %v\n", err)
		return 1
	}
	if strings.EqualFold(strings.TrimSpace(verification.Status), orchestration.StatusFailed) {
		return 1
	}
	return 0
}

func (a *App) runStatus(ctx context.Context, args []string) int {
	fs := newFlagSet("status", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	issue := fs.Int("issue", 0, a.runtime.IssueFlagDescription())
	pr := fs.Int("pr", 0, a.runtime.PullRequestFlagDescription())
	worker := fs.String("worker", "", "detached worker name: issue-N, pr-N, or daemon")
	workers := fs.Bool("workers", false, "list detached workers from the local registry")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")
	autonomousSessionFile := fs.String("autonomous-session-file", "", "read daemon batch status from a session checkpoint file")
	mergeQueuePlan := fs.Bool("merge-queue-plan", false, "print dry-run autonomous merge queue plan")
	planLimit := fs.Int("limit", 100, "max open issues to scan for merge queue plan")
	asJSON := fs.Bool("json", false, "print machine-readable JSON")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected status argument: %s\n", fs.Arg(0))
		return 2
	}
	targets := 0
	if *issue > 0 {
		targets++
	}
	if *pr > 0 {
		targets++
	}
	if strings.TrimSpace(*worker) != "" {
		targets++
	}
	if strings.TrimSpace(*autonomousSessionFile) != "" {
		targets++
	}
	if *mergeQueuePlan {
		targets++
	}
	if *workers {
		targets++
	}
	if targets != 1 {
		_, _ = fmt.Fprintln(a.err, "status requires exactly one of --issue N, --pr N, --worker NAME, --workers, --autonomous-session-file PATH, or --merge-queue-plan")
		return 2
	}
	if *workers {
		return a.runDetachedStatusList(ctx, *workerDir, *asJSON)
	}
	if strings.TrimSpace(*worker) != "" {
		return a.runDetachedStatus(ctx, *workerDir, *worker, *asJSON)
	}
	if strings.TrimSpace(*autonomousSessionFile) != "" {
		return a.runAutonomousSessionStatus(*autonomousSessionFile, *asJSON)
	}
	if *mergeQueuePlan {
		repo := strings.TrimSpace(*opts.repo)
		if repo == "" {
			_, _ = fmt.Fprintln(a.err, "orchestrator: status --merge-queue-plan requires --repo owner/name")
			return 2
		}
		if *planLimit <= 0 {
			_, _ = fmt.Fprintln(a.err, "orchestrator: status --merge-queue-plan requires --limit > 0")
			return 2
		}
		return a.runMergeQueuePlanStatus(ctx, repo, *planLimit, *asJSON)
	}

	repo := strings.TrimSpace(*opts.repo)
	if repo == "" {
		_, _ = fmt.Fprintln(a.err, "orchestrator: status --issue/--pr requires --repo owner/name")
		return 2
	}
	if *issue > 0 {
		return a.runIssueStatus(ctx, repo, *issue, *asJSON)
	}
	return a.runPRStatus(ctx, repo, *pr, *asJSON)
}

func (a *App) runMergeQueuePlanStatus(ctx context.Context, repo string, limit int, asJSON bool) int {
	report, err := a.mergeQueuePlanReport(ctx, repo, limit)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return 1
	}
	if asJSON {
		encoded, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode merge queue plan: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", encoded)
		return 0
	}
	_, _ = fmt.Fprintf(a.out, "Merge queue plan (dry-run): %d eligible, %d skipped\n", len(report.Eligible), len(report.Skipped))
	if len(report.Eligible) > 0 {
		_, _ = fmt.Fprintln(a.out, "Eligible:")
		for idx, item := range report.Eligible {
			_, _ = fmt.Fprintf(a.out, "  %d. issue #%d -> PR #%d (%s): %s\n", idx+1, item.IssueNumber, item.PRNumber, fallbackString(item.Branch, "branch-unknown"), item.Reason)
		}
	}
	if len(report.Skipped) > 0 {
		_, _ = fmt.Fprintln(a.out, "Skipped:")
		for _, item := range report.Skipped {
			if item.PRNumber > 0 {
				_, _ = fmt.Fprintf(a.out, "  - issue #%d -> PR #%d (%s): %s\n", item.IssueNumber, item.PRNumber, fallbackString(item.Branch, "branch-unknown"), item.Reason)
				continue
			}
			_, _ = fmt.Fprintf(a.out, "  - issue #%d: %s\n", item.IssueNumber, item.Reason)
		}
	}
	return 0
}

func (a *App) mergeQueuePlanReport(ctx context.Context, repo string, limit int) (mergeQueuePlanReport, error) {
	issues, err := a.daemon.ListIssues(ctx, repo, "open", limit)
	if err != nil {
		return mergeQueuePlanReport{}, fmt.Errorf("failed to list open issues for merge queue plan: %w", err)
	}
	report := mergeQueuePlanReport{Repo: repo, DryRun: true, Eligible: []mergeQueuePlanItem{}, Skipped: []mergeQueuePlanItem{}, Generated: nowRFC3339()}
	for _, issue := range issues {
		skip := mergeQueuePlanItem{IssueNumber: issue.Number}
		issueComments, err := a.daemon.ListIssueComments(ctx, repo, issue.Number)
		if err != nil {
			return mergeQueuePlanReport{}, fmt.Errorf("failed to list issue #%d comments: %w", issue.Number, err)
		}
		trackerIssueComments := make([]orchestration.TrackerComment, 0, len(issueComments))
		for _, comment := range issueComments {
			trackerIssueComments = append(trackerIssueComments, orchestration.TrackerComment{ID: comment.ID, CreatedAt: comment.CreatedAt, HTMLURL: comment.HTMLURL, Body: comment.Body})
		}
		issueState, _ := orchestration.SelectLatestParseableOrchestrationState(trackerIssueComments, fmt.Sprintf("issue #%d", issue.Number))
		if issueState == nil || !strings.EqualFold(strings.TrimSpace(issueState.Status), orchestration.StatusReadyToMerge) {
			skip.Reason = "not-ready-to-merge"
			report.Skipped = append(report.Skipped, skip)
			continue
		}
		linkedPR, err := a.daemon.FindOpenPullRequestForIssue(ctx, repo, issue)
		if err != nil {
			return mergeQueuePlanReport{}, fmt.Errorf("failed to find linked PR for issue #%d: %w", issue.Number, err)
		}
		if linkedPR == nil || linkedPR.Number <= 0 {
			skip.Reason = "missing-linked-pr"
			report.Skipped = append(report.Skipped, skip)
			continue
		}
		skip.PRNumber = linkedPR.Number
		skip.Branch = strings.TrimSpace(linkedPR.HeadRefName)

		prComments, err := a.daemon.ConversationCommentsForPullRequest(ctx, repo, linkedPR.Number)
		if err != nil {
			return mergeQueuePlanReport{}, fmt.Errorf("failed to list PR #%d comments: %w", linkedPR.Number, err)
		}
		trackerPRComments := make([]orchestration.TrackerComment, 0, len(prComments))
		for _, comment := range prComments {
			trackerPRComments = append(trackerPRComments, orchestration.TrackerComment{HTMLURL: comment.URL, Body: comment.Body})
		}
		prState, _ := orchestration.SelectLatestParseableOrchestrationState(trackerPRComments, fmt.Sprintf("pr #%d", linkedPR.Number))
		if prState == nil || !strings.EqualFold(strings.TrimSpace(prState.Status), orchestration.StatusReadyToMerge) {
			skip.Reason = "stale-linked-state"
			report.Skipped = append(report.Skipped, skip)
			continue
		}

		if !mergeQueueOwnershipConsistent(issue.Number, issueState.Payload, linkedPR.Number, strings.TrimSpace(linkedPR.HeadRefName)) ||
			!mergeQueueOwnershipConsistent(issue.Number, prState.Payload, linkedPR.Number, strings.TrimSpace(linkedPR.HeadRefName)) {
			skip.Reason = "stale-linked-state"
			report.Skipped = append(report.Skipped, skip)
			continue
		}

		readiness := issueState.Payload.MergeReadiness
		if readiness == nil {
			readiness = prState.Payload.MergeReadiness
		}
		if readiness == nil {
			skip.Reason = "stale-linked-state"
			report.Skipped = append(report.Skipped, skip)
			continue
		}

		policy := issueState.Payload.MergePolicy
		if policy == nil {
			policy = prState.Payload.MergePolicy
		}
		if policy != nil && !policy.Auto {
			skip.Reason = "merge-policy-disabled"
			report.Skipped = append(report.Skipped, skip)
			continue
		}

		if strings.EqualFold(strings.TrimSpace(readiness.ReviewDecision), orchestration.ReviewDecisionReviewRequired) {
			skip.Reason = "review-required"
			report.Skipped = append(report.Skipped, skip)
			continue
		}
		if strings.EqualFold(strings.TrimSpace(readiness.MergeReadinessState), orchestration.MergeReadinessConflicting) {
			skip.Reason = "merge-conflict"
			report.Skipped = append(report.Skipped, skip)
			continue
		}
		if mergeQueueHasFailingCI(issueState.Payload.CIChecks) || mergeQueueHasFailingCI(prState.Payload.CIChecks) {
			skip.Reason = "ci-failing"
			report.Skipped = append(report.Skipped, skip)
			continue
		}
		verification := readiness.MergeResultVerification
		if verification == nil || !strings.EqualFold(strings.TrimSpace(verification.Status), "passed") {
			skip.Reason = "missing-verification"
			report.Skipped = append(report.Skipped, skip)
			continue
		}

		report.Eligible = append(report.Eligible, mergeQueuePlanItem{
			IssueNumber: issue.Number,
			PRNumber:    linkedPR.Number,
			Branch:      strings.TrimSpace(linkedPR.HeadRefName),
			Reason:      "ready-to-merge with passing verification",
		})
	}
	sort.Slice(report.Eligible, func(i, j int) bool { return report.Eligible[i].IssueNumber < report.Eligible[j].IssueNumber })
	sort.Slice(report.Skipped, func(i, j int) bool { return report.Skipped[i].IssueNumber < report.Skipped[j].IssueNumber })
	return report, nil
}

func mergeQueueOwnershipConsistent(issueNumber int, state orchestration.TrackedState, prNumber int, branch string) bool {
	if state.Issue != nil && *state.Issue != issueNumber {
		return false
	}
	if state.PR != nil && *state.PR != prNumber {
		return false
	}
	if stateBranch := strings.TrimSpace(state.Branch); stateBranch != "" && branch != "" && stateBranch != branch {
		return false
	}
	return true
}

func mergeQueueHasFailingCI(checks []orchestration.PRCICheck) bool {
	for _, check := range checks {
		state := strings.ToLower(strings.TrimSpace(check.State))
		if state == "failure" || state == "failed" || state == "error" {
			return true
		}
	}
	return false
}

func nowRFC3339() string {
	return time.Now().UTC().Format(time.RFC3339)
}

func (a *App) runIssueStatus(ctx context.Context, repo string, issueNumber int, asJSON bool) int {
	report, warnings, err := a.issueStatusReport(ctx, repo, issueNumber)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return 1
	}
	for _, warning := range warnings {
		_, _ = fmt.Fprintf(a.err, "Warning: %s\n", warning)
	}
	if asJSON {
		encoded, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode issue status: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", encoded)
		return 0
	}
	_, _ = fmt.Fprintf(a.out, "Target: %s\n", report.Target)
	_, _ = fmt.Fprintf(a.out, "Latest state: %s\n", report.LatestState)
	if report.Branch != "" {
		_, _ = fmt.Fprintf(a.out, "Branch: %s\n", report.Branch)
	}
	if report.Current != "" {
		_, _ = fmt.Fprintf(a.out, "Current: %s\n", report.Current)
	}
	if report.Next != "" {
		_, _ = fmt.Fprintf(a.out, "Next: %s\n", report.Next)
	}
	if report.Blockers != "" {
		_, _ = fmt.Fprintf(a.out, "Blockers: %s\n", report.Blockers)
	}
	if report.PR != "" {
		_, _ = fmt.Fprintf(a.out, "PR: %s\n", report.PR)
	}
	if report.Updated != "" {
		_, _ = fmt.Fprintf(a.out, "Updated: %s\n", report.Updated)
	}
	return 0
}

func (a *App) issueStatusReport(ctx context.Context, repo string, issueNumber int) (trackedStatusReport, []string, error) {
	issue, err := a.issueLifecycle.FetchIssue(ctx, repo, issueNumber)
	if err != nil {
		return trackedStatusReport{}, nil, fmt.Errorf("failed to fetch issue #%d: %w", issueNumber, err)
	}
	comments, err := a.issueLifecycle.ListIssueComments(ctx, repo, issueNumber)
	if err != nil {
		return trackedStatusReport{}, nil, fmt.Errorf("failed to list issue #%d comments: %w", issueNumber, err)
	}
	trackerComments := make([]orchestration.TrackerComment, 0, len(comments))
	for _, comment := range comments {
		trackerComments = append(trackerComments, orchestration.TrackerComment{ID: comment.ID, CreatedAt: comment.CreatedAt, HTMLURL: comment.HTMLURL, Body: comment.Body})
	}
	latest, warnings := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("issue #%d", issueNumber))
	linkedPR, err := a.issueLifecycle.FindOpenPullRequestForIssue(ctx, repo, issue)
	if err != nil {
		return trackedStatusReport{}, warnings, fmt.Errorf("failed to discover linked PR for issue #%d: %w", issueNumber, err)
	}
	payload := orchestration.TrackedState{}
	if latest != nil {
		payload = latest.Payload
	}
	status := "new"
	if latest != nil && strings.TrimSpace(latest.Status) != "" {
		status = latest.Status
	}
	report := trackedStatusReport{Target: fmt.Sprintf("issue #%d", issueNumber), Repo: repo, LatestState: status, Branch: strings.TrimSpace(payload.Branch), Current: strings.TrimSpace(payload.Stage), Next: strings.TrimSpace(payload.NextAction), Blockers: strings.TrimSpace(payload.Error), PRReadiness: detachedPRReadinessSummary(payload), Updated: strings.TrimSpace(payload.Timestamp)}
	if linkedPR != nil {
		report.PR = fmt.Sprintf("#%d", linkedPR.Number)
		if report.Branch == "" {
			report.Branch = strings.TrimSpace(linkedPR.HeadRefName)
		}
	} else if payload.PR != nil && *payload.PR > 0 {
		report.PR = fmt.Sprintf("#%d", *payload.PR)
	}
	return report, warnings, nil
}

func (a *App) runPRStatus(ctx context.Context, repo string, prNumber int, asJSON bool) int {
	report, warnings, err := a.prStatusReport(ctx, repo, prNumber)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return 1
	}
	for _, warning := range warnings {
		_, _ = fmt.Fprintf(a.err, "Warning: %s\n", warning)
	}
	if asJSON {
		encoded, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode PR status: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", encoded)
		return 0
	}
	_, _ = fmt.Fprintf(a.out, "Target: %s\n", report.Target)
	_, _ = fmt.Fprintf(a.out, "Latest state: %s\n", report.LatestState)
	if report.Branch != "" {
		_, _ = fmt.Fprintf(a.out, "Branch: %s\n", report.Branch)
	}
	if report.BaseBranch != "" {
		_, _ = fmt.Fprintf(a.out, "Base branch: %s\n", report.BaseBranch)
	}
	if report.Current != "" {
		_, _ = fmt.Fprintf(a.out, "Current: %s\n", report.Current)
	}
	if report.Next != "" {
		_, _ = fmt.Fprintf(a.out, "Next: %s\n", report.Next)
	}
	if report.Blockers != "" {
		_, _ = fmt.Fprintf(a.out, "Blockers: %s\n", report.Blockers)
	}
	if report.Updated != "" {
		_, _ = fmt.Fprintf(a.out, "Updated: %s\n", report.Updated)
	}
	return 0
}

func (a *App) prStatusReport(ctx context.Context, repo string, prNumber int) (trackedStatusReport, []string, error) {
	pullRequest, err := a.prLifecycle.FetchPullRequest(ctx, repo, prNumber)
	if err != nil {
		return trackedStatusReport{}, nil, fmt.Errorf("failed to fetch PR #%d: %w", prNumber, err)
	}
	conversation, err := a.prLifecycle.ConversationCommentsForPullRequest(ctx, repo, prNumber)
	if err != nil {
		return trackedStatusReport{}, nil, fmt.Errorf("failed to list PR #%d comments: %w", prNumber, err)
	}
	trackerComments := make([]orchestration.TrackerComment, 0, len(conversation))
	for _, comment := range conversation {
		trackerComments = append(trackerComments, orchestration.TrackerComment{HTMLURL: comment.URL, Body: comment.Body})
	}
	latest, warnings := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("pr #%d", prNumber))
	payload := orchestration.TrackedState{}
	if latest != nil {
		payload = latest.Payload
	}
	status := "new"
	if latest != nil && strings.TrimSpace(latest.Status) != "" {
		status = latest.Status
	}
	if payload.Branch == "" {
		payload.Branch = strings.TrimSpace(pullRequest.HeadRefName)
	}
	if payload.BaseBranch == "" {
		payload.BaseBranch = strings.TrimSpace(pullRequest.BaseRefName)
	}
	prRef := fmt.Sprintf("#%d", pullRequest.Number)
	if payload.PR != nil && *payload.PR > 0 {
		prRef = fmt.Sprintf("#%d", *payload.PR)
	}
	return trackedStatusReport{Target: fmt.Sprintf("pr #%d", prNumber), Repo: repo, LatestState: status, Branch: strings.TrimSpace(payload.Branch), BaseBranch: strings.TrimSpace(payload.BaseBranch), Current: strings.TrimSpace(payload.Stage), Next: strings.TrimSpace(payload.NextAction), Blockers: strings.TrimSpace(payload.Error), PR: prRef, PRReadiness: detachedPRReadinessSummary(payload), Updated: strings.TrimSpace(payload.Timestamp)}, warnings, nil
}

func newFlagSet(name string, err io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(err)
	return fs
}
