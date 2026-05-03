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
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

type doctorCheck struct {
	Status string
	Name   string
	Detail string
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
			checks = append(checks, doctorCheck{Status: "PASS", Name: "Repository access", Detail: fmt.Sprintf("requested repository: %s", repo)})
		}
	}

	smokeStatus := "WARN"
	smokeDetail := "skipped (use --doctor-smoke-check to enable)"
	if *doctorSmokeCheck {
		if _, err := exec.LookPath(runner); err != nil {
			smokeStatus = "FAIL"
			smokeDetail = fmt.Sprintf("%s CLI not found", runner)
		} else {
			smokeStatus = "PASS"
			smokeDetail = "CLI invocation enabled"
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

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected verify argument: %s\n", fs.Arg(0))
		return 2
	}

	verification, err := a.runPostBatchVerification(ctx, opts, *createFollowupIssue, "")
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
	if *workers {
		targets++
	}
	if targets != 1 {
		_, _ = fmt.Fprintln(a.err, "status requires exactly one of --issue N, --pr N, --worker NAME, --workers, or --autonomous-session-file PATH")
		return 2
	}
	if *workers {
		return a.runDetachedStatusList(*workerDir, *asJSON)
	}
	if strings.TrimSpace(*worker) != "" {
		return a.runDetachedStatus(*workerDir, *worker, *asJSON)
	}
	if strings.TrimSpace(*autonomousSessionFile) != "" {
		return a.runAutonomousSessionStatus(*autonomousSessionFile, *asJSON)
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

func (a *App) runIssueStatus(ctx context.Context, repo string, issueNumber int, asJSON bool) int {
	issue, err := a.issueLifecycle.FetchIssue(ctx, repo, issueNumber)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to fetch issue #%d: %v\n", issueNumber, err)
		return 1
	}
	comments, err := a.issueLifecycle.ListIssueComments(ctx, repo, issueNumber)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to list issue #%d comments: %v\n", issueNumber, err)
		return 1
	}
	trackerComments := make([]orchestration.TrackerComment, 0, len(comments))
	for _, comment := range comments {
		trackerComments = append(trackerComments, orchestration.TrackerComment{ID: comment.ID, CreatedAt: comment.CreatedAt, HTMLURL: comment.HTMLURL, Body: comment.Body})
	}
	latest, warnings := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("issue #%d", issueNumber))
	for _, warning := range warnings {
		_, _ = fmt.Fprintf(a.err, "Warning: %s\n", warning)
	}
	linkedPR, err := a.issueLifecycle.FindOpenPullRequestForIssue(ctx, repo, issue)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to discover linked PR for issue #%d: %v\n", issueNumber, err)
		return 1
	}
	payload := orchestration.TrackedState{}
	if latest != nil {
		payload = latest.Payload
	}
	status := "new"
	if latest != nil && strings.TrimSpace(latest.Status) != "" {
		status = latest.Status
	}
	if asJSON {
		data := map[string]any{"target": fmt.Sprintf("issue #%d", issueNumber), "repo": repo, "latest_state": status, "branch": payload.Branch, "current": payload.Stage, "next": payload.NextAction, "blockers": payload.Error, "updated": payload.Timestamp}
		if linkedPR != nil {
			data["pr"] = fmt.Sprintf("#%d", linkedPR.Number)
		}
		encoded, err := json.MarshalIndent(data, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode issue status: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", encoded)
		return 0
	}
	_, _ = fmt.Fprintf(a.out, "Target: issue #%d\n", issueNumber)
	_, _ = fmt.Fprintf(a.out, "Latest state: %s\n", status)
	if payload.Branch != "" {
		_, _ = fmt.Fprintf(a.out, "Branch: %s\n", payload.Branch)
	}
	if payload.Stage != "" {
		_, _ = fmt.Fprintf(a.out, "Current: %s\n", payload.Stage)
	}
	if payload.NextAction != "" {
		_, _ = fmt.Fprintf(a.out, "Next: %s\n", payload.NextAction)
	}
	if payload.Error != "" {
		_, _ = fmt.Fprintf(a.out, "Blockers: %s\n", payload.Error)
	}
	if linkedPR != nil {
		_, _ = fmt.Fprintf(a.out, "PR: #%d\n", linkedPR.Number)
	}
	if payload.Timestamp != "" {
		_, _ = fmt.Fprintf(a.out, "Updated: %s\n", payload.Timestamp)
	}
	return 0
}

func (a *App) runPRStatus(ctx context.Context, repo string, prNumber int, asJSON bool) int {
	pullRequest, err := a.prLifecycle.FetchPullRequest(ctx, repo, prNumber)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to fetch PR #%d: %v\n", prNumber, err)
		return 1
	}
	conversation, err := a.prLifecycle.ConversationCommentsForPullRequest(ctx, repo, prNumber)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to list PR #%d comments: %v\n", prNumber, err)
		return 1
	}
	trackerComments := make([]orchestration.TrackerComment, 0, len(conversation))
	for _, comment := range conversation {
		trackerComments = append(trackerComments, orchestration.TrackerComment{HTMLURL: comment.URL, Body: comment.Body})
	}
	latest, warnings := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("pr #%d", prNumber))
	for _, warning := range warnings {
		_, _ = fmt.Fprintf(a.err, "Warning: %s\n", warning)
	}
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
	if payload.Timestamp == "" {
		payload.Timestamp = time.Now().UTC().Format(time.RFC3339)
	}
	if asJSON {
		data := map[string]any{"target": fmt.Sprintf("pr #%d", prNumber), "repo": repo, "latest_state": status, "branch": payload.Branch, "base_branch": payload.BaseBranch, "current": payload.Stage, "next": payload.NextAction, "blockers": payload.Error, "updated": payload.Timestamp}
		encoded, err := json.MarshalIndent(data, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode PR status: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", encoded)
		return 0
	}
	_, _ = fmt.Fprintf(a.out, "Target: pr #%d\n", prNumber)
	_, _ = fmt.Fprintf(a.out, "Latest state: %s\n", status)
	if payload.Branch != "" {
		_, _ = fmt.Fprintf(a.out, "Branch: %s\n", payload.Branch)
	}
	if payload.BaseBranch != "" {
		_, _ = fmt.Fprintf(a.out, "Base branch: %s\n", payload.BaseBranch)
	}
	if payload.Stage != "" {
		_, _ = fmt.Fprintf(a.out, "Current: %s\n", payload.Stage)
	}
	if payload.NextAction != "" {
		_, _ = fmt.Fprintf(a.out, "Next: %s\n", payload.NextAction)
	}
	if payload.Error != "" {
		_, _ = fmt.Fprintf(a.out, "Blockers: %s\n", payload.Error)
	}
	if payload.Timestamp != "" {
		_, _ = fmt.Fprintf(a.out, "Updated: %s\n", payload.Timestamp)
	}
	return 0
}

func newFlagSet(name string, err io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(err)
	return fs
}
