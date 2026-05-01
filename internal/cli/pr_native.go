package cli

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/agentexec"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/githublifecycle"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

const (
	nativePRDefaultRunner = "opencode"
	nativePRDefaultAgent  = "review"
	nativePRDefaultModel  = "openai/gpt-4o"
)

type prReviewLifecycle interface {
	FetchPullRequest(ctx context.Context, repo string, number int) (githublifecycle.PullRequest, error)
	CommentOnPullRequest(ctx context.Context, repo string, number int, body string) error
	ReviewThreadsForPullRequest(ctx context.Context, repo string, number int) ([]githublifecycle.PullRequestReviewThread, error)
	ConversationCommentsForPullRequest(ctx context.Context, repo string, number int) ([]githublifecycle.PullRequestConversationComment, error)
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
		return "--detach is not supported by the Go-native PR path yet"
	}
	if *opts.common.dryRun {
		return "--dry-run is not supported by the Go-native PR path yet"
	}
	if opts.isolateWorktree {
		return "--isolate-worktree is not supported by the Go-native PR path yet"
	}
	if opts.postSummary {
		return "--post-pr-summary is not supported by the Go-native PR path yet"
	}
	if strings.TrimSpace(opts.followupPrefix) != "" {
		return "--pr-followup-branch-prefix is not supported by the Go-native PR path yet"
	}
	if opts.conflictRecoveryOnly {
		return "--conflict-recovery-only is not supported by the Go-native PR path yet"
	}
	if strings.TrimSpace(opts.syncStrategy) != "" {
		return "--sync-strategy is not supported by the Go-native PR path yet"
	}
	if strings.TrimSpace(*opts.common.local) != "" {
		return "--local-config is not supported by the Go-native PR path yet"
	}
	if strings.TrimSpace(*opts.common.project) != "" {
		return "--project-config is not supported by the Go-native PR path yet"
	}
	if strings.TrimSpace(*opts.common.preset) != "" {
		return "--preset is not supported by the Go-native PR path yet"
	}
	if tracker := strings.TrimSpace(*opts.common.tracker); tracker != "" && !strings.EqualFold(tracker, githublifecycle.TrackerGitHub) {
		return "native PR flow currently supports only the GitHub tracker"
	}
	if codehost := strings.TrimSpace(*opts.common.codehost); codehost != "" && !strings.EqualFold(codehost, githublifecycle.TrackerGitHub) {
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

	targetBranch := strings.TrimSpace(pullRequest.HeadRefName)
	if targetBranch == "" {
		_, _ = fmt.Fprintf(a.err, "orchestrator: PR #%d is missing head branch metadata\n", pullRequest.Number)
		return 1
	}
	activeBranch, err := a.gitCurrentBranch(ctx, repoRoot)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve current branch: %v\n", err)
		return 1
	}
	if activeBranch != targetBranch {
		if !opts.allowBranchSwitch {
			_, _ = fmt.Fprintf(a.err, "orchestrator: current branch %q does not match PR branch %q; rerun with --allow-pr-branch-switch or switch branches manually\n", activeBranch, targetBranch)
			return 1
		}
		if _, err := a.runGit(ctx, repoRoot, "checkout", targetBranch); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to switch to PR branch %q: %v\n", targetBranch, err)
			return 1
		}
		activeBranch = targetBranch
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
	failedState := func(attempt int, stage, nextAction, message string) orchestration.TrackedState {
		return orchestration.TrackedState{
			Status:     orchestration.StatusFailed,
			TaskType:   "pr",
			PR:         intPtr(pullRequest.Number),
			Branch:     activeBranch,
			BaseBranch: strings.TrimSpace(pullRequest.BaseRefName),
			Runner:     runnerName,
			Agent:      agentName,
			Model:      modelName,
			Attempt:    attempt,
			Stage:      stage,
			NextAction: nextAction,
			Error:      message,
			Timestamp:  time.Now().UTC().Format(time.RFC3339),
		}
	}

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		reviewItems, reviewStats, err := a.fetchNativePRReviewFeedback(ctx, repo, pullRequest)
		if err != nil {
			postState(failedState(attempt, "review_feedback", "inspect_review_feedback", err.Error()))
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
			postState(failedState(attempt, "agent_run", "inspect_git_status", err.Error()))
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
			postState(failedState(attempt, "agent_run", "inspect_agent_failure", message))
			_, _ = fmt.Fprintf(a.err, "orchestrator: agent failed for PR #%d: %v\n", pullRequest.Number, err)
			return 1
		}
		if result != nil && result.ExitCode != 0 {
			message := fmt.Sprintf("Agent exited with code %d", result.ExitCode)
			postState(failedState(attempt, "agent_run", "inspect_agent_failure", message))
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
					Status:     orchestration.StatusWaitingForAuthor,
					TaskType:   "pr",
					PR:         intPtr(pullRequest.Number),
					Branch:     activeBranch,
					BaseBranch: strings.TrimSpace(pullRequest.BaseRefName),
					Runner:     runnerName,
					Agent:      agentName,
					Model:      modelName,
					Attempt:    attempt,
					Stage:      "agent_run",
					NextAction: "await_author_reply",
					Error:      reason,
					Timestamp:  time.Now().UTC().Format(time.RFC3339),
					Stats:      statsMap(result.Stats),
				})
				_, _ = fmt.Fprintf(a.out, "Paused PR #%d for clarification: %s\n", pullRequest.Number, question)
				return 0
			}
		}

		hasChanges, err := a.gitHasChanges(ctx, repoRoot)
		if err != nil {
			postState(failedState(attempt, "commit_push", "inspect_git_status", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect post-agent changes: %v\n", err)
			return 1
		}
		if !hasChanges {
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
				Stage:      "post_agent_check",
				NextAction: "await_more_feedback_or_manual_changes",
				Error:      "Agent produced no repository changes",
				Timestamp:  time.Now().UTC().Format(time.RFC3339),
				Stats:      statsMap(result.Stats),
			})
			_, _ = fmt.Fprintf(a.out, "No changes detected for PR #%d; skipping commit and push\n", pullRequest.Number)
			return 0
		}

		if err := a.assertNativeGitContext(ctx, repoRoot, activeBranch, "commit PR review changes"); err != nil {
			postState(failedState(attempt, "commit_push", "restore_branch_context", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
			return 1
		}
		if err := a.gitStageIssueChanges(ctx, repoRoot, preRunUntracked); err != nil {
			postState(failedState(attempt, "commit_push", "inspect_stage_failure", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to stage PR changes: %v\n", err)
			return 1
		}
		if err := a.gitCommit(ctx, repoRoot, nativePRCommitTitle(pullRequest)); err != nil {
			postState(failedState(attempt, "commit_push", "inspect_commit_failure", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to commit PR changes: %v\n", err)
			return 1
		}
		if err := a.gitPushBranch(ctx, repoRoot, activeBranch, false); err != nil {
			postState(failedState(attempt, "commit_push", "inspect_push_failure", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to push PR branch %q: %v\n", activeBranch, err)
			return 1
		}

		postState(orchestration.TrackedState{
			Status:     orchestration.StatusWaitingForCI,
			TaskType:   "pr",
			PR:         intPtr(pullRequest.Number),
			Branch:     activeBranch,
			BaseBranch: strings.TrimSpace(pullRequest.BaseRefName),
			Runner:     runnerName,
			Agent:      agentName,
			Model:      modelName,
			Attempt:    attempt,
			Stage:      "pr_update",
			NextAction: "wait_for_ci",
			Timestamp:  time.Now().UTC().Format(time.RFC3339),
			Stats:      statsMap(result.Stats),
		})

		updatedPR, err := a.prLifecycle.FetchPullRequest(ctx, repo, pullRequest.Number)
		if err != nil {
			postState(failedState(attempt, "review_feedback", "inspect_review_feedback", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to refresh PR #%d after push: %v\n", pullRequest.Number, err)
			return 1
		}
		pullRequest = updatedPR
		remainingItems, remainingStats, err := a.fetchNativePRReviewFeedback(ctx, repo, pullRequest)
		if err != nil {
			postState(failedState(attempt, "review_feedback", "inspect_review_feedback", err.Error()))
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to refresh review feedback for PR #%d: %v\n", pullRequest.Number, err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "Review prompt sources: %s\n", orchestration.FormatReviewFeedbackStats(remainingStats))
		if len(remainingItems) == 0 {
			_, _ = fmt.Fprintf(a.out, "Done. Processed PR #%d with no remaining actionable review items after attempt %d.\n", pullRequest.Number, attempt)
			return 0
		}
		if attempt >= maxAttempts {
			postState(orchestration.TrackedState{
				Status:     orchestration.StatusBlocked,
				TaskType:   "pr",
				PR:         intPtr(pullRequest.Number),
				Branch:     activeBranch,
				BaseBranch: strings.TrimSpace(pullRequest.BaseRefName),
				Runner:     runnerName,
				Agent:      agentName,
				Model:      modelName,
				Attempt:    attempt,
				Stage:      "review_feedback",
				NextAction: "manual_review_follow_up_required",
				Error:      fmt.Sprintf("%d actionable review items remain after %d/%d attempts", len(remainingItems), attempt, maxAttempts),
				Timestamp:  time.Now().UTC().Format(time.RFC3339),
				Stats:      statsMap(result.Stats),
			})
			_, _ = fmt.Fprintf(a.out, "PR #%d still has %d actionable review items after %d/%d attempts; blocking for manual follow-up.\n", pullRequest.Number, len(remainingItems), attempt, maxAttempts)
			return 0
		}
		_, _ = fmt.Fprintf(a.out, "PR #%d still has %d actionable review items after attempt %d; continuing review feedback loop (%d/%d).\n", pullRequest.Number, len(remainingItems), attempt, attempt+1, maxAttempts)
	}

	return 0
}

func (a *App) fetchNativePRReviewFeedback(ctx context.Context, repo string, pullRequest githublifecycle.PullRequest) ([]orchestration.ReviewFeedbackItem, orchestration.ReviewFeedbackStats, error) {
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

func (a *App) loadLinkedIssueContext(ctx context.Context, repo string, pullRequest githublifecycle.PullRequest) []githublifecycle.Issue {
	if len(pullRequest.ClosingIssuesReferences) == 0 {
		return nil
	}
	linked := make([]githublifecycle.Issue, 0, len(pullRequest.ClosingIssuesReferences))
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

func buildNativePRReviewPrompt(pullRequest githublifecycle.PullRequest, reviewItems []orchestration.ReviewFeedbackItem, linkedIssues []githublifecycle.Issue, lightweight bool) string {
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
		"%s\nImplement the fix requested in PR review comments in repository files.\nDo not run git commands; git actions are handled by orchestration script.\n\nIf the requested change is ambiguous, unsafe, or needs product/business judgment, do not guess and do not wait for interactive approval. Instead, stop and print %s followed by a JSON object like {\"question\":\"<focused question>\",\"reason\":\"<why clarification is required>\"}.\n\nPull Request: #%d - %s\nPR URL: %s\n\nPR description:\n%s\n\nLinked issue context:\n%s\n\nReview comments to address:\n%s",
		firstLine,
		orchestration.ClarificationRequestMarker,
		pullRequest.Number,
		strings.TrimSpace(pullRequest.Title),
		strings.TrimSpace(pullRequest.URL),
		strings.TrimSpace(pullRequest.Body),
		strings.Join(issueContextLines, "\n"),
		strings.Join(commentLines, "\n"),
	))
}

func nativePRCommitTitle(pullRequest githublifecycle.PullRequest) string {
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
