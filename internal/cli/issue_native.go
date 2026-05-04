package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/agentexec"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/lifecycle"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

const (
	nativeIssueDefaultRunner       = "opencode"
	nativeIssueDefaultAgent        = "build"
	nativeIssueDefaultModel        = "openai/gpt-4o"
	nativeIssueDefaultBranchPrefix = "issue-fix"
)

type nativeIssueOptions struct {
	issueID              int
	common               commonOptions
	sessionPath          string
	base                 string
	includeEmpty         bool
	failOnExisting       bool
	forceIssueFlow       bool
	skipIfPRExists       bool
	noSkipIfPRExists     bool
	skipIfBranchExists   bool
	noSkipIfBranchExists bool
	forceReprocess       bool
	conflictRecoveryOnly bool
	syncReusedBranch     bool
	noSyncReusedBranch   bool
	syncStrategy         string
	detach               bool
}

func (a *App) tryRunNativeIssue(ctx context.Context, opts nativeIssueOptions) (int, bool) {
	repo := strings.TrimSpace(*opts.common.repo)
	if repo == "" {
		return 0, false
	}
	if reason := nativeIssueFallbackReason(opts); reason != "" {
		_, _ = fmt.Fprintf(a.err, "orchestrator: falling back to python issue runner: %s\n", reason)
		return 0, false
	}
	if a.issueLifecycle == nil || a.agentRunner == nil {
		_, _ = fmt.Fprintln(a.err, "orchestrator: falling back to python issue runner: native issue dependencies are not configured")
		return 0, false
	}
	return a.runNativeIssue(ctx, repo, opts), true
}

func nativeIssueFallbackReason(opts nativeIssueOptions) string {
	if opts.detach {
		return "--detach is not supported by the Go-native issue path yet"
	}
	if strings.TrimSpace(*opts.common.local) != "" {
		return "--local-config is not supported by the Go-native issue path yet"
	}
	if tracker := strings.TrimSpace(*opts.common.tracker); tracker != "" && !strings.EqualFold(tracker, lifecycle.TrackerGitHub) {
		return "native issue flow currently supports only the GitHub tracker"
	}
	if codehost := strings.TrimSpace(*opts.common.codehost); codehost != "" && !strings.EqualFold(codehost, lifecycle.CodeHostGitHub) {
		return "native issue flow currently supports only the GitHub code host"
	}
	return ""
}

func (a *App) runNativeIssue(ctx context.Context, repo string, opts nativeIssueOptions) (exitCode int) {
	tracker := startNativeSessionTracker(opts.sessionPath, fmt.Sprintf("issue #%d", opts.issueID), strconv.Itoa(opts.issueID))
	var latestState *orchestration.TrackedState
	defer func() {
		tracker.finish(latestState, exitCode)
	}()

	issue, err := a.issueLifecycle.FetchIssue(ctx, repo, opts.issueID)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to fetch issue #%d: %v\n", opts.issueID, err)
		return 1
	}
	if opts.conflictRecoveryOnly {
		return a.runNativeIssueConflictRecovery(ctx, repo, issue, opts, &latestState)
	}
	comments, err := a.issueLifecycle.ListIssueComments(ctx, repo, issue.Number)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to read issue #%d comments: %v\n", issue.Number, err)
		return 1
	}
	trackerComments := make([]orchestration.TrackerComment, 0, len(comments))
	for _, comment := range comments {
		trackerComments = append(trackerComments, orchestration.TrackerComment{
			ID:        comment.ID,
			CreatedAt: comment.CreatedAt,
			HTMLURL:   comment.HTMLURL,
			Body:      comment.Body,
		})
	}
	recoveredState, _ := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("issue #%d", issue.Number))
	linkedPR, err := a.issueLifecycle.FindOpenPullRequestForIssue(ctx, repo, issue)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to find linked PR for issue #%d: %v\n", issue.Number, err)
		return 1
	}
	decision := orchestration.ChooseExecutionMode(issue.Number, linkedPRNumber(linkedPR), opts.forceIssueFlow, parsedStatePayload(recoveredState), nil)
	if decision.Mode == orchestration.ExecutionModeSkip {
		_, _ = fmt.Fprintf(a.out, "Skipping issue #%d: %s\n", issue.Number, decision.Reason)
		return 0
	}
	if decision.Mode != orchestration.ExecutionModeIssueFlow {
		if decision.Mode == orchestration.ExecutionModePRReview && linkedPR != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: routing issue #%d to pr review flow: %s\n", issue.Number, decision.Reason)
			if code, ok := a.tryRunNativePR(ctx, nativePROptions{
				prID:   linkedPR.Number,
				common: opts.common,
			}); ok {
				return code
			}
			return a.runPython(ctx, buildPRPythonArgs(
				a.runtime.RunnerScript(),
				opts.common,
				linkedPR.Number,
				false,
				false,
				false,
				"",
				false,
				"",
			))
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: falling back to python issue runner: %s\n", decision.Reason)
		return a.runPython(ctx, buildIssuePythonArgs(
			a.runtime.RunnerScript(),
			opts.common,
			issue.Number,
			opts.base,
			opts.includeEmpty,
			false,
			opts.failOnExisting,
			opts.forceIssueFlow,
			opts.skipIfPRExists,
			opts.noSkipIfPRExists,
			opts.skipIfBranchExists,
			opts.noSkipIfBranchExists,
			opts.forceReprocess,
			opts.conflictRecoveryOnly,
			opts.syncReusedBranch,
			opts.noSyncReusedBranch,
			opts.syncStrategy,
			nilFlagState{},
		))
	}
	if strings.TrimSpace(issue.Body) == "" && !opts.includeEmpty {
		_, _ = fmt.Fprintf(a.out, "Skipping issue #%d: body is empty and --include-empty is not set\n", issue.Number)
		return 0
	}
	if linkedPR != nil {
		if opts.failOnExisting {
			_, _ = fmt.Fprintf(a.err, "orchestrator: issue #%d already has linked PR #%d and --fail-on-existing is enabled\n", issue.Number, linkedPR.Number)
			return 1
		}
		if shouldSkipExisting(opts.skipIfPRExists, opts.noSkipIfPRExists, opts.forceReprocess) {
			_, _ = fmt.Fprintf(a.out, "Skipping issue #%d: linked open PR #%d already exists\n", issue.Number, linkedPR.Number)
			return 0
		}
		_, _ = fmt.Fprintln(a.err, "orchestrator: falling back to python issue runner: linked PR reuse is not supported by the Go-native issue path yet")
		return a.runPython(ctx, buildIssuePythonArgs(
			a.runtime.RunnerScript(),
			opts.common,
			issue.Number,
			opts.base,
			opts.includeEmpty,
			false,
			opts.failOnExisting,
			opts.forceIssueFlow,
			opts.skipIfPRExists,
			opts.noSkipIfPRExists,
			opts.skipIfBranchExists,
			opts.noSkipIfBranchExists,
			opts.forceReprocess,
			opts.conflictRecoveryOnly,
			opts.syncReusedBranch,
			opts.noSyncReusedBranch,
			opts.syncStrategy,
			nilFlagState{},
		))
	}

	cwd := defaultSourceDir(*opts.common.dir)
	repoRoot, err := a.gitRepoRoot(ctx, cwd)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve repository root: %v\n", err)
		return 1
	}
	if dirty, err := a.gitHasChanges(ctx, repoRoot); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect worktree status: %v\n", err)
		return 1
	} else if dirty {
		_, _ = fmt.Fprintln(a.err, "orchestrator: git working tree must be clean before native issue execution")
		return 1
	}
	originalBranch, err := a.gitCurrentBranch(ctx, repoRoot)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve current branch: %v\n", err)
		return 1
	}
	baseBranch, stackedBase, err := a.resolveIssueBaseBranch(ctx, repo, repoRoot, originalBranch, opts.base)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve base branch: %v\n", err)
		return 1
	}
	branchPrefix := strings.TrimSpace(*opts.common.branch)
	if branchPrefix == "" {
		branchPrefix = nativeIssueDefaultBranchPrefix
	}
	issueBranch := nativeIssueBranchName(issue, branchPrefix)
	localBranchExists, err := a.gitLocalBranchExists(ctx, repoRoot, issueBranch)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect local branch %q: %v\n", issueBranch, err)
		return 1
	}
	remoteBranchExists, err := a.gitRemoteBranchExists(ctx, repoRoot, issueBranch)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect remote branch %q: %v\n", issueBranch, err)
		return 1
	}
	if localBranchExists || remoteBranchExists {
		if opts.failOnExisting {
			_, _ = fmt.Fprintf(a.err, "orchestrator: branch %q already exists and --fail-on-existing is enabled\n", issueBranch)
			return 1
		}
		if shouldSkipExisting(opts.skipIfBranchExists, opts.noSkipIfBranchExists, opts.forceReprocess) {
			_, _ = fmt.Fprintf(a.out, "Skipping issue #%d: branch %q already exists\n", issue.Number, issueBranch)
			return 0
		}
	}
	if *opts.common.dryRun {
		_, _ = fmt.Fprintf(a.out, "[dry-run] Native issue flow preflight succeeded for issue #%d on branch %q from base %q; agent run skipped\n", issue.Number, issueBranch, baseBranch)
		return 0
	}

	runnerName := fallbackString(strings.TrimSpace(*opts.common.runner), nativeIssueDefaultRunner)
	agentName := fallbackString(strings.TrimSpace(*opts.common.agent), nativeIssueDefaultAgent)
	modelName := fallbackString(strings.TrimSpace(*opts.common.model), nativeIssueDefaultModel)
	branchLifecycle := orchestration.BranchLifecycleCreated
	var reusedBranchSync *orchestration.ReusedBranchSyncVerdict
	postState := func(state orchestration.TrackedState) {
		copy := state
		latestState = &copy
		if err := a.safePostIssueState(ctx, repo, issue.Number, state); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post state for issue #%d: %v\n", issue.Number, err)
		}
	}
	failedState := func(stage, nextAction, message string) orchestration.TrackedState {
		return orchestration.TrackedState{
			Status:           orchestration.StatusFailed,
			TaskType:         "issue",
			Issue:            intPtr(issue.Number),
			Branch:           issueBranch,
			BranchLifecycle:  branchLifecycle,
			BaseBranch:       baseBranch,
			Runner:           runnerName,
			Agent:            agentName,
			Model:            modelName,
			Attempt:          1,
			Stage:            stage,
			NextAction:       nextAction,
			Error:            message,
			Timestamp:        time.Now().UTC().Format(time.RFC3339),
			ReusedBranchSync: reusedBranchSync,
		}
	}
	pushAfterAgentWithLease := false
	branchLifecycle, reusedBranchSync, pushAfterAgentWithLease, err = a.prepareIssueBranchPreflight(ctx, repoRoot, baseBranch, issueBranch, localBranchExists, remoteBranchExists, opts.syncReusedBranch, opts.syncStrategy)
	if err != nil {
		state := failedState("sync_branch", "resolve_branch_sync_conflict", err.Error())
		state.Status = orchestration.StatusBlocked
		postState(state)
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to prepare issue branch %q: %v\n", issueBranch, err)
		return 1
	}
	postState(orchestration.TrackedState{
		Status:           orchestration.StatusInProgress,
		TaskType:         "issue",
		Issue:            intPtr(issue.Number),
		Branch:           issueBranch,
		BranchLifecycle:  branchLifecycle,
		BaseBranch:       baseBranch,
		Runner:           runnerName,
		Agent:            agentName,
		Model:            modelName,
		Attempt:          1,
		Stage:            "agent_run",
		NextAction:       "wait_for_agent_result",
		Timestamp:        time.Now().UTC().Format(time.RFC3339),
		ReusedBranchSync: reusedBranchSync,
	})
	preRunUntracked, err := a.gitUntrackedFiles(ctx, repoRoot)
	if err != nil {
		postState(failedState("agent_run", "inspect_git_status", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to capture untracked baseline: %v\n", err)
		return 1
	}
	prompt := buildNativeIssuePrompt(issue, *opts.common.lightweight || strings.EqualFold(strings.TrimSpace(*opts.common.mode), "lightweight"))
	result, err := a.agentRunner.Run(ctx, fmt.Sprintf("issue #%d", issue.Number), agentexec.Request{
		Runner:              runnerName,
		Prompt:              prompt,
		Agent:               agentName,
		Model:               modelName,
		OpenCodeAutoApprove: *opts.common.autoYes,
		Cwd:                 repoRoot,
		TrackTokens:         opts.common.trackTokens != nil && *opts.common.trackTokens,
		TokenBudget:         positiveIntPtrValue(opts.common.tokenBudget),
		CostBudgetUSD:       positiveFloatPtrValue(opts.common.costBudget),
		Timeout:             durationSeconds(*opts.common.timeout),
		IdleTimeout:         durationSeconds(*opts.common.idleTime),
		Stdout:              a.out,
		Stderr:              a.err,
	})
	if err != nil {
		message := err.Error()
		if result != nil && result.ClarificationRequest != nil {
			message = stringValue(result.ClarificationRequest["reason"], message)
		}
		postState(failedState("agent_run", "inspect_agent_failure", message))
		_, _ = fmt.Fprintf(a.err, "orchestrator: agent failed for issue #%d: %v\n", issue.Number, err)
		return 1
	}
	if result != nil && result.ClarificationRequest != nil {
		question := stringValue(result.ClarificationRequest["question"], "")
		reason := stringValue(result.ClarificationRequest["reason"], question)
		if question != "" {
			if err := a.safePostIssueComment(ctx, repo, issue.Number, orchestration.BuildClarificationRequestComment(question, reason)); err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post clarification request for issue #%d: %v\n", issue.Number, err)
			}
			postState(orchestration.TrackedState{
				Status:           orchestration.StatusWaitingForAuthor,
				TaskType:         "issue",
				Issue:            intPtr(issue.Number),
				Branch:           issueBranch,
				BranchLifecycle:  branchLifecycle,
				BaseBranch:       baseBranch,
				Runner:           runnerName,
				Agent:            agentName,
				Model:            modelName,
				Attempt:          1,
				Stage:            "agent_run",
				NextAction:       "await_author_reply",
				Error:            reason,
				Timestamp:        time.Now().UTC().Format(time.RFC3339),
				ReusedBranchSync: reusedBranchSync,
				Stats:            statsMap(result.Stats),
			})
			_, _ = fmt.Fprintf(a.out, "Paused issue #%d for clarification: %s\n", issue.Number, question)
			return 0
		}
	}
	hasChanges, err := a.gitHasChanges(ctx, repoRoot)
	if err != nil {
		postState(failedState("commit_push", "inspect_git_status", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect post-agent changes: %v\n", err)
		return 1
	}
	if !hasChanges {
		if branchLifecycle == orchestration.BranchLifecycleReused && reusedBranchSync != nil && reusedBranchSync.Changed {
			if err := a.assertNativeGitContext(ctx, repoRoot, issueBranch, "push sync-only issue branch updates"); err != nil {
				postState(failedState("commit_push", "restore_branch_context", err.Error()))
				_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
				return 1
			}
			if err := a.gitPushBranch(ctx, repoRoot, issueBranch, pushAfterAgentWithLease); err != nil {
				postState(failedState("commit_push", "inspect_push_failure", err.Error()))
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to push sync-only issue branch %q: %v\n", issueBranch, err)
				return 1
			}
			prURL, err := a.issueLifecycle.CreatePullRequest(ctx, lifecycle.CreatePullRequestRequest{
				Repo:               repo,
				BaseBranch:         baseBranch,
				HeadBranch:         issueBranch,
				Title:              issue.Title,
				IssueRef:           fmt.Sprintf("#%d", issue.Number),
				IssueURL:           issue.URL,
				CloseLinkedIssue:   true,
				StackedBaseContext: stackedBase,
			})
			if err != nil {
				postState(failedState("pr_ready", "inspect_pr_creation_failure", err.Error()))
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create PR for sync-only issue #%d: %v\n", issue.Number, err)
				return 1
			}
			prNumber := parsePullRequestNumber(prURL)
			postState(orchestration.TrackedState{
				Status:           orchestration.StatusReadyForReview,
				TaskType:         "issue",
				Issue:            intPtr(issue.Number),
				PR:               prNumber,
				Branch:           issueBranch,
				BranchLifecycle:  branchLifecycle,
				BaseBranch:       baseBranch,
				Runner:           runnerName,
				Agent:            agentName,
				Model:            modelName,
				Attempt:          1,
				Stage:            "pr_ready",
				NextAction:       "wait_for_review",
				Error:            "Agent finished without changing files; pushed sync-only branch updates",
				Timestamp:        time.Now().UTC().Format(time.RFC3339),
				ReusedBranchSync: reusedBranchSync,
				Stats:            statsMap(result.Stats),
			})
			if prURL != "" {
				_, _ = fmt.Fprintf(a.out, "No file changes from agent for issue #%d; pushed sync-only branch updates and prepared PR: %s\n", issue.Number, prURL)
			} else {
				_, _ = fmt.Fprintf(a.out, "No file changes from agent for issue #%d; pushed sync-only branch updates and prepared PR\n", issue.Number)
			}
			return 0
		}
		postState(orchestration.TrackedState{
			Status:           orchestration.StatusWaitingForAuthor,
			TaskType:         "issue",
			Issue:            intPtr(issue.Number),
			Branch:           issueBranch,
			BranchLifecycle:  branchLifecycle,
			BaseBranch:       baseBranch,
			Runner:           runnerName,
			Agent:            agentName,
			Model:            modelName,
			Attempt:          1,
			Stage:            "agent_run",
			NextAction:       "inspect_noop_result",
			Error:            "Agent finished without changing the worktree",
			Timestamp:        time.Now().UTC().Format(time.RFC3339),
			ReusedBranchSync: reusedBranchSync,
			Stats:            statsMap(result.Stats),
		})
		_, _ = fmt.Fprintf(a.out, "No changes detected for issue #%d; skipping commit and PR\n", issue.Number)
		return 0
	}
	if err := a.assertNativeGitContext(ctx, repoRoot, issueBranch, "commit issue changes"); err != nil {
		postState(failedState("commit_push", "restore_branch_context", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return 1
	}
	if err := a.gitStageIssueChanges(ctx, repoRoot, preRunUntracked); err != nil {
		postState(failedState("commit_push", "inspect_stage_failure", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to stage issue changes: %v\n", err)
		return 1
	}
	commitMessage := nativeIssueCommitTitle(issue)
	if err := a.gitCommit(ctx, repoRoot, commitMessage); err != nil {
		postState(failedState("commit_push", "inspect_commit_failure", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to commit issue changes: %v\n", err)
		return 1
	}
	if err := a.gitPushBranch(ctx, repoRoot, issueBranch, pushAfterAgentWithLease); err != nil {
		postState(failedState("commit_push", "inspect_push_failure", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to push issue branch %q: %v\n", issueBranch, err)
		return 1
	}
	prURL, err := a.issueLifecycle.CreatePullRequest(ctx, lifecycle.CreatePullRequestRequest{
		Repo:               repo,
		BaseBranch:         baseBranch,
		HeadBranch:         issueBranch,
		Title:              issue.Title,
		IssueRef:           fmt.Sprintf("#%d", issue.Number),
		IssueURL:           issue.URL,
		CloseLinkedIssue:   true,
		StackedBaseContext: stackedBase,
	})
	if err != nil {
		postState(failedState("pr_ready", "inspect_pr_creation_failure", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create PR for issue #%d: %v\n", issue.Number, err)
		return 1
	}
	prNumber := parsePullRequestNumber(prURL)
	postState(orchestration.TrackedState{
		Status:           orchestration.StatusReadyForReview,
		TaskType:         "issue",
		Issue:            intPtr(issue.Number),
		PR:               prNumber,
		Branch:           issueBranch,
		BranchLifecycle:  branchLifecycle,
		BaseBranch:       baseBranch,
		Runner:           runnerName,
		Agent:            agentName,
		Model:            modelName,
		Attempt:          1,
		Stage:            "pr_ready",
		NextAction:       "wait_for_review",
		Timestamp:        time.Now().UTC().Format(time.RFC3339),
		ReusedBranchSync: reusedBranchSync,
		Stats:            statsMap(result.Stats),
	})
	if prURL != "" {
		_, _ = fmt.Fprintf(a.out, "Prepared issue #%d for review: %s\n", issue.Number, prURL)
	} else {
		_, _ = fmt.Fprintf(a.out, "Prepared issue #%d for review\n", issue.Number)
	}
	return 0
}

type nilFlagState struct{}

func (nilFlagState) wasPassed(string) bool { return false }

func linkedPRNumber(pr *lifecycle.PullRequest) *orchestration.LinkedPullRequest {
	if pr == nil {
		return nil
	}
	return &orchestration.LinkedPullRequest{
		Number:           pr.Number,
		MergeStateStatus: pr.MergeStateStatus,
		Mergeable:        pr.Mergeable,
	}
}

func parsedStatePayload(parsed *orchestration.ParsedTrackerComment[orchestration.TrackedState]) *orchestration.TrackedState {
	if parsed == nil {
		return nil
	}
	state := parsed.Payload
	return &state
}

func shouldSkipExisting(skip, noSkip, forceReprocess bool) bool {
	if noSkip || forceReprocess {
		return false
	}
	return skip
}

func fallbackString(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return strings.TrimSpace(value)
}

func durationSeconds(seconds int) time.Duration {
	if seconds <= 0 {
		return 0
	}
	return time.Duration(seconds) * time.Second
}

func buildNativeIssuePrompt(issue lifecycle.Issue, lightweight bool) string {
	if lightweight {
		return strings.TrimSpace(fmt.Sprintf(
			"You are working on a small, well-scoped issue in the current git branch.\nImplement the fix in repository files using the smallest correct change.\nDo not run git commands; git actions are handled by orchestration script.\n\nIf the task is ambiguous, unsafe, or needs product/business judgment, do not guess and do not wait for interactive approval. Instead, stop and print %s followed by a JSON object like {\"question\":\"<focused question>\",\"reason\":\"<why clarification is required>\"}.\n\nIssue: #%d - %s\nURL: %s\n\nIssue body:\n%s",
			orchestration.ClarificationRequestMarker,
			issue.Number,
			issue.Title,
			issue.URL,
			strings.TrimSpace(issue.Body),
		))
	}
	return strings.TrimSpace(fmt.Sprintf(
		"You are working on an issue in the current git branch.\nImplement the fix for the issue in the repository files.\nDo not run git commands; git actions are handled by orchestration script.\n\nIf the task is ambiguous, unsafe, or needs product/business judgment, do not guess and do not wait for interactive approval. Instead, stop and print %s followed by a JSON object like {\"question\":\"<focused question>\",\"reason\":\"<why clarification is required>\"}.\n\nIssue: #%d - %s\nURL: %s\n\nIssue body:\n%s",
		orchestration.ClarificationRequestMarker,
		issue.Number,
		issue.Title,
		issue.URL,
		strings.TrimSpace(issue.Body),
	))
}

func nativeIssueBranchName(issue lifecycle.Issue, prefix string) string {
	cleaned := regexp.MustCompile(`[^a-zA-Z0-9]+`).ReplaceAllString(strings.ToLower(issue.Title), "-")
	cleaned = strings.Trim(cleaned, "-")
	if len(cleaned) > 40 {
		cleaned = cleaned[:40]
		cleaned = strings.Trim(cleaned, "-")
	}
	if cleaned == "" {
		cleaned = "issue"
	}
	return fmt.Sprintf("%s/%d-%s", prefix, issue.Number, cleaned)
}

func nativeIssueCommitTitle(issue lifecycle.Issue) string {
	return fmt.Sprintf("Fix issue #%d: %s", issue.Number, issue.Title)
}

func parsePullRequestNumber(url string) *int {
	matches := regexp.MustCompile(`/pull/([0-9]+)`).FindStringSubmatch(strings.TrimSpace(url))
	if len(matches) != 2 {
		return nil
	}
	number, err := strconv.Atoi(matches[1])
	if err != nil || number <= 0 {
		return nil
	}
	return &number
}

func statsMap(stats agentexec.Stats) map[string]any {
	encoded, err := json.Marshal(stats)
	if err != nil || string(encoded) == "{}" {
		return nil
	}
	var normalized map[string]any
	if err := json.Unmarshal(encoded, &normalized); err != nil || len(normalized) == 0 {
		return nil
	}
	return normalized
}

func stringValue(value any, fallback string) string {
	text, _ := value.(string)
	text = strings.TrimSpace(text)
	if text == "" {
		return fallback
	}
	return text
}

func (a *App) safePostIssueComment(ctx context.Context, repo string, issueNumber int, body string) error {
	if err := a.issueLifecycle.CommentOnIssue(ctx, repo, issueNumber, body); err != nil {
		return err
	}
	return nil
}

func (a *App) safePostIssueState(ctx context.Context, repo string, issueNumber int, state orchestration.TrackedState) error {
	body, err := orchestration.BuildOrchestrationStateComment(state)
	if err != nil {
		return err
	}
	return a.safePostIssueComment(ctx, repo, issueNumber, body)
}

func (a *App) resolveIssueBaseBranch(ctx context.Context, repo, repoRoot, currentBranch, mode string) (string, string, error) {
	mode = strings.TrimSpace(mode)
	switch mode {
	case "", "default":
		base, err := a.issueLifecycle.DefaultBranch(ctx, repo)
		return base, "", err
	case "current":
		return currentBranch, currentBranch, nil
	default:
		return "", "", fmt.Errorf("unsupported base branch mode %q (want default or current)", mode)
	}
}

func (a *App) checkoutFreshIssueBranch(ctx context.Context, repoRoot, baseBranch, branchName string) error {
	if _, err := a.runGit(ctx, repoRoot, "checkout", baseBranch); err != nil {
		return err
	}
	if _, err := a.runGit(ctx, repoRoot, "checkout", "-b", branchName); err != nil {
		return err
	}
	return nil
}

func (a *App) prepareIssueBranchPreflight(ctx context.Context, repoRoot, baseBranch, branchName string, localExists, remoteExists, syncReusedBranch bool, strategy string) (string, *orchestration.ReusedBranchSyncVerdict, bool, error) {
	if !localExists && !remoteExists {
		if err := a.checkoutFreshIssueBranch(ctx, repoRoot, baseBranch, branchName); err != nil {
			return orchestration.BranchLifecycleCreated, nil, false, err
		}
		return orchestration.BranchLifecycleCreated, nil, false, nil
	}

	if _, err := a.runGit(ctx, repoRoot, "checkout", baseBranch); err != nil {
		return orchestration.BranchLifecycleReused, nil, false, err
	}
	if localExists {
		if _, err := a.runGit(ctx, repoRoot, "checkout", branchName); err != nil {
			return orchestration.BranchLifecycleReused, nil, false, err
		}
	} else {
		if _, err := a.runGit(ctx, repoRoot, "checkout", "-b", branchName, "--track", "origin/"+branchName); err != nil {
			return orchestration.BranchLifecycleReused, nil, false, err
		}
	}

	if !syncReusedBranch {
		return orchestration.BranchLifecycleReused, nil, false, nil
	}

	verdict, err := a.syncReusedIssueBranchWithBase(ctx, repoRoot, baseBranch, branchName, strategy)
	if err != nil {
		return orchestration.BranchLifecycleReused, nil, false, err
	}
	useForceWithLease := verdict.Changed && strings.TrimSpace(verdict.AppliedStrategy) == "rebase"
	return orchestration.BranchLifecycleReused, &verdict, useForceWithLease, nil
}

func normalizeSyncStrategy(strategy string) (string, error) {
	normalized := strings.TrimSpace(strategy)
	if normalized == "" {
		return "rebase", nil
	}
	switch normalized {
	case "rebase", "merge":
		return normalized, nil
	default:
		return "", fmt.Errorf("unsupported sync strategy %q (want rebase or merge)", strategy)
	}
}

func (a *App) syncReusedIssueBranchWithBase(ctx context.Context, repoRoot, baseBranch, branchName, strategy string) (orchestration.ReusedBranchSyncVerdict, error) {
	normalizedStrategy, err := normalizeSyncStrategy(strategy)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	remoteBaseRef := "origin/" + strings.TrimSpace(baseBranch)
	if _, err := a.runGit(ctx, repoRoot, "fetch", "origin", baseBranch); err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	if normalizedStrategy == "merge" {
		return a.mergeSyncWithAutoResolution(ctx, repoRoot, remoteBaseRef, branchName, normalizedStrategy)
	}

	beforeSyncSHA, err := a.gitCurrentHeadSHA(ctx, repoRoot)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	if _, err := a.runGit(ctx, repoRoot, "rebase", remoteBaseRef); err != nil {
		_, _ = a.gitCommandSucceeds(ctx, repoRoot, "rebase", "--abort")
		return a.mergeSyncWithAutoResolution(ctx, repoRoot, remoteBaseRef, branchName, normalizedStrategy)
	}
	afterSyncSHA, err := a.gitCurrentHeadSHA(ctx, repoRoot)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	changed := beforeSyncSHA != afterSyncSHA
	return orchestration.ReusedBranchSyncVerdict{
		BranchName:        branchName,
		RemoteBaseRef:     remoteBaseRef,
		RequestedStrategy: normalizedStrategy,
		AppliedStrategy:   "rebase",
		Status:            branchSyncStatus(changed, false),
		Changed:           changed,
		AutoResolved:      false,
	}, nil
}

func (a *App) mergeSyncWithAutoResolution(ctx context.Context, repoRoot, remoteBaseRef, branchName, requestedStrategy string) (orchestration.ReusedBranchSyncVerdict, error) {
	beforeSyncSHA, err := a.gitCurrentHeadSHA(ctx, repoRoot)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	autoResolved := false
	if _, err := a.runGit(ctx, repoRoot, "merge", "--no-edit", "-X", "theirs", remoteBaseRef); err != nil {
		autoResolved = true
		if err := a.autoResolveMergeConflictsWithBase(ctx, repoRoot, remoteBaseRef); err != nil {
			_, _ = a.gitCommandSucceeds(ctx, repoRoot, "merge", "--abort")
			return orchestration.ReusedBranchSyncVerdict{}, fmt.Errorf(
				"failed to auto-resolve merge conflicts while syncing reused branch %q with %q: resolve conflicts manually or rerun with --no-sync-reused-branch",
				branchName,
				remoteBaseRef,
			)
		}
	}
	afterSyncSHA, err := a.gitCurrentHeadSHA(ctx, repoRoot)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	changed := beforeSyncSHA != afterSyncSHA
	return orchestration.ReusedBranchSyncVerdict{
		BranchName:        branchName,
		RemoteBaseRef:     remoteBaseRef,
		RequestedStrategy: requestedStrategy,
		AppliedStrategy:   "merge",
		Status:            branchSyncStatus(changed, autoResolved),
		Changed:           changed,
		AutoResolved:      autoResolved && changed,
	}, nil
}

func branchSyncStatus(changed, autoResolved bool) string {
	if !changed {
		return orchestration.BranchSyncStatusAlreadyCurrent
	}
	if autoResolved {
		return orchestration.BranchSyncStatusAutoResolved
	}
	return orchestration.BranchSyncStatusSyncedCleanly
}

func (a *App) autoResolveMergeConflictsWithBase(ctx context.Context, repoRoot, remoteBaseRef string) error {
	conflictedPaths, err := a.gitConflictedPaths(ctx, repoRoot)
	if err != nil {
		return err
	}
	if len(conflictedPaths) == 0 {
		return fmt.Errorf("merge with %q reported conflicts, but no conflicted files were detected", remoteBaseRef)
	}
	for _, path := range conflictedPaths {
		if _, err := a.runGit(ctx, repoRoot, "checkout", "--theirs", "--", path); err != nil {
			return err
		}
	}
	if _, err := a.runGit(ctx, repoRoot, "add", "-A"); err != nil {
		return err
	}
	if _, err := a.runGit(ctx, repoRoot, "commit", "--no-edit"); err != nil {
		return err
	}
	return nil
}

func (a *App) gitStageIssueChanges(ctx context.Context, repoRoot string, baseline []string) error {
	if _, err := a.runGit(ctx, repoRoot, "add", "-u"); err != nil {
		return err
	}
	after, err := a.gitUntrackedFiles(ctx, repoRoot)
	if err != nil {
		return err
	}
	baselineSet := make(map[string]struct{}, len(baseline))
	for _, path := range baseline {
		baselineSet[path] = struct{}{}
	}
	newFiles := make([]string, 0)
	for _, path := range after {
		if _, ok := baselineSet[path]; !ok {
			newFiles = append(newFiles, path)
		}
	}
	sort.Strings(newFiles)
	if len(newFiles) == 0 {
		return nil
	}
	args := append([]string{"add", "--"}, newFiles...)
	_, err = a.runGit(ctx, repoRoot, args...)
	return err
}

func (a *App) gitCommit(ctx context.Context, repoRoot, message string) error {
	_, err := a.runGit(ctx, repoRoot, "commit", "-m", message)
	return err
}

func (a *App) gitPushBranch(ctx context.Context, repoRoot, branchName string, forceWithLease bool) error {
	if err := a.assertNativeGitContext(ctx, repoRoot, branchName, "push branch"); err != nil {
		return err
	}
	args := []string{"push", "-u"}
	if forceWithLease {
		args = append(args, "--force-with-lease")
	}
	args = append(args, "origin", branchName)
	_, err := a.runGit(ctx, repoRoot, args...)
	return err
}

func (a *App) runNativeIssueConflictRecovery(
	ctx context.Context,
	repo string,
	issue lifecycle.Issue,
	opts nativeIssueOptions,
	latestState **orchestration.TrackedState,
) int {
	cwd := defaultSourceDir(*opts.common.dir)
	repoRoot, err := a.gitRepoRoot(ctx, cwd)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve repository root: %v\n", err)
		return 1
	}
	if dirty, err := a.gitHasChanges(ctx, repoRoot); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect worktree status: %v\n", err)
		return 1
	} else if dirty {
		_, _ = fmt.Fprintln(a.err, "orchestrator: git working tree must be clean before native issue execution")
		return 1
	}
	originalBranch, err := a.gitCurrentBranch(ctx, repoRoot)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve current branch: %v\n", err)
		return 1
	}
	baseBranch, _, err := a.resolveIssueBaseBranch(ctx, repo, repoRoot, originalBranch, opts.base)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve base branch: %v\n", err)
		return 1
	}
	branchPrefix := strings.TrimSpace(*opts.common.branch)
	if branchPrefix == "" {
		branchPrefix = nativeIssueDefaultBranchPrefix
	}
	issueBranch := nativeIssueBranchName(issue, branchPrefix)
	localBranchExists, err := a.gitLocalBranchExists(ctx, repoRoot, issueBranch)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect local branch %q: %v\n", issueBranch, err)
		return 1
	}
	remoteBranchExists, err := a.gitRemoteBranchExists(ctx, repoRoot, issueBranch)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect remote branch %q: %v\n", issueBranch, err)
		return 1
	}

	runnerName := fallbackString(strings.TrimSpace(*opts.common.runner), nativeIssueDefaultRunner)
	agentName := fallbackString(strings.TrimSpace(*opts.common.agent), nativeIssueDefaultAgent)
	modelName := fallbackString(strings.TrimSpace(*opts.common.model), nativeIssueDefaultModel)
	branchLifecycle := orchestration.BranchLifecycleReused
	var reusedBranchSync *orchestration.ReusedBranchSyncVerdict
	postState := func(state orchestration.TrackedState) {
		copy := state
		*latestState = &copy
		if err := a.safePostIssueState(ctx, repo, issue.Number, state); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post state for issue #%d: %v\n", issue.Number, err)
		}
	}
	buildState := func(status, stage, nextAction, message string) orchestration.TrackedState {
		return orchestration.TrackedState{
			Status:           status,
			TaskType:         "issue",
			Issue:            intPtr(issue.Number),
			Branch:           issueBranch,
			BranchLifecycle:  branchLifecycle,
			BaseBranch:       baseBranch,
			Runner:           runnerName,
			Agent:            agentName,
			Model:            modelName,
			Attempt:          1,
			Stage:            stage,
			NextAction:       nextAction,
			Error:            message,
			Timestamp:        time.Now().UTC().Format(time.RFC3339),
			ReusedBranchSync: reusedBranchSync,
		}
	}

	if !localBranchExists && !remoteBranchExists {
		message := fmt.Sprintf(
			"conflict recovery only requires an existing deterministic issue branch, but %q was not found locally or on origin; run the normal issue flow first",
			issueBranch,
		)
		postState(buildState(orchestration.StatusBlocked, "sync_branch", "run_normal_issue_flow_first", message))
		_, _ = fmt.Fprintf(a.err, "orchestrator: %s\n", message)
		return 1
	}

	_, reusedBranchSync, pushWithLease, err := a.prepareIssueBranchPreflight(ctx, repoRoot, baseBranch, issueBranch, localBranchExists, remoteBranchExists, true, opts.syncStrategy)
	if err != nil {
		postState(buildState(orchestration.StatusBlocked, "sync_branch", "resolve_branch_sync_conflict", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to recover issue branch %q: %v\n", issueBranch, err)
		return 1
	}
	if reusedBranchSync == nil {
		message := fmt.Sprintf("conflict recovery for issue branch %q did not produce a sync verdict", issueBranch)
		postState(buildState(orchestration.StatusFailed, "sync_branch", "inspect_recovery_result", message))
		_, _ = fmt.Fprintf(a.err, "orchestrator: %s\n", message)
		return 1
	}

	if reusedBranchSync.Changed {
		if err := a.gitPushBranch(ctx, repoRoot, issueBranch, pushWithLease); err != nil {
			postState(buildState(orchestration.StatusFailed, "sync_branch", "inspect_push_failure", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to push recovered issue branch %q: %v\n", issueBranch, err)
			return 1
		}
		_, _ = fmt.Fprintln(a.out, reusedBranchSync.PushSummary(false))
	}

	postState(buildState(orchestration.StatusWaitingForAuthor, "sync_branch", "inspect_conflict_recovery_result", ""))
	_, _ = fmt.Fprintln(a.out, reusedBranchSync.Summary(false))
	return 0
}

func (a *App) assertNativeGitContext(ctx context.Context, repoRoot, branchName, operation string) error {
	actualBranch, err := a.gitCurrentBranch(ctx, repoRoot)
	if err != nil {
		return err
	}
	actualRoot, err := a.gitRepoRoot(ctx, repoRoot)
	if err != nil {
		return err
	}
	if actualBranch != branchName {
		return fmt.Errorf("refusing to %s: expected branch %q, got %q", operation, branchName, actualBranch)
	}
	if filepath.Clean(actualRoot) != filepath.Clean(repoRoot) {
		return fmt.Errorf("refusing to %s: expected repo root %q, got %q", operation, repoRoot, actualRoot)
	}
	return nil
}

func (a *App) gitRepoRoot(ctx context.Context, cwd string) (string, error) {
	stdout, err := a.runGit(ctx, cwd, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", err
	}
	return filepath.Clean(strings.TrimSpace(stdout)), nil
}

func (a *App) gitCurrentBranch(ctx context.Context, cwd string) (string, error) {
	stdout, err := a.runGit(ctx, cwd, "rev-parse", "--abbrev-ref", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(stdout), nil
}

func (a *App) gitCurrentHeadSHA(ctx context.Context, cwd string) (string, error) {
	stdout, err := a.runGit(ctx, cwd, "rev-parse", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(stdout), nil
}

func (a *App) gitHasChanges(ctx context.Context, cwd string) (bool, error) {
	stdout, err := a.runGit(ctx, cwd, "status", "--porcelain")
	if err != nil {
		return false, err
	}
	return strings.TrimSpace(stdout) != "", nil
}

func (a *App) gitLocalBranchExists(ctx context.Context, cwd, branchName string) (bool, error) {
	result, err := a.runShell(ctx, cwd, gitCommand("show-ref", "--verify", "--quiet", "refs/heads/"+branchName))
	if err != nil {
		return false, err
	}
	if result.ExitCode == 0 {
		return true, nil
	}
	if result.ExitCode == 1 {
		return false, nil
	}
	return false, fmt.Errorf("git show-ref failed with exit code %d: %s", result.ExitCode, strings.TrimSpace(result.Stderr))
}

func (a *App) gitRemoteBranchExists(ctx context.Context, cwd, branchName string) (bool, error) {
	result, err := a.runShell(ctx, cwd, gitCommand("ls-remote", "--exit-code", "--heads", "origin", branchName))
	if err != nil {
		return false, err
	}
	if result.ExitCode == 0 {
		return true, nil
	}
	if result.ExitCode == 2 {
		return false, nil
	}
	return false, fmt.Errorf("git ls-remote failed with exit code %d: %s", result.ExitCode, strings.TrimSpace(result.Stderr))
}

func (a *App) gitUntrackedFiles(ctx context.Context, cwd string) ([]string, error) {
	stdout, err := a.runGit(ctx, cwd, "ls-files", "--others", "--exclude-standard")
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(stdout) == "" {
		return nil, nil
	}
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	files := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line != "" {
			files = append(files, line)
		}
	}
	sort.Strings(files)
	return files, nil
}

func (a *App) gitConflictedPaths(ctx context.Context, cwd string) ([]string, error) {
	stdout, err := a.runGit(ctx, cwd, "diff", "--name-only", "--diff-filter=U")
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(stdout) == "" {
		return nil, nil
	}
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	paths := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line != "" {
			paths = append(paths, line)
		}
	}
	sort.Strings(paths)
	return paths, nil
}

func (a *App) gitCommandSucceeds(ctx context.Context, cwd string, args ...string) (bool, error) {
	result, err := a.runShell(ctx, cwd, gitCommand(args...))
	if err != nil {
		return false, err
	}
	return result.ExitCode == 0, nil
}

func (a *App) runGit(ctx context.Context, cwd string, args ...string) (string, error) {
	result, err := a.runShell(ctx, cwd, gitCommand(args...))
	if err != nil {
		return "", err
	}
	if result.ExitCode != 0 {
		message := strings.TrimSpace(result.Stderr)
		if message == "" {
			message = strings.TrimSpace(result.Stdout)
		}
		if message == "" {
			message = fmt.Sprintf("exit code %d", result.ExitCode)
		}
		return "", fmt.Errorf("git %s failed: %s", strings.Join(args, " "), message)
	}
	return result.Stdout, nil
}

func (a *App) runShell(ctx context.Context, cwd, command string) (shellExecutionResult, error) {
	if a.shell == nil {
		return shellExecutionResult{}, fmt.Errorf("shell executor is not configured")
	}
	return a.shell.Run(ctx, cwd, command)
}

func gitCommand(args ...string) string {
	parts := make([]string, 0, len(args)+1)
	parts = append(parts, "git")
	for _, arg := range args {
		parts = append(parts, shellQuote(arg))
	}
	return strings.Join(parts, " ")
}

func shellQuote(value string) string {
	if value == "" {
		return "''"
	}
	return "'" + strings.ReplaceAll(value, "'", `'"'"'`) + "'"
}
