package cli

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/agentexec"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/lifecycle"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

const (
	nativePRDefaultRunner = "opencode"
	nativePRDefaultAgent  = "review"
	nativePRDefaultModel  = "openai/gpt-4o"
)

type prReviewLifecycle interface {
	FetchPullRequest(ctx context.Context, repo string, number int) (lifecycle.PullRequest, error)
	CommentOnPullRequest(ctx context.Context, repo string, number int, body string) error
	ReviewThreadsForPullRequest(ctx context.Context, repo string, number int) ([]lifecycle.PullRequestReviewThread, error)
	ConversationCommentsForPullRequest(ctx context.Context, repo string, number int) ([]lifecycle.PullRequestConversationComment, error)
}

type nativePROptions struct {
	prID                 int
	common               commonOptions
	sessionPath          string
	allowBranchSwitch    bool
	isolateWorktree      bool
	postSummary          bool
	followupPrefix       string
	conflictRecoveryOnly bool
	syncStrategy         string
	detach               bool
}

func (a *App) tryRunNativePR(ctx context.Context, opts nativePROptions) (int, bool) {
	repo := strings.TrimSpace(*opts.common.repo)
	if repo == "" {
		return 0, false
	}
	if reason := nativePRFallbackReason(opts); reason != "" {
		_, _ = fmt.Fprintf(a.err, "orchestrator: falling back to python pr runner: %s\n", reason)
		return 0, false
	}
	if a.prLifecycle == nil || a.issueLifecycle == nil || a.agentRunner == nil {
		_, _ = fmt.Fprintln(a.err, "orchestrator: falling back to python pr runner: native PR dependencies are not configured")
		return 0, false
	}
	return a.runNativePR(ctx, repo, opts), true
}

func nativePRFallbackReason(opts nativePROptions) string {
	if opts.detach {
		return "--detach requires worker launch and is handled before native PR execution"
	}
	if strings.TrimSpace(opts.syncStrategy) != "" && !opts.conflictRecoveryOnly {
		return "--sync-strategy is not supported by the Go-native PR path yet"
	}
	if strings.TrimSpace(*opts.common.local) != "" {
		return "--local-config is not supported by the Go-native PR path yet"
	}
	if tracker := strings.TrimSpace(*opts.common.tracker); tracker != "" && !strings.EqualFold(tracker, lifecycle.TrackerGitHub) {
		return "native PR flow currently supports only the GitHub tracker"
	}
	if codehost := strings.TrimSpace(*opts.common.codehost); codehost != "" && !strings.EqualFold(codehost, lifecycle.CodeHostGitHub) {
		return "native PR flow currently supports only the GitHub code host"
	}
	return ""
}

func (a *App) runNativePR(ctx context.Context, repo string, opts nativePROptions) (exitCode int) {
	tracker := startNativeSessionTracker(opts.sessionPath, fmt.Sprintf("PR #%d", opts.prID), strconv.Itoa(opts.prID))
	var latestState *orchestration.TrackedState
	defer func() {
		tracker.finish(latestState, exitCode)
	}()

	pullRequest, err := a.prLifecycle.FetchPullRequest(ctx, repo, opts.prID)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to fetch PR #%d: %v\n", opts.prID, err)
		return 1
	}
	if opts.conflictRecoveryOnly {
		if *opts.common.dryRun {
			_, _ = fmt.Fprintf(a.out, "[dry-run] Native PR conflict recovery preflight succeeded for PR #%d on branch %q; sync/push/state updates skipped\n", pullRequest.Number, strings.TrimSpace(pullRequest.HeadRefName))
			return 0
		}
		return a.runNativePRConflictRecovery(ctx, repo, pullRequest, opts, &latestState)
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
		_, _ = fmt.Fprintln(a.err, "orchestrator: git working tree must be clean before native PR execution")
		return 1
	}

	prBranch := strings.TrimSpace(pullRequest.HeadRefName)
	if prBranch == "" {
		_, _ = fmt.Fprintf(a.err, "orchestrator: PR #%d is missing head branch metadata\n", pullRequest.Number)
		return 1
	}
	if opts.isolateWorktree {
		if *opts.common.dryRun {
			preview := filepath.Join(os.TempDir(), fmt.Sprintf("opencode-pr-%s-<random>", sanitizeBranchForPath(prBranch)))
			_, _ = fmt.Fprintf(a.out, "[dry-run] Would create isolated worktree for %q at %q\n", prBranch, preview)
		} else {
			worktreeDir, err := a.createNativePRIsolatedWorktree(ctx, repoRoot, prBranch)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create isolated PR worktree: %v\n", err)
				return 1
			}
			defer func() {
				if _, err := a.runGit(context.Background(), repoRoot, "worktree", "remove", "--force", worktreeDir); err != nil {
					_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to remove isolated worktree %q: %v\n", worktreeDir, err)
				}
			}()
			repoRoot = worktreeDir
		}
	}
	activeBranch := prBranch
	if !opts.isolateWorktree || !*opts.common.dryRun {
		currentBranch, err := a.gitCurrentBranch(ctx, repoRoot)
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve current branch: %v\n", err)
			return 1
		}
		activeBranch = currentBranch
		if activeBranch != prBranch {
			if !opts.allowBranchSwitch {
				_, _ = fmt.Fprintf(a.err, "orchestrator: current branch %q does not match PR branch %q; rerun with --allow-pr-branch-switch or switch branches manually\n", activeBranch, prBranch)
				return 1
			}
			if _, err := a.runGit(ctx, repoRoot, "checkout", prBranch); err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to switch to PR branch %q: %v\n", prBranch, err)
				return 1
			}
			activeBranch = prBranch
		}
	}
	if prefix := strings.Trim(strings.TrimSpace(opts.followupPrefix), "/"); prefix != "" {
		followupBranch := fmt.Sprintf("%s/pr-%d-review-comments", prefix, pullRequest.Number)
		if *opts.common.dryRun {
			_, _ = fmt.Fprintf(a.out, "[dry-run] Would create follow-up branch %q from %q\n", followupBranch, activeBranch)
		} else if err := a.createNativePRFollowupBranch(ctx, repoRoot, followupBranch); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create follow-up branch %q: %v\n", followupBranch, err)
			return 1
		}
		activeBranch = followupBranch
	}
	if *opts.common.dryRun {
		_, _ = fmt.Fprintf(a.out, "[dry-run] Native PR flow preflight succeeded for PR #%d on branch %q; agent run skipped\n", pullRequest.Number, activeBranch)
		return 0
	}

	runnerName := fallbackString(strings.TrimSpace(*opts.common.runner), nativePRDefaultRunner)
	agentName := fallbackString(strings.TrimSpace(*opts.common.agent), nativePRDefaultAgent)
	modelName := fallbackString(strings.TrimSpace(*opts.common.model), nativePRDefaultModel)
	maxAttempts := *opts.common.maxTry
	if maxAttempts <= 0 {
		maxAttempts = 1
	}
	linkedIssues := a.loadLinkedIssueContext(ctx, repo, pullRequest)

	postState := func(state orchestration.TrackedState) {
		copy := state
		latestState = &copy
		if err := a.safePostPRState(ctx, repo, pullRequest.Number, state); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post state for PR #%d: %v\n", pullRequest.Number, err)
		}
	}
	failedState := func(attempt int, stage, nextAction, message string, reviewOutcome *orchestration.PRReviewOutcomeSummary) orchestration.TrackedState {
		return orchestration.TrackedState{
			Status:         orchestration.StatusFailed,
			TaskType:       "pr",
			PR:             intPtr(pullRequest.Number),
			Branch:         activeBranch,
			BaseBranch:     strings.TrimSpace(pullRequest.BaseRefName),
			Runner:         runnerName,
			Agent:          agentName,
			Model:          modelName,
			Attempt:        attempt,
			Stage:          stage,
			NextAction:     nextAction,
			Error:          message,
			Timestamp:      time.Now().UTC().Format(time.RFC3339),
			ReviewFeedback: reviewOutcome,
		}
	}

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		reviewItems, reviewStats, err := a.fetchNativePRReviewFeedback(ctx, repo, pullRequest)
		if err != nil {
			postState(failedState(attempt, "review_feedback", "inspect_review_feedback", err.Error(), nil))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to collect review feedback for PR #%d: %v\n", pullRequest.Number, err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "Review prompt sources: %s\n", orchestration.FormatReviewFeedbackStats(reviewStats))
		if len(reviewItems) == 0 {
			postState(orchestration.TrackedState{
				Status:     orchestration.StatusWaitingForAuthor,
				TaskType:   "pr",
				PR:         intPtr(pullRequest.Number),
				Branch:     activeBranch,
				BaseBranch: strings.TrimSpace(pullRequest.BaseRefName),
				Runner:     runnerName,
				Agent:      agentName,
				Model:      modelName,
				Attempt:    attempt,
				Stage:      "review_feedback",
				NextAction: "await_new_review_comments",
				Error:      "No actionable review comments found",
				Timestamp:  time.Now().UTC().Format(time.RFC3339),
			})
			_, _ = fmt.Fprintf(a.out, "No actionable review comments found for PR #%d; nothing to do.\n", pullRequest.Number)
			return 0
		}

		postState(orchestration.TrackedState{
			Status:     orchestration.StatusInProgress,
			TaskType:   "pr",
			PR:         intPtr(pullRequest.Number),
			Branch:     activeBranch,
			BaseBranch: strings.TrimSpace(pullRequest.BaseRefName),
			Runner:     runnerName,
			Agent:      agentName,
			Model:      modelName,
			Attempt:    attempt,
			Stage:      "agent_run",
			NextAction: "wait_for_agent_result",
			Timestamp:  time.Now().UTC().Format(time.RFC3339),
		})

		preRunUntracked, err := a.gitUntrackedFiles(ctx, repoRoot)
		if err != nil {
			postState(failedState(attempt, "agent_run", "inspect_git_status", err.Error(), buildPRReviewFailureOutcome(reviewItems, err.Error(), "inspect_git_status")))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to capture untracked baseline: %v\n", err)
			return 1
		}
		prompt := buildNativePRReviewPrompt(pullRequest, reviewItems, linkedIssues, *opts.common.lightweight || strings.EqualFold(strings.TrimSpace(*opts.common.mode), "lightweight"))
		result, err := a.agentRunner.Run(ctx, fmt.Sprintf("PR #%d", pullRequest.Number), agentexec.Request{
			Runner:              runnerName,
			Prompt:              prompt,
			Agent:               agentName,
			Model:               modelName,
			OpenCodeAutoApprove: *opts.common.autoYes,
			Cwd:                 repoRoot,
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
			postState(failedState(attempt, "agent_run", "inspect_agent_failure", message, buildPRReviewFailureOutcome(reviewItems, message, "inspect_agent_failure")))
			_, _ = fmt.Fprintf(a.err, "orchestrator: agent failed for PR #%d: %v\n", pullRequest.Number, err)
			return 1
		}
		if result != nil && result.ExitCode != 0 {
			message := fmt.Sprintf("Agent exited with code %d", result.ExitCode)
			postState(failedState(attempt, "agent_run", "inspect_agent_failure", message, buildPRReviewFailureOutcome(reviewItems, message, "inspect_agent_failure")))
			_, _ = fmt.Fprintf(a.err, "orchestrator: %s for PR #%d\n", message, pullRequest.Number)
			return 1
		}
		if result != nil && result.ClarificationRequest != nil {
			question := stringValue(result.ClarificationRequest["question"], "")
			reason := stringValue(result.ClarificationRequest["reason"], question)
			if question != "" {
				if err := a.safePostPRComment(ctx, repo, pullRequest.Number, orchestration.BuildClarificationRequestComment(question, reason)); err != nil {
					_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post clarification request for PR #%d: %v\n", pullRequest.Number, err)
				}
				postState(orchestration.TrackedState{
					Status:         orchestration.StatusWaitingForAuthor,
					TaskType:       "pr",
					PR:             intPtr(pullRequest.Number),
					Branch:         activeBranch,
					BaseBranch:     strings.TrimSpace(pullRequest.BaseRefName),
					Runner:         runnerName,
					Agent:          agentName,
					Model:          modelName,
					Attempt:        attempt,
					Stage:          "agent_run",
					NextAction:     "await_author_reply",
					Error:          reason,
					Timestamp:      time.Now().UTC().Format(time.RFC3339),
					Stats:          statsMap(result.Stats),
					ReviewFeedback: buildPRReviewFailureOutcome(reviewItems, reason, "await_author_reply"),
				})
				_, _ = fmt.Fprintf(a.out, "Paused PR #%d for clarification: %s\n", pullRequest.Number, question)
				return 0
			}
		}

		reviewOutcome := buildPRReviewOutcomeSummary(result, reviewItems)

		hasChanges, err := a.gitHasChanges(ctx, repoRoot)
		if err != nil {
			postState(failedState(attempt, "commit_push", "inspect_git_status", err.Error(), reviewOutcome))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect post-agent changes: %v\n", err)
			return 1
		}
		if !hasChanges {
			postState(orchestration.TrackedState{
				Status:         orchestration.StatusWaitingForAuthor,
				TaskType:       "pr",
				PR:             intPtr(pullRequest.Number),
				Branch:         activeBranch,
				BaseBranch:     strings.TrimSpace(pullRequest.BaseRefName),
				Runner:         runnerName,
				Agent:          agentName,
				Model:          modelName,
				Attempt:        attempt,
				Stage:          "post_agent_check",
				NextAction:     "await_more_feedback_or_manual_changes",
				Error:          "Agent produced no repository changes",
				Timestamp:      time.Now().UTC().Format(time.RFC3339),
				Stats:          statsMap(result.Stats),
				ReviewFeedback: reviewOutcome,
			})
			_, _ = fmt.Fprintf(a.out, "No changes detected for PR #%d; skipping commit and push\n", pullRequest.Number)
			return 0
		}

		if err := a.assertNativeGitContext(ctx, repoRoot, activeBranch, "commit PR review changes"); err != nil {
			postState(failedState(attempt, "commit_push", "restore_branch_context", err.Error(), reviewOutcome))
			_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
			return 1
		}
		if err := a.gitStageIssueChanges(ctx, repoRoot, preRunUntracked); err != nil {
			postState(failedState(attempt, "commit_push", "inspect_stage_failure", err.Error(), reviewOutcome))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to stage PR changes: %v\n", err)
			return 1
		}
		if err := a.gitCommit(ctx, repoRoot, nativePRCommitTitle(pullRequest)); err != nil {
			postState(failedState(attempt, "commit_push", "inspect_commit_failure", err.Error(), reviewOutcome))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to commit PR changes: %v\n", err)
			return 1
		}
		var pushRebase *orchestration.ReusedBranchSyncVerdict
		if err := a.gitPushBranch(ctx, repoRoot, activeBranch, false); err != nil {
			if isGitPushRejected(err) {
				verdict, retryErr := a.rebasePRBranchAfterPushRejection(ctx, repoRoot, activeBranch)
				if retryErr == nil {
					pushRebase = &verdict
					_, _ = fmt.Fprintln(a.out, verdict.Summary(false))
					retryErr = a.gitPushBranch(ctx, repoRoot, activeBranch, false)
				}
				if retryErr == nil {
					_, _ = fmt.Fprintf(a.out, "Retried PR branch push after rebasing on origin/%s.\n", activeBranch)
				} else {
					postState(failedState(attempt, "commit_push", "resolve_branch_sync_conflict", retryErr.Error(), reviewOutcome))
					_, _ = fmt.Fprintf(a.err, "orchestrator: failed to recover rejected push for PR branch %q: %v\n", activeBranch, retryErr)
					return 1
				}
			} else {
				postState(failedState(attempt, "commit_push", "inspect_push_failure", err.Error(), reviewOutcome))
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to push PR branch %q: %v\n", activeBranch, err)
				return 1
			}
		}

		postState(orchestration.TrackedState{
			Status:           orchestration.StatusWaitingForCI,
			TaskType:         "pr",
			PR:               intPtr(pullRequest.Number),
			Branch:           activeBranch,
			BaseBranch:       strings.TrimSpace(pullRequest.BaseRefName),
			Runner:           runnerName,
			Agent:            agentName,
			Model:            modelName,
			Attempt:          attempt,
			Stage:            "pr_update",
			NextAction:       "wait_for_ci",
			Timestamp:        time.Now().UTC().Format(time.RFC3339),
			Stats:            statsMap(result.Stats),
			ReviewFeedback:   reviewOutcome,
			ReusedBranchSync: pushRebase,
		})
		if opts.postSummary {
			if err := a.safePostPRComment(ctx, repo, pullRequest.Number, buildNativePRSummaryComment(len(reviewItems))); err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post PR summary for PR #%d: %v\n", pullRequest.Number, err)
			}
		}

		updatedPR, err := a.prLifecycle.FetchPullRequest(ctx, repo, pullRequest.Number)
		if err != nil {
			postState(failedState(attempt, "review_feedback", "inspect_review_feedback", err.Error(), reviewOutcome))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to refresh PR #%d after push: %v\n", pullRequest.Number, err)
			return 1
		}
		pullRequest = updatedPR
		if prNeedsConflictRecovery(pullRequest) {
			_, _ = fmt.Fprintf(a.out, "PR #%d is %s/%s after review update; running conflict recovery before more review feedback.\n", pullRequest.Number, strings.TrimSpace(pullRequest.MergeStateStatus), strings.TrimSpace(pullRequest.Mergeable))
			return a.runNativePRConflictRecovery(ctx, repo, pullRequest, opts, &latestState)
		}
		remainingItems, remainingStats, err := a.fetchNativePRReviewFeedback(ctx, repo, pullRequest)
		if err != nil {
			postState(failedState(attempt, "review_feedback", "inspect_review_feedback", err.Error(), reviewOutcome))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to refresh review feedback for PR #%d: %v\n", pullRequest.Number, err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "Review prompt sources: %s\n", orchestration.FormatReviewFeedbackStats(remainingStats))
		if len(remainingItems) == 0 {
			_, _ = fmt.Fprintf(a.out, "Done. Processed PR #%d with no remaining actionable review items after attempt %d.\n", pullRequest.Number, attempt)
			return 0
		}
		if attempt >= maxAttempts {
			blockedReviewOutcome := appendRemainingReviewOutcomes(reviewOutcome, remainingItems)
			postState(orchestration.TrackedState{
				Status:         orchestration.StatusBlocked,
				TaskType:       "pr",
				PR:             intPtr(pullRequest.Number),
				Branch:         activeBranch,
				BaseBranch:     strings.TrimSpace(pullRequest.BaseRefName),
				Runner:         runnerName,
				Agent:          agentName,
				Model:          modelName,
				Attempt:        attempt,
				Stage:          "review_feedback",
				NextAction:     "manual_review_follow_up_required",
				Error:          fmt.Sprintf("%d actionable review items remain after %d/%d attempts", len(remainingItems), attempt, maxAttempts),
				Timestamp:      time.Now().UTC().Format(time.RFC3339),
				Stats:          statsMap(result.Stats),
				ReviewFeedback: blockedReviewOutcome,
			})
			_, _ = fmt.Fprintf(a.out, "PR #%d still has %d actionable review items after %d/%d attempts; blocking for manual follow-up.\n", pullRequest.Number, len(remainingItems), attempt, maxAttempts)
			return 0
		}
		_, _ = fmt.Fprintf(a.out, "PR #%d still has %d actionable review items after attempt %d; continuing review feedback loop (%d/%d).\n", pullRequest.Number, len(remainingItems), attempt, attempt+1, maxAttempts)
	}

	return 0
}

func (a *App) runNativePRConflictRecovery(
	ctx context.Context,
	repo string,
	pullRequest lifecycle.PullRequest,
	opts nativePROptions,
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
		_, _ = fmt.Fprintln(a.err, "orchestrator: git working tree must be clean before native PR execution")
		return 1
	}

	prBranch := strings.TrimSpace(pullRequest.HeadRefName)
	if prBranch == "" {
		_, _ = fmt.Fprintf(a.err, "orchestrator: PR #%d is missing head branch metadata\n", pullRequest.Number)
		return 1
	}
	baseBranch := strings.TrimSpace(pullRequest.BaseRefName)
	if baseBranch == "" {
		_, _ = fmt.Fprintf(a.err, "orchestrator: PR #%d is missing base branch metadata\n", pullRequest.Number)
		return 1
	}

	localBranchExists, err := a.gitLocalBranchExists(ctx, repoRoot, prBranch)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect local branch %q: %v\n", prBranch, err)
		return 1
	}
	remoteBranchExists, err := a.gitRemoteBranchExists(ctx, repoRoot, prBranch)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect remote branch %q: %v\n", prBranch, err)
		return 1
	}

	runnerName := fallbackString(strings.TrimSpace(*opts.common.runner), nativePRDefaultRunner)
	agentName := fallbackString(strings.TrimSpace(*opts.common.agent), nativePRDefaultAgent)
	modelName := fallbackString(strings.TrimSpace(*opts.common.model), nativePRDefaultModel)
	branchLifecycle := orchestration.BranchLifecycleReused
	var reusedBranchSync *orchestration.ReusedBranchSyncVerdict
	postState := func(state orchestration.TrackedState) {
		copy := state
		*latestState = &copy
		if err := a.safePostPRState(ctx, repo, pullRequest.Number, state); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to post state for PR #%d: %v\n", pullRequest.Number, err)
		}
	}
	buildState := func(status, stage, nextAction, message string) orchestration.TrackedState {
		return orchestration.TrackedState{
			Status:           status,
			TaskType:         "pr",
			PR:               intPtr(pullRequest.Number),
			Branch:           prBranch,
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
			"conflict recovery only requires an existing PR branch, but %q was not found locally or on origin; fetch the branch or run normal PR flow first",
			prBranch,
		)
		postState(buildState(orchestration.StatusBlocked, "sync_branch", "run_normal_pr_flow_first", message))
		_, _ = fmt.Fprintf(a.err, "orchestrator: %s\n", message)
		return 1
	}

	_, reusedBranchSync, pushWithLease, err := a.prepareIssueBranchPreflight(ctx, repoRoot, baseBranch, prBranch, localBranchExists, remoteBranchExists, true, opts.syncStrategy)
	if err != nil {
		postState(buildState(orchestration.StatusBlocked, "sync_branch", "resolve_branch_sync_conflict", err.Error()))
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to recover PR branch %q: %v\n", prBranch, err)
		return 1
	}
	if reusedBranchSync == nil {
		message := fmt.Sprintf("conflict recovery for PR branch %q did not produce a sync verdict", prBranch)
		postState(buildState(orchestration.StatusFailed, "sync_branch", "inspect_recovery_result", message))
		_, _ = fmt.Fprintf(a.err, "orchestrator: %s\n", message)
		return 1
	}

	if reusedBranchSync.Changed {
		if err := a.gitPushBranch(ctx, repoRoot, prBranch, pushWithLease); err != nil {
			postState(buildState(orchestration.StatusFailed, "sync_branch", "inspect_push_failure", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to push recovered PR branch %q: %v\n", prBranch, err)
			return 1
		}
		_, _ = fmt.Fprintln(a.out, reusedBranchSync.PushSummary(false))
	}

	postState(buildState(orchestration.StatusWaitingForAuthor, "sync_branch", "inspect_conflict_recovery_result", ""))
	_, _ = fmt.Fprintln(a.out, reusedBranchSync.Summary(false))
	return 0
}

func (a *App) fetchNativePRReviewFeedback(ctx context.Context, repo string, pullRequest lifecycle.PullRequest) ([]orchestration.ReviewFeedbackItem, orchestration.ReviewFeedbackStats, error) {
	threads, err := a.prLifecycle.ReviewThreadsForPullRequest(ctx, repo, pullRequest.Number)
	if err != nil {
		return nil, orchestration.ReviewFeedbackStats{}, err
	}
	conversationComments, err := a.prLifecycle.ConversationCommentsForPullRequest(ctx, repo, pullRequest.Number)
	if err != nil {
		return nil, orchestration.ReviewFeedbackStats{}, err
	}
	normalizedThreads := make([]orchestration.ReviewThread, 0, len(threads))
	for _, thread := range threads {
		comments := make([]orchestration.ReviewThreadComment, 0, len(thread.Comments))
		for _, comment := range thread.Comments {
			author := ""
			if comment.Author != nil {
				author = comment.Author.Login
			}
			comments = append(comments, orchestration.ReviewThreadComment{
				Body:     comment.Body,
				Path:     comment.Path,
				Line:     comment.Line,
				Outdated: comment.Outdated,
				URL:      comment.URL,
				Author:   author,
			})
		}
		normalizedThreads = append(normalizedThreads, orchestration.ReviewThread{
			Resolved: thread.IsResolved,
			Outdated: thread.IsOutdated,
			Comments: comments,
		})
	}
	normalizedConversation := make([]orchestration.ConversationComment, 0, len(conversationComments))
	for _, comment := range conversationComments {
		normalizedConversation = append(normalizedConversation, orchestration.ConversationComment{
			Author: comment.Author,
			Body:   comment.Body,
			URL:    comment.URL,
		})
	}
	normalizedReviews := make([]orchestration.PullRequestReview, 0, len(pullRequest.Reviews))
	for _, review := range pullRequest.Reviews {
		normalizedReviews = append(normalizedReviews, orchestration.PullRequestReview{
			State:       review.State,
			SubmittedAt: review.SubmittedAt,
			AuthorLogin: review.AuthorLogin,
			Body:        review.Body,
			URL:         review.URL,
		})
	}
	prAuthor := ""
	if pullRequest.Author != nil {
		prAuthor = pullRequest.Author.Login
	}
	items, stats := orchestration.NormalizeReviewFeedback(normalizedThreads, normalizedReviews, normalizedConversation, prAuthor)
	return items, stats, nil
}

func (a *App) loadLinkedIssueContext(ctx context.Context, repo string, pullRequest lifecycle.PullRequest) []lifecycle.Issue {
	if len(pullRequest.ClosingIssuesReferences) == 0 {
		return nil
	}
	linked := make([]lifecycle.Issue, 0, len(pullRequest.ClosingIssuesReferences))
	for _, reference := range pullRequest.ClosingIssuesReferences {
		if reference.Number <= 0 {
			continue
		}
		issue, err := a.issueLifecycle.FetchIssue(ctx, repo, reference.Number)
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: warning: failed to load linked issue #%d for PR #%d: %v\n", reference.Number, pullRequest.Number, err)
			continue
		}
		linked = append(linked, issue)
	}
	return linked
}

func buildNativePRReviewPrompt(pullRequest lifecycle.PullRequest, reviewItems []orchestration.ReviewFeedbackItem, linkedIssues []lifecycle.Issue, lightweight bool) string {
	issueContextLines := make([]string, 0, len(linkedIssues))
	for _, issue := range linkedIssues {
		issueContextLines = append(issueContextLines, strings.TrimSpace(fmt.Sprintf(
			"- Issue #%d: %s\n  URL: %s\n  Body: %s",
			issue.Number,
			strings.TrimSpace(issue.Title),
			strings.TrimSpace(issue.URL),
			strings.TrimSpace(issue.Body),
		)))
	}
	if len(issueContextLines) == 0 {
		issueContextLines = append(issueContextLines, "- No linked issue context found.")
	}
	commentLines := make([]string, 0, len(reviewItems))
	for index, item := range reviewItems {
		author := fallbackString(strings.TrimSpace(item.Author), "unknown")
		body := strings.TrimSpace(item.Body)
		url := strings.TrimSpace(item.URL)
		switch item.Type {
		case "review_summary":
			commentLines = append(commentLines, strings.TrimSpace(fmt.Sprintf(
				"%d. Type: review_summary\n   Author: %s\n   State: %s\n   Feedback: %s\n   Link: %s",
				index+1,
				author,
				strings.TrimSpace(item.State),
				body,
				url,
			)))
		case "conversation_comment":
			commentLines = append(commentLines, strings.TrimSpace(fmt.Sprintf(
				"%d. Type: conversation_comment\n   Author: %s\n   Feedback: %s\n   Link: %s",
				index+1,
				author,
				body,
				url,
			)))
		default:
			location := strings.TrimSpace(item.Path)
			if item.Line > 0 {
				if location == "" {
					location = fmt.Sprintf("%d", item.Line)
				} else {
					location = fmt.Sprintf("%s:%d", location, item.Line)
				}
			}
			if location == "" {
				location = "unknown-location"
			}
			commentLines = append(commentLines, strings.TrimSpace(fmt.Sprintf(
				"%d. Type: review_comment\n   Author: %s\n   Location: %s\n   Feedback: %s\n   Link: %s",
				index+1,
				author,
				location,
				body,
				url,
			)))
		}
	}
	if len(commentLines) == 0 {
		commentLines = append(commentLines, "- No actionable review comments found.")
	}
	firstLine := "You are working on an existing GitHub pull request review cycle in the current git branch."
	if lightweight {
		firstLine = "You are working on a small follow-up for an existing GitHub pull request review cycle in the current git branch."
	}
	return strings.TrimSpace(fmt.Sprintf(
		"%s\nImplement the fix requested in PR review comments in repository files.\nDo not run git commands; git actions are handled by orchestration script.\n\nAfter finishing, print %s followed by JSON object like {\"items\":[{\"item\":1,\"status\":\"fixed|not-fixed|needs-human-follow-up\",\"summary\":\"what changed or why not\",\"next_action\":\"required only when not fixed\"}]}. Include one item per review comment in the same order. If code was changed, mention relevant files and tests.\n\nIf the requested change is ambiguous, unsafe, or needs product/business judgment, do not guess and do not wait for interactive approval. Instead, stop and print %s followed by a JSON object like {\"question\":\"<focused question>\",\"reason\":\"<why clarification is required>\"}.\n\nPull Request: #%d - %s\nPR URL: %s\n\nPR description:\n%s\n\nLinked issue context:\n%s\n\nReview comments to address:\n%s",
		firstLine,
		orchestration.PRReviewOutcomeMarker,
		orchestration.ClarificationRequestMarker,
		pullRequest.Number,
		strings.TrimSpace(pullRequest.Title),
		strings.TrimSpace(pullRequest.URL),
		strings.TrimSpace(pullRequest.Body),
		strings.Join(issueContextLines, "\n"),
		strings.Join(commentLines, "\n"),
	))
}

func buildPRReviewOutcomeSummary(result *agentexec.Result, reviewItems []orchestration.ReviewFeedbackItem) *orchestration.PRReviewOutcomeSummary {
	if result != nil {
		if parsed := orchestration.ParsePRReviewOutcomeSummary(result.Output); parsed != nil {
			return parsed
		}
	}
	if len(reviewItems) == 0 {
		return nil
	}
	items := make([]orchestration.PRReviewItemOutcome, 0, len(reviewItems))
	for i, item := range reviewItems {
		summary := strings.TrimSpace(item.Body)
		if summary == "" {
			summary = "Review item was included in the prompt, but the agent did not report a structured outcome."
		}
		items = append(items, orchestration.PRReviewItemOutcome{
			Item:       i + 1,
			Status:     "not-fixed",
			Summary:    summary,
			NextAction: "manual_review_follow_up_required",
		})
	}
	return &orchestration.PRReviewOutcomeSummary{Items: items}
}

func buildPRReviewFailureOutcome(reviewItems []orchestration.ReviewFeedbackItem, message, nextAction string) *orchestration.PRReviewOutcomeSummary {
	if len(reviewItems) == 0 {
		return nil
	}
	message = strings.TrimSpace(message)
	if message == "" {
		message = "PR review worker did not complete successfully."
	}
	nextAction = strings.TrimSpace(nextAction)
	if nextAction == "" {
		nextAction = "manual_review_follow_up_required"
	}
	items := make([]orchestration.PRReviewItemOutcome, 0, len(reviewItems))
	for i := range reviewItems {
		items = append(items, orchestration.PRReviewItemOutcome{
			Item:       i + 1,
			Status:     "needs-human-follow-up",
			Summary:    message,
			NextAction: nextAction,
		})
	}
	return &orchestration.PRReviewOutcomeSummary{Items: items}
}

func appendRemainingReviewOutcomes(existing *orchestration.PRReviewOutcomeSummary, remainingItems []orchestration.ReviewFeedbackItem) *orchestration.PRReviewOutcomeSummary {
	if len(remainingItems) == 0 {
		return existing
	}
	items := make([]orchestration.PRReviewItemOutcome, 0, len(remainingItems))
	if existing != nil {
		items = append(items, existing.Items...)
	}
	start := len(items)
	for i, item := range remainingItems {
		summary := strings.TrimSpace(item.Body)
		if summary == "" {
			summary = "Actionable review item remains after PR-review worker attempt."
		}
		items = append(items, orchestration.PRReviewItemOutcome{
			Item:       start + i + 1,
			Status:     "not-fixed",
			Summary:    summary,
			NextAction: "manual_review_follow_up_required",
		})
	}
	return &orchestration.PRReviewOutcomeSummary{Items: items}
}

func prNeedsConflictRecovery(pullRequest lifecycle.PullRequest) bool {
	return orchestration.ClassifyPRMergeReadinessState(pullRequest.MergeStateStatus, pullRequest.Mergeable) == orchestration.MergeReadinessConflicting
}

func isGitPushRejected(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "fetch first") ||
		strings.Contains(message, "non-fast-forward") ||
		(strings.Contains(message, "failed to push some refs") && strings.Contains(message, "rejected"))
}

func (a *App) createNativePRIsolatedWorktree(ctx context.Context, repoRoot, branchName string) (string, error) {
	worktreeDir, err := os.MkdirTemp("", fmt.Sprintf("opencode-pr-%s-", sanitizeBranchForPath(branchName)))
	if err != nil {
		return "", err
	}
	created := false
	defer func() {
		if !created {
			_ = os.RemoveAll(worktreeDir)
		}
	}()
	localExists, err := a.gitLocalBranchExists(ctx, repoRoot, branchName)
	if err != nil {
		return "", err
	}
	if localExists {
		if _, err := a.runGit(ctx, repoRoot, "worktree", "add", worktreeDir, branchName); err != nil {
			return "", err
		}
		created = true
		return worktreeDir, nil
	}
	if _, err := a.runGit(ctx, repoRoot, "fetch", "origin", branchName); err != nil {
		return "", err
	}
	if _, err := a.runGit(ctx, repoRoot, "worktree", "add", "-b", branchName, worktreeDir, "origin/"+branchName); err != nil {
		return "", err
	}
	if _, err := a.runGit(ctx, worktreeDir, "branch", "--set-upstream-to", "origin/"+branchName, branchName); err != nil {
		return "", err
	}
	created = true
	return worktreeDir, nil
}

func (a *App) createNativePRFollowupBranch(ctx context.Context, repoRoot, branchName string) error {
	localExists, err := a.gitLocalBranchExists(ctx, repoRoot, branchName)
	if err != nil {
		return err
	}
	if localExists {
		if _, err := a.runGit(ctx, repoRoot, "checkout", branchName); err != nil {
			return err
		}
		_, _ = fmt.Fprintf(a.out, "Reusing existing follow-up branch: %s\n", branchName)
		return nil
	}
	if _, err := a.runGit(ctx, repoRoot, "checkout", "-b", branchName); err != nil {
		return err
	}
	_, _ = fmt.Fprintf(a.out, "Created follow-up branch: %s\n", branchName)
	return nil
}

func buildNativePRSummaryComment(reviewItemsCount int) string {
	return fmt.Sprintf("Automated follow-up completed.\n\n- Addressed review feedback items: %d\n- Please run another review pass for confirmation.", reviewItemsCount)
}

func sanitizeBranchForPath(branchName string) string {
	branchName = strings.TrimSpace(branchName)
	if branchName == "" {
		return "branch"
	}
	var b strings.Builder
	for _, r := range branchName {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '.' || r == '_' || r == '-' {
			b.WriteRune(r)
		} else {
			b.WriteByte('-')
		}
	}
	return strings.Trim(b.String(), "-")
}

func (a *App) rebasePRBranchAfterPushRejection(ctx context.Context, repoRoot, branchName string) (orchestration.ReusedBranchSyncVerdict, error) {
	remoteBranchRef := "origin/" + strings.TrimSpace(branchName)
	beforeSyncSHA, err := a.gitCurrentHeadSHA(ctx, repoRoot)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	if _, err := a.runGit(ctx, repoRoot, "fetch", "origin", branchName); err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	if _, err := a.runGit(ctx, repoRoot, "rebase", remoteBranchRef); err != nil {
		_, _ = a.gitCommandSucceeds(ctx, repoRoot, "rebase", "--abort")
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	afterSyncSHA, err := a.gitCurrentHeadSHA(ctx, repoRoot)
	if err != nil {
		return orchestration.ReusedBranchSyncVerdict{}, err
	}
	changed := beforeSyncSHA != afterSyncSHA
	return orchestration.ReusedBranchSyncVerdict{
		BranchName:        branchName,
		RemoteBaseRef:     remoteBranchRef,
		RequestedStrategy: "rebase",
		AppliedStrategy:   "rebase",
		Status:            branchSyncStatus(changed, false),
		Changed:           changed,
		AutoResolved:      false,
	}, nil
}

func nativePRCommitTitle(pullRequest lifecycle.PullRequest) string {
	return fmt.Sprintf("Address review comments for PR #%d", pullRequest.Number)
}

func (a *App) safePostPRComment(ctx context.Context, repo string, prNumber int, body string) error {
	return a.prLifecycle.CommentOnPullRequest(ctx, repo, prNumber, body)
}

func (a *App) safePostPRState(ctx context.Context, repo string, prNumber int, state orchestration.TrackedState) error {
	body, err := orchestration.BuildOrchestrationStateComment(state)
	if err != nil {
		return err
	}
	return a.safePostPRComment(ctx, repo, prNumber, body)
}
