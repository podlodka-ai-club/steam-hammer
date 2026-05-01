package cli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

type issueIDListFlag struct {
	ids []int
}

func (f *issueIDListFlag) String() string {
	if len(f.ids) == 0 {
		return ""
	}
	parts := make([]string, 0, len(f.ids))
	for _, id := range f.ids {
		parts = append(parts, strconv.Itoa(id))
	}
	return strings.Join(parts, ",")
}

func (f *issueIDListFlag) Set(value string) error {
	for _, raw := range strings.Split(value, ",") {
		trimmed := strings.TrimSpace(raw)
		if trimmed == "" {
			continue
		}
		id, err := strconv.Atoi(trimmed)
		if err != nil || id <= 0 {
			return fmt.Errorf("invalid issue id %q", trimmed)
		}
		duplicate := false
		for _, existing := range f.ids {
			if existing == id {
				duplicate = true
				break
			}
		}
		if !duplicate {
			f.ids = append(f.ids, id)
		}
	}
	return nil
}

func (a *App) runRun(ctx context.Context, args []string) int {
	if len(args) == 0 {
		_, _ = fmt.Fprint(a.err, runUsage())
		return 2
	}

	switch args[0] {
	case "-h", "--help", "help":
		_, _ = fmt.Fprint(a.out, runUsage())
		return 0
	case "issue":
		return a.runIssue(ctx, args[1:])
	case "batch":
		return a.runBatch(ctx, args[1:])
	case "daemon":
		return a.runDaemon(ctx, args[1:])
	case "pr":
		return a.runPR(ctx, args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown run target %q\n\n%s", args[0], runUsage())
		return 2
	}
}

func buildIssuePythonArgs(script string, opts commonOptions, id int, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch bool, syncStrategy string, fs flagState) []string {
	pythonArgs := []string{script, "--issue", strconv.Itoa(id)}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	return appendIssueRunArgs(pythonArgs, base, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch, syncStrategy, fs)
}

func buildIssueCLIArgs(opts commonOptions, id int, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch bool, syncStrategy string, fs flagState) []string {
	args := []string{"run", "issue", "--id", strconv.Itoa(id)}
	args = appendCommonPythonArgs(args, opts)
	return appendIssueRunArgs(args, base, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch, syncStrategy, fs)
}

func appendIssueRunArgs(args []string, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch bool, syncStrategy string, fs flagState) []string {
	if base != "" {
		args = append(args, "--base", base)
	}
	if includeEmpty {
		args = append(args, "--include-empty")
	}
	if stopOnError {
		args = append(args, "--stop-on-error")
	}
	if failOnExisting {
		args = append(args, "--fail-on-existing")
	}
	if forceIssueFlow {
		args = append(args, "--force-issue-flow")
	}
	if noSkipIfPRExists {
		args = append(args, "--no-skip-if-pr-exists")
	} else if fs.wasPassed("skip-if-pr-exists") && skipIfPRExists {
		args = append(args, "--skip-if-pr-exists")
	} else if !skipIfPRExists {
		args = append(args, "--no-skip-if-pr-exists")
	}
	if noSkipIfBranchExists {
		args = append(args, "--no-skip-if-branch-exists")
	} else if fs.wasPassed("skip-if-branch-exists") && skipIfBranchExists {
		args = append(args, "--skip-if-branch-exists")
	} else if !skipIfBranchExists {
		args = append(args, "--no-skip-if-branch-exists")
	}
	if forceReprocess {
		args = append(args, "--force-reprocess")
	}
	if conflictRecoveryOnly {
		args = append(args, "--conflict-recovery-only")
	}
	if noSyncReusedBranch {
		args = append(args, "--no-sync-reused-branch")
	} else if fs.wasPassed("sync-reused-branch") && syncReusedBranch {
		args = append(args, "--sync-reused-branch")
	} else if !syncReusedBranch {
		args = append(args, "--no-sync-reused-branch")
	}
	if syncStrategy != "" {
		args = append(args, "--sync-strategy", syncStrategy)
	}
	return args
}

func buildPRPythonArgs(script string, opts commonOptions, id int, allowBranchSwitch, isolateWorktree, postSummary bool, followupPrefix string, conflictRecoveryOnly bool, syncStrategy string) []string {
	pythonArgs := []string{script, "--pr", strconv.Itoa(id), "--from-review-comments"}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	return appendPRRunArgs(pythonArgs, allowBranchSwitch, isolateWorktree, postSummary, followupPrefix, conflictRecoveryOnly, syncStrategy)
}

func buildPRCLIArgs(opts commonOptions, id int, allowBranchSwitch, isolateWorktree, postSummary bool, followupPrefix string, conflictRecoveryOnly bool, syncStrategy string) []string {
	args := []string{"run", "pr", "--id", strconv.Itoa(id)}
	args = appendCommonPythonArgs(args, opts)
	return appendPRRunArgs(args, allowBranchSwitch, isolateWorktree, postSummary, followupPrefix, conflictRecoveryOnly, syncStrategy)
}

func appendPRRunArgs(args []string, allowBranchSwitch, isolateWorktree, postSummary bool, followupPrefix string, conflictRecoveryOnly bool, syncStrategy string) []string {
	if allowBranchSwitch {
		args = append(args, "--allow-pr-branch-switch")
	}
	if isolateWorktree {
		args = append(args, "--isolate-worktree")
	}
	if postSummary {
		args = append(args, "--post-pr-summary")
	}
	if followupPrefix != "" {
		args = append(args, "--pr-followup-branch-prefix", followupPrefix)
	}
	if syncStrategy != "" {
		args = append(args, "--sync-strategy", syncStrategy)
	}
	if conflictRecoveryOnly {
		args = append(args, "--conflict-recovery-only")
	}
	return args
}

func appendAutonomousSessionFile(args []string, sessionPath string) []string {
	if strings.TrimSpace(sessionPath) == "" {
		return args
	}
	return append(args, "--autonomous-session-file", sessionPath)
}

type flagState interface {
	wasPassed(name string) bool
}

type flagStateAdapter struct {
	fs interface{ Visit(func(*flag.Flag)) }
}

func (f flagStateAdapter) wasPassed(name string) bool {
	passed := false
	f.fs.Visit(func(current *flag.Flag) {
		if current.Name == name {
			passed = true
		}
	})
	return passed
}

func buildVerifyPythonArgs(script string, opts commonOptions, createFollowupIssue bool) []string {
	pythonArgs := []string{script, "--post-batch-verify"}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if createFollowupIssue {
		pythonArgs = append(pythonArgs, "--create-followup-issue")
	}
	return pythonArgs
}

func buildDaemonPythonArgs(script string, opts commonOptions, state string, limit int, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, syncReusedBranch, noSyncReusedBranch bool, syncStrategy, sessionPath string, postBatchVerify, createFollowupIssue bool, fs flagState) []string {
	pythonArgs := []string{script, "--autonomous", "--state", state, "--limit", strconv.Itoa(limit)}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if base != "" {
		pythonArgs = append(pythonArgs, "--base", base)
	}
	if includeEmpty {
		pythonArgs = append(pythonArgs, "--include-empty")
	}
	if stopOnError {
		pythonArgs = append(pythonArgs, "--stop-on-error")
	}
	if failOnExisting {
		pythonArgs = append(pythonArgs, "--fail-on-existing")
	}
	if forceIssueFlow {
		pythonArgs = append(pythonArgs, "--force-issue-flow")
	}
	if noSkipIfPRExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-pr-exists")
	} else if fs.wasPassed("skip-if-pr-exists") && skipIfPRExists {
		pythonArgs = append(pythonArgs, "--skip-if-pr-exists")
	} else if !skipIfPRExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-pr-exists")
	}
	if noSkipIfBranchExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-branch-exists")
	} else if fs.wasPassed("skip-if-branch-exists") && skipIfBranchExists {
		pythonArgs = append(pythonArgs, "--skip-if-branch-exists")
	} else if !skipIfBranchExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-branch-exists")
	}
	if forceReprocess {
		pythonArgs = append(pythonArgs, "--force-reprocess")
	}
	if noSyncReusedBranch {
		pythonArgs = append(pythonArgs, "--no-sync-reused-branch")
	} else if fs.wasPassed("sync-reused-branch") && syncReusedBranch {
		pythonArgs = append(pythonArgs, "--sync-reused-branch")
	} else if !syncReusedBranch {
		pythonArgs = append(pythonArgs, "--no-sync-reused-branch")
	}
	if syncStrategy != "" {
		pythonArgs = append(pythonArgs, "--sync-strategy", syncStrategy)
	}
	if sessionPath != "" {
		pythonArgs = append(pythonArgs, "--autonomous-session-file", sessionPath)
	}
	if postBatchVerify {
		pythonArgs = append(pythonArgs, "--post-batch-verify")
	}
	if createFollowupIssue {
		pythonArgs = append(pythonArgs, "--create-followup-issue")
	}
	return pythonArgs
}

func buildDaemonCLIArgs(opts commonOptions, state string, limit int, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, syncReusedBranch, noSyncReusedBranch bool, syncStrategy, sessionPath string, postBatchVerify, createFollowupIssue bool, fs flagState) []string {
	args := []string{"run", "daemon", "--state", state, "--limit", strconv.Itoa(limit)}
	args = appendCommonPythonArgs(args, opts)
	if base != "" {
		args = append(args, "--base", base)
	}
	if includeEmpty {
		args = append(args, "--include-empty")
	}
	if stopOnError {
		args = append(args, "--stop-on-error")
	}
	if failOnExisting {
		args = append(args, "--fail-on-existing")
	}
	if forceIssueFlow {
		args = append(args, "--force-issue-flow")
	}
	if noSkipIfPRExists {
		args = append(args, "--no-skip-if-pr-exists")
	} else if fs.wasPassed("skip-if-pr-exists") && skipIfPRExists {
		args = append(args, "--skip-if-pr-exists")
	} else if !skipIfPRExists {
		args = append(args, "--no-skip-if-pr-exists")
	}
	if noSkipIfBranchExists {
		args = append(args, "--no-skip-if-branch-exists")
	} else if fs.wasPassed("skip-if-branch-exists") && skipIfBranchExists {
		args = append(args, "--skip-if-branch-exists")
	} else if !skipIfBranchExists {
		args = append(args, "--no-skip-if-branch-exists")
	}
	if forceReprocess {
		args = append(args, "--force-reprocess")
	}
	if noSyncReusedBranch {
		args = append(args, "--no-sync-reused-branch")
	} else if fs.wasPassed("sync-reused-branch") && syncReusedBranch {
		args = append(args, "--sync-reused-branch")
	} else if !syncReusedBranch {
		args = append(args, "--no-sync-reused-branch")
	}
	if syncStrategy != "" {
		args = append(args, "--sync-strategy", syncStrategy)
	}
	if sessionPath != "" {
		args = append(args, "--autonomous-session-file", sessionPath)
	}
	if postBatchVerify {
		args = append(args, "--post-batch-verify")
	}
	if createFollowupIssue {
		args = append(args, "--create-followup-issue")
	}
	return args
}

func defaultSourceDir(configuredDir string) string {
	if strings.TrimSpace(configuredDir) != "" {
		return configuredDir
	}
	return "."
}

type daemonHandledState struct {
	signatures map[int]string
}

type daemonSelectedIssue struct {
	issueID    int
	signature  string
	workerName string
}

func loadDaemonHandledState(sessionPath string) daemonHandledState {
	state := daemonHandledState{signatures: map[int]string{}}
	if strings.TrimSpace(sessionPath) == "" {
		return state
	}
	raw, err := os.ReadFile(sessionPath)
	if err != nil || len(strings.TrimSpace(string(raw))) == 0 {
		return state
	}
	parsed, err := orchestration.ParseState(raw)
	if err != nil {
		return state
	}
	for key, value := range parsed.ProcessedIssues {
		id, err := strconv.Atoi(strings.TrimSpace(key))
		if err != nil || id <= 0 {
			continue
		}
		status := orchestration.ProcessedIssueStatus(value)
		signature := orchestration.ProcessedIssueSignature(value)
		if signature == "" {
			if status == "" {
				continue
			}
			signature = "state:" + status
		}
		if status == "" {
			continue
		}
		state.signatures[id] = signature
	}
	return state
}

func daemonWorkerName(slot int) string {
	if slot <= 1 {
		return "daemon"
	}
	return workerName("daemon", strconv.Itoa(slot))
}

func workerRuntimeLabel(name string) string {
	if name == "python3" {
		return "python runner"
	}
	return "orchestrator worker"
}

func shouldUseGoDaemonPolicy(opts commonOptions, lifecycle daemonLifecycle) bool {
	if lifecycle == nil {
		return false
	}
	if strings.TrimSpace(*opts.repo) == "" {
		return false
	}
	tracker := strings.TrimSpace(*opts.tracker)
	if tracker != "" && !strings.EqualFold(tracker, "github") {
		return false
	}
	return true
}

func daemonClaimTTL(pollIntervalSeconds int) time.Duration {
	if pollIntervalSeconds <= 0 {
		return 10 * time.Minute
	}
	ttl := time.Duration(pollIntervalSeconds*2) * time.Second
	if ttl < 2*time.Minute {
		return 2 * time.Minute
	}
	return ttl
}

type daemonParallelConfig struct {
	opts                 commonOptions
	flags                flagState
	runID                string
	state                string
	limit                int
	maxParallelTasks     int
	base                 string
	includeEmpty         bool
	stopOnError          bool
	failOnExisting       bool
	forceIssueFlow       bool
	skipIfPRExists       bool
	noSkipIfPRExists     bool
	skipIfBranchExists   bool
	noSkipIfBranchExists bool
	forceReprocess       bool
	syncReusedBranch     bool
	noSyncReusedBranch   bool
	syncStrategy         string
	postBatchVerify      bool
	createFollowupIssue  bool
	detach               bool
	workerDir            string
	sessionPath          string
	effectiveMaxCycles   int
	pollIntervalSeconds  int
}

type daemonParallelPreparedWorker struct {
	name       string
	issueID    int
	opts       commonOptions
	command    workerLaunchCommand
	pythonArgs []string
	workerPath detachedWorkerPaths
	cleanup    func()
}

type workerLaunchCommand struct {
	name           string
	args           []string
	fallbackReason string
}

func (a *App) currentExecutable() (string, error) {
	if a.executablePath == nil {
		return "", fmt.Errorf("orchestrator executable path is not configured")
	}
	return a.executablePath()
}

func (a *App) buildBatchWorkerLaunchCommand(ctx context.Context, opts commonOptions, id int, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch bool, syncStrategy string, fs flagState) workerLaunchCommand {
	pythonIssue := workerLaunchCommand{
		name: "python3",
		args: buildIssuePythonArgs(a.runtime.RunnerScript(), opts, id, base, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch, syncStrategy, fs),
	}
	if strings.TrimSpace(*opts.repo) == "" {
		pythonIssue.fallbackReason = "native worker dispatch requires --repo"
		return pythonIssue
	}
	if a.issueLifecycle == nil {
		pythonIssue.fallbackReason = "native worker dispatch requires issue lifecycle dependencies"
		return pythonIssue
	}

	issue, err := a.issueLifecycle.FetchIssue(ctx, strings.TrimSpace(*opts.repo), id)
	if err != nil {
		pythonIssue.fallbackReason = fmt.Sprintf("failed to inspect issue #%d for native dispatch: %v", id, err)
		return pythonIssue
	}
	comments, err := a.issueLifecycle.ListIssueComments(ctx, strings.TrimSpace(*opts.repo), id)
	if err != nil {
		pythonIssue.fallbackReason = fmt.Sprintf("failed to inspect issue #%d comments for native dispatch: %v", id, err)
		return pythonIssue
	}
	trackerComments := make([]orchestration.TrackerComment, 0, len(comments))
	for _, comment := range comments {
		trackerComments = append(trackerComments, orchestration.TrackerComment{ID: comment.ID, CreatedAt: comment.CreatedAt, HTMLURL: comment.HTMLURL, Body: comment.Body})
	}
	recoveredState, _ := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("issue #%d", id))
	linkedPR, err := a.issueLifecycle.FindOpenPullRequestForIssue(ctx, strings.TrimSpace(*opts.repo), issue)
	if err != nil {
		pythonIssue.fallbackReason = fmt.Sprintf("failed to inspect linked PR for issue #%d: %v", id, err)
		return pythonIssue
	}
	decision := orchestration.ChooseExecutionMode(id, linkedPRNumber(linkedPR), forceIssueFlow, parsedStatePayload(recoveredState), nil)

	if decision.Mode == orchestration.ExecutionModePRReview && linkedPR != nil {
		pythonPR := workerLaunchCommand{
			name: "python3",
			args: buildPRPythonArgs(a.runtime.RunnerScript(), opts, linkedPR.Number, false, false, false, "", false, ""),
		}
		if reason := nativePRFallbackReason(nativePROptions{prID: linkedPR.Number, common: opts}); reason != "" {
			pythonPR.fallbackReason = reason
			return pythonPR
		}
		execPath, err := a.currentExecutable()
		if err != nil {
			pythonPR.fallbackReason = fmt.Sprintf("failed to resolve orchestrator executable: %v", err)
			return pythonPR
		}
		return workerLaunchCommand{name: execPath, args: buildPRCLIArgs(opts, linkedPR.Number, false, false, false, "", false, "")}
	}

	if reason := nativeIssueFallbackReason(nativeIssueOptions{
		issueID:              id,
		common:               opts,
		base:                 base,
		includeEmpty:         includeEmpty,
		failOnExisting:       failOnExisting,
		forceIssueFlow:       forceIssueFlow,
		skipIfPRExists:       skipIfPRExists,
		noSkipIfPRExists:     noSkipIfPRExists,
		skipIfBranchExists:   skipIfBranchExists,
		noSkipIfBranchExists: noSkipIfBranchExists,
		forceReprocess:       forceReprocess,
		conflictRecoveryOnly: conflictRecoveryOnly,
		syncReusedBranch:     syncReusedBranch,
		noSyncReusedBranch:   noSyncReusedBranch,
		syncStrategy:         syncStrategy,
	}); reason != "" {
		pythonIssue.fallbackReason = reason
		return pythonIssue
	}
	execPath, err := a.currentExecutable()
	if err != nil {
		pythonIssue.fallbackReason = fmt.Sprintf("failed to resolve orchestrator executable: %v", err)
		return pythonIssue
	}
	return workerLaunchCommand{name: execPath, args: buildIssueCLIArgs(opts, id, base, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch, syncStrategy, fs)}
}

func (a *App) selectDaemonIssues(ctx context.Context, config daemonParallelConfig, handled daemonHandledState) ([]daemonSelectedIssue, error) {
	issues, err := a.daemon.ListIssues(ctx, strings.TrimSpace(*config.opts.repo), config.state, config.limit)
	if err != nil {
		return nil, err
	}
	selected := make([]daemonSelectedIssue, 0, config.maxParallelTasks)
	seen := make(map[int]struct{}, config.maxParallelTasks)
	now := time.Now().UTC()
	for _, issue := range issues {
		if len(selected) >= config.maxParallelTasks {
			break
		}
		if _, ok := seen[issue.Number]; ok {
			continue
		}
		comments, err := a.daemon.ListIssueComments(ctx, strings.TrimSpace(*config.opts.repo), issue.Number)
		if err != nil {
			return nil, err
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
		latestState, _ := orchestration.SelectLatestParseableOrchestrationState(trackerComments, fmt.Sprintf("issue #%d", issue.Number))
		latestClaim, _ := orchestration.SelectLatestParseableOrchestrationClaim(trackerComments, fmt.Sprintf("issue #%d", issue.Number))
		latestDecomposition, _ := orchestration.SelectLatestParseableDecompositionPlan(trackerComments, fmt.Sprintf("issue #%d", issue.Number))

		snapshot := orchestration.DaemonTaskSnapshot{
			IssueNumber:          issue.Number,
			RunID:                config.runID,
			ForceReprocess:       config.forceReprocess,
			LastHandledSignature: handled.signatures[issue.Number],
		}
		if latestState != nil {
			snapshot.LatestStateStatus = latestState.Status
			snapshot.LatestStateTaskType = latestState.Payload.TaskType
		}
		if latestClaim != nil {
			snapshot.LatestClaim = latestClaim.Payload
		}
		if latestDecomposition != nil {
			snapshot.LatestDecomposition = latestDecomposition.Payload
		}
		decision := orchestration.EvaluateDaemonTaskSelection(snapshot, now)
		if !decision.Eligible {
			continue
		}
		selected = append(selected, daemonSelectedIssue{
			issueID:    issue.Number,
			signature:  decision.Signature,
			workerName: daemonWorkerName(len(selected) + 1),
		})
		seen[issue.Number] = struct{}{}
	}
	return selected, nil
}

func (a *App) claimDaemonIssues(ctx context.Context, config daemonParallelConfig, selected []daemonSelectedIssue) ([]daemonSelectedIssue, error) {
	if *config.opts.dryRun || len(selected) == 0 {
		return selected, nil
	}
	claimed := make([]daemonSelectedIssue, 0, len(selected))
	ttl := daemonClaimTTL(config.pollIntervalSeconds)
	for _, issue := range selected {
		claimedAt := time.Now().UTC()
		expiresAt := claimedAt.Add(ttl)
		if err := a.daemon.CommentOnIssue(ctx, strings.TrimSpace(*config.opts.repo), issue.issueID, orchestration.BuildDaemonClaimComment(issue.issueID, config.runID, issue.workerName, claimedAt, expiresAt)); err != nil {
			return claimed, err
		}
		comments, err := a.daemon.ListIssueComments(ctx, strings.TrimSpace(*config.opts.repo), issue.issueID)
		if err != nil {
			return claimed, err
		}
		trackerComments := make([]orchestration.TrackerComment, 0, len(comments))
		for _, comment := range comments {
			trackerComments = append(trackerComments, orchestration.TrackerComment{ID: comment.ID, CreatedAt: comment.CreatedAt, HTMLURL: comment.HTMLURL, Body: comment.Body})
		}
		latestClaim, _ := orchestration.SelectLatestParseableOrchestrationClaim(trackerComments, fmt.Sprintf("issue #%d", issue.issueID))
		if latestClaim == nil || daemonPayloadStatus(latestClaim.Payload, "status") != orchestration.DaemonClaimStatusClaimed || daemonPayloadString(latestClaim.Payload, "run_id") != config.runID || daemonPayloadString(latestClaim.Payload, "worker") != issue.workerName {
			continue
		}
		claimed = append(claimed, issue)
	}
	return claimed, nil
}

func (a *App) releaseDaemonClaims(ctx context.Context, config daemonParallelConfig, selected []daemonSelectedIssue) error {
	if *config.opts.dryRun || len(selected) == 0 {
		return nil
	}
	var firstErr error
	for _, issue := range selected {
		if err := a.daemon.CommentOnIssue(ctx, strings.TrimSpace(*config.opts.repo), issue.issueID, orchestration.BuildDaemonReleaseComment(issue.issueID, config.runID, issue.workerName, time.Now().UTC())); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func daemonPayloadStatus(payload map[string]any, key string) string {
	return strings.ToLower(strings.TrimSpace(daemonPayloadString(payload, key)))
}

func daemonPayloadString(payload map[string]any, key string) string {
	if payload == nil {
		return ""
	}
	value, _ := payload[key].(string)
	return strings.TrimSpace(value)
}

func (a *App) prepareParallelDaemonWorker(ctx context.Context, config daemonParallelConfig, issue daemonSelectedIssue, sessionPath string) (daemonParallelPreparedWorker, error) {
	workerOpts := config.opts
	if config.detach {
		workerPaths, err := resolveDetachedWorkerPaths(config.workerDir, *config.opts.dir, "daemon", strings.TrimPrefix(issue.workerName, "daemon-"))
		if err != nil {
			return daemonParallelPreparedWorker{}, fmt.Errorf("failed to resolve detached worker paths: %w", err)
		}
		clonePath, err := a.prepareDetachedWorkerClone(*config.opts.dir, workerPaths)
		if err != nil {
			return daemonParallelPreparedWorker{}, fmt.Errorf("failed to prepare detached worker clone: %w", err)
		}
		workerOpts = withCommonOptionsDir(workerOpts, clonePath)
		return daemonParallelPreparedWorker{
			name:    issue.workerName,
			issueID: issue.issueID,
			opts:    workerOpts,
			command: func() workerLaunchCommand {
				command := a.buildBatchWorkerLaunchCommand(ctx, workerOpts, issue.issueID, config.base, config.includeEmpty, config.stopOnError, config.failOnExisting, config.forceIssueFlow, config.skipIfPRExists, config.noSkipIfPRExists, config.skipIfBranchExists, config.noSkipIfBranchExists, config.forceReprocess, false, config.syncReusedBranch, config.noSyncReusedBranch, config.syncStrategy, config.flags)
				command.args = appendAutonomousSessionFile(command.args, workerPaths.SessionPath)
				return command
			}(),
			workerPath: workerPaths,
		}, nil
	}

	tempRoot, err := os.MkdirTemp("", "orchestrator-daemon-worker-*")
	if err != nil {
		return daemonParallelPreparedWorker{}, fmt.Errorf("failed to create daemon worker temp directory: %w", err)
	}
	cleanup := func() {
		_ = os.RemoveAll(tempRoot)
	}
	clonePath := filepath.Join(tempRoot, "repo")
	if _, err := a.clone.Prepare(defaultSourceDir(*config.opts.dir), clonePath); err != nil {
		cleanup()
		return daemonParallelPreparedWorker{}, fmt.Errorf("failed to prepare daemon worker clone: %w", err)
	}
	sessionFile, err := os.CreateTemp(tempRoot, "autonomous-session-*.json")
	if err != nil {
		cleanup()
		return daemonParallelPreparedWorker{}, fmt.Errorf("failed to create daemon worker session file: %w", err)
	}
	workerSessionPath := sessionFile.Name()
	_ = sessionFile.Close()
	workerOpts = withCommonOptionsDir(workerOpts, clonePath)
	return daemonParallelPreparedWorker{
		name:    issue.workerName,
		issueID: issue.issueID,
		opts:    workerOpts,
		command: func() workerLaunchCommand {
			command := a.buildBatchWorkerLaunchCommand(ctx, workerOpts, issue.issueID, config.base, config.includeEmpty, config.stopOnError, config.failOnExisting, config.forceIssueFlow, config.skipIfPRExists, config.noSkipIfPRExists, config.skipIfBranchExists, config.noSkipIfBranchExists, config.forceReprocess, false, config.syncReusedBranch, config.noSyncReusedBranch, config.syncStrategy, config.flags)
			command.args = appendAutonomousSessionFile(command.args, workerSessionPath)
			return command
		}(),
		cleanup: cleanup,
	}, nil
}

func (a *App) runParallelDaemon(ctx context.Context, config daemonParallelConfig) int {
	if config.maxParallelTasks <= 0 {
		return 0
	}
	if a.clone == nil {
		_, _ = fmt.Fprintln(a.err, "orchestrator: detached batch clone preparer is not configured")
		return 1
	}

	cycles := 0
	lastCode := 0
	handled := loadDaemonHandledState(config.sessionPath)
	for {
		cycles++
		selectionFailed := false
		selected, err := a.selectDaemonIssues(ctx, config, handled)
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to select daemon issues: %v\n", err)
			if config.stopOnError {
				return 1
			}
			lastCode = 1
			selectionFailed = true
		}
		if !selectionFailed {
			selected, err = a.claimDaemonIssues(ctx, config, selected)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to claim daemon issues: %v\n", err)
				if config.stopOnError {
					return 1
				}
				lastCode = 1
				selectionFailed = true
			}
		}
		preparedWorkers := make([]daemonParallelPreparedWorker, 0, len(selected))
		if !selectionFailed {
			for _, issue := range selected {
				handled.signatures[issue.issueID] = issue.signature
				worker, err := a.prepareParallelDaemonWorker(ctx, config, issue, config.sessionPath)
				if err != nil {
					_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
					lastCode = 1
					if config.stopOnError {
						for _, prepared := range preparedWorkers {
							if prepared.cleanup != nil {
								prepared.cleanup()
							}
						}
						return lastCode
					}
					continue
				}
				preparedWorkers = append(preparedWorkers, worker)
			}
		}

		cycleCode := 0
		if !selectionFailed {
			cycleCtx := ctx
			cancel := func() {}
			if config.stopOnError {
				cycleCtx, cancel = context.WithCancel(ctx)
			}
			var wg sync.WaitGroup
			var mu sync.Mutex
			for _, worker := range preparedWorkers {
				worker := worker
				if worker.command.fallbackReason != "" {
					_, _ = fmt.Fprintf(a.err, "orchestrator: falling back to python worker for issue #%d: %s\n", worker.issueID, worker.command.fallbackReason)
				}
				wg.Add(1)
				go func() {
					defer wg.Done()
					code := a.runSubprocess(cycleCtx, workerRuntimeLabel(worker.command.name), worker.command.name, worker.command.args)
					if worker.cleanup != nil {
						worker.cleanup()
					}
					if code == 0 {
						return
					}
					mu.Lock()
					if cycleCode == 0 {
						cycleCode = code
					}
					mu.Unlock()
					if config.stopOnError {
						cancel()
					}
				}()
			}
			wg.Wait()
			cancel()
			if err := a.releaseDaemonClaims(ctx, config, selected); err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to release daemon claims: %v\n", err)
				if cycleCode == 0 {
					cycleCode = 1
				}
			}
		}

		if cycleCode == 0 && config.postBatchVerify {
			verification, err := a.runPostBatchVerification(ctx, config.opts, config.createFollowupIssue, config.sessionPath)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: post-batch verification failed: %v\n", err)
				cycleCode = 1
			} else if strings.EqualFold(strings.TrimSpace(verification.Status), orchestration.StatusFailed) {
				cycleCode = 1
			}
		}
		if cycleCode != 0 {
			lastCode = cycleCode
			if config.stopOnError {
				return cycleCode
			}
			_, _ = fmt.Fprintf(a.err, "orchestrator: daemon poll cycle %d exited with code %d\n", cycles, cycleCode)
		}
		if config.effectiveMaxCycles > 0 && cycles >= config.effectiveMaxCycles {
			return lastCode
		}

		select {
		case <-ctx.Done():
			if errors.Is(ctx.Err(), context.Canceled) {
				_, _ = fmt.Fprintln(a.err, "orchestrator: daemon canceled")
				return 130
			}
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				_, _ = fmt.Fprintln(a.err, "orchestrator: daemon timed out")
				return 124
			}
			return 1
		case <-time.After(time.Duration(config.pollIntervalSeconds) * time.Second):
		}
	}
}

func (a *App) runSerialDaemon(ctx context.Context, config daemonParallelConfig) int {
	handled := loadDaemonHandledState(config.sessionPath)
	cycles := 0
	lastCode := 0
	for {
		cycles++
		selected, err := a.selectDaemonIssues(ctx, config, handled)
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to select daemon issues: %v\n", err)
			if config.stopOnError {
				return 1
			}
			lastCode = 1
		} else {
			selected, err = a.claimDaemonIssues(ctx, config, selected)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to claim daemon issues: %v\n", err)
				if config.stopOnError {
					return 1
				}
				lastCode = 1
				selected = nil
			}
			cycleCode := 0
			for _, issue := range selected {
				handled.signatures[issue.issueID] = issue.signature
				command := a.buildBatchWorkerLaunchCommand(ctx, config.opts, issue.issueID, config.base, config.includeEmpty, config.stopOnError, config.failOnExisting, config.forceIssueFlow, config.skipIfPRExists, config.noSkipIfPRExists, config.skipIfBranchExists, config.noSkipIfBranchExists, config.forceReprocess, false, config.syncReusedBranch, config.noSyncReusedBranch, config.syncStrategy, config.flags)
				if command.fallbackReason != "" {
					_, _ = fmt.Fprintf(a.err, "orchestrator: falling back to python worker for issue #%d: %s\n", issue.issueID, command.fallbackReason)
				}
				command.args = appendAutonomousSessionFile(command.args, config.sessionPath)
				code := a.runSubprocess(ctx, workerRuntimeLabel(command.name), command.name, command.args)
				if code != 0 && cycleCode == 0 {
					cycleCode = code
				}
				if code != 0 && config.stopOnError {
					_ = a.releaseDaemonClaims(ctx, config, selected)
					return code
				}
			}
			if err := a.releaseDaemonClaims(ctx, config, selected); err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to release daemon claims: %v\n", err)
				if cycleCode == 0 {
					cycleCode = 1
				}
			}
			if cycleCode == 0 && config.postBatchVerify {
				verification, err := a.runPostBatchVerification(ctx, config.opts, config.createFollowupIssue, config.sessionPath)
				if err != nil {
					_, _ = fmt.Fprintf(a.err, "orchestrator: post-batch verification failed: %v\n", err)
					cycleCode = 1
				} else if strings.EqualFold(strings.TrimSpace(verification.Status), orchestration.StatusFailed) {
					cycleCode = 1
				}
			}
			if cycleCode != 0 {
				lastCode = cycleCode
				if config.stopOnError {
					return cycleCode
				}
				_, _ = fmt.Fprintf(a.err, "orchestrator: daemon poll cycle %d exited with code %d\n", cycles, cycleCode)
			}
		}
		if config.effectiveMaxCycles > 0 && cycles >= config.effectiveMaxCycles {
			return lastCode
		}

		select {
		case <-ctx.Done():
			if errors.Is(ctx.Err(), context.Canceled) {
				_, _ = fmt.Fprintln(a.err, "orchestrator: daemon canceled")
				return 130
			}
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				_, _ = fmt.Fprintln(a.err, "orchestrator: daemon timed out")
				return 124
			}
			return 1
		case <-time.After(time.Duration(config.pollIntervalSeconds) * time.Second):
		}
	}
}

func (a *App) runDaemon(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedRunDaemonFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("run daemon", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	state := fs.String("state", "open", "issue state to poll: open, closed, or all")
	limit := fs.Int("limit", 10, "maximum number of issues to scan per poll")
	pollIntervalSeconds := fs.Int("poll-interval-seconds", 120, "delay between autonomous polls")
	maxParallelTasks := fs.Int("max-parallel-tasks", 1, "maximum parallel autonomous workers to run per poll cycle")
	maxCycles := fs.Int("max-cycles", 0, "optional test/debug bound on daemon polling cycles")
	includeEmpty := fs.Bool("include-empty", false, "process issues even if body is empty")
	stopOnError := fs.Bool("stop-on-error", false, "stop after the first failed poll cycle")
	failOnExisting := fs.Bool("fail-on-existing", false, "fail if issue branch or PR already exists")
	forceIssueFlow := fs.Bool("force-issue-flow", false, "disable auto-switch to PR-review mode")
	skipIfPRExists := fs.Bool("skip-if-pr-exists", true, "skip issue processing when a linked open PR exists")
	noSkipIfPRExists := fs.Bool("no-skip-if-pr-exists", false, "do not skip issue processing when a linked open PR exists")
	skipIfBranchExists := fs.Bool("skip-if-branch-exists", true, "skip issue processing when deterministic issue branch exists on origin")
	noSkipIfBranchExists := fs.Bool("no-skip-if-branch-exists", false, "do not skip issue processing when deterministic issue branch exists on origin")
	forceReprocess := fs.Bool("force-reprocess", false, "override skip guards during autonomous polling")
	syncReusedBranch := fs.Bool("sync-reused-branch", true, "sync reused issue branches before running the agent")
	noSyncReusedBranch := fs.Bool("no-sync-reused-branch", false, "disable sync for reused issue branches before the agent step")
	syncStrategy := fs.String("sync-strategy", "", "reused branch sync strategy: rebase or merge")
	base := ""
	fs.StringVar(&base, "base", "", "base branch mode: default or current")
	fs.StringVar(&base, "base-branch", "", "base branch mode: default or current")
	postBatchVerify := fs.Bool("post-batch-verify", false, "run post-batch verification after the daemon cycle completes")
	createFollowupIssue := fs.Bool("create-followup-issue", false, a.runtime.FollowUpIssueFlagDescription("post-batch verification"))
	detach := fs.Bool("detach", false, "start the worker in the background and write logs/state to a predictable path")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")
	autonomousSessionFile := fs.String("autonomous-session-file", "", "internal autonomous session checkpoint path")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected run daemon argument: %s\n", fs.Arg(0))
		return 2
	}
	if *pollIntervalSeconds < 0 {
		_, _ = fmt.Fprintln(a.err, "run daemon requires --poll-interval-seconds to be zero or greater")
		return 2
	}
	if *pollIntervalSeconds == 0 && !*opts.dryRun && *maxCycles != 1 {
		_, _ = fmt.Fprintln(a.err, "run daemon requires --poll-interval-seconds > 0 unless dry-run or --max-cycles=1")
		return 2
	}
	if *limit <= 0 {
		_, _ = fmt.Fprintln(a.err, "run daemon requires --limit > 0")
		return 2
	}
	if *maxParallelTasks <= 0 {
		_, _ = fmt.Fprintln(a.err, "run daemon requires --max-parallel-tasks > 0")
		return 2
	}
	if *state != "open" && *state != "closed" && *state != "all" {
		_, _ = fmt.Fprintln(a.err, "run daemon requires --state to be one of: open, closed, all")
		return 2
	}

	flags := flagStateAdapter{fs: fs}
	effectiveMaxCycles := *maxCycles
	if *opts.dryRun && effectiveMaxCycles == 0 {
		effectiveMaxCycles = 1
	}
	if *detach && effectiveMaxCycles == 0 {
		effectiveMaxCycles = 1
	}
	if *detach {
		execPath, err := a.currentExecutable()
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve orchestrator executable: %v\n", err)
			return 1
		}
		lastCode := 0
		for workerIndex := 1; workerIndex <= *maxParallelTasks; workerIndex++ {
			workerID := ""
			workerLabel := "daemon"
			if *maxParallelTasks > 1 {
				workerID = strconv.Itoa(workerIndex)
				workerLabel = workerName("daemon", workerID)
			}
			workerPaths, err := resolveDetachedWorkerPaths(*workerDir, *opts.dir, "daemon", workerID)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
				return 1
			}
			clonePath, err := a.prepareDetachedWorkerClone(*opts.dir, workerPaths)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to prepare detached worker clone for %s: %v\n", workerLabel, err)
				if *stopOnError {
					return 1
				}
				lastCode = 1
				continue
			}
			daemonOpts := withCommonOptionsDir(opts, clonePath)
			workerArgs := buildDaemonCLIArgs(
				daemonOpts,
				*state,
				*limit,
				base,
				*includeEmpty,
				*stopOnError,
				*failOnExisting,
				*forceIssueFlow,
				*skipIfPRExists,
				*noSkipIfPRExists,
				*skipIfBranchExists,
				*noSkipIfBranchExists,
				*forceReprocess,
				*syncReusedBranch,
				*noSyncReusedBranch,
				*syncStrategy,
				workerPaths.SessionPath,
				*postBatchVerify,
				*createFollowupIssue,
				flags,
			)
			workerArgs = append(workerArgs, "--max-parallel-tasks", "1", "--max-cycles", "1")
			state := detachedWorkerStateFromOptions(
				workerLabel,
				"run daemon",
				"daemon",
				workerID,
				daemonOpts,
				append([]string{execPath}, workerArgs...),
				workerPaths,
			)
			state.WorkDir = clonePath
			if _, code := a.startDetachedWorkerState(state); code != 0 {
				if *stopOnError {
					return code
				}
				lastCode = code
			}
		}
		return lastCode
	}

	sessionPath := strings.TrimSpace(*autonomousSessionFile)
	cleanupSession := func() {}
	if sessionPath == "" {
		sessionFile, err := os.CreateTemp("", "orchestrator-daemon-session-*.json")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create daemon session file: %v\n", err)
			return 1
		}
		sessionPath = sessionFile.Name()
		_ = sessionFile.Close()
		cleanupSession = func() { _ = os.Remove(sessionPath) }
	}
	defer cleanupSession()

	if !shouldUseGoDaemonPolicy(opts, a.daemon) {
		pythonArgs := buildDaemonPythonArgs(a.runtime.RunnerScript(), opts, *state, *limit, base, *includeEmpty, *stopOnError, *failOnExisting, *forceIssueFlow, *skipIfPRExists, *noSkipIfPRExists, *skipIfBranchExists, *noSkipIfBranchExists, *forceReprocess, *syncReusedBranch, *noSyncReusedBranch, *syncStrategy, sessionPath, *postBatchVerify, *createFollowupIssue, flags)

		cycles := 0
		for {
			cycles++
			code := a.runPython(ctx, pythonArgs)
			if code != 0 {
				if *stopOnError {
					return code
				}
				_, _ = fmt.Fprintf(a.err, "orchestrator: daemon poll cycle %d exited with code %d\n", cycles, code)
			}
			if effectiveMaxCycles > 0 && cycles >= effectiveMaxCycles {
				return code
			}

			select {
			case <-ctx.Done():
				if errors.Is(ctx.Err(), context.Canceled) {
					_, _ = fmt.Fprintln(a.err, "orchestrator: daemon canceled")
					return 130
				}
				if errors.Is(ctx.Err(), context.DeadlineExceeded) {
					_, _ = fmt.Fprintln(a.err, "orchestrator: daemon timed out")
					return 124
				}
				return 1
			case <-time.After(time.Duration(*pollIntervalSeconds) * time.Second):
			}
		}
	}

	if *maxParallelTasks > 1 {
		return a.runParallelDaemon(ctx, daemonParallelConfig{
			opts:                 opts,
			flags:                flags,
			runID:                fmt.Sprintf("run-%d", time.Now().UTC().UnixNano()),
			state:                *state,
			limit:                *limit,
			maxParallelTasks:     *maxParallelTasks,
			base:                 base,
			includeEmpty:         *includeEmpty,
			stopOnError:          *stopOnError,
			failOnExisting:       *failOnExisting,
			forceIssueFlow:       *forceIssueFlow,
			skipIfPRExists:       *skipIfPRExists,
			noSkipIfPRExists:     *noSkipIfPRExists,
			skipIfBranchExists:   *skipIfBranchExists,
			noSkipIfBranchExists: *noSkipIfBranchExists,
			forceReprocess:       *forceReprocess,
			syncReusedBranch:     *syncReusedBranch,
			noSyncReusedBranch:   *noSyncReusedBranch,
			syncStrategy:         *syncStrategy,
			postBatchVerify:      *postBatchVerify,
			createFollowupIssue:  *createFollowupIssue,
			workerDir:            *workerDir,
			sessionPath:          sessionPath,
			effectiveMaxCycles:   effectiveMaxCycles,
			pollIntervalSeconds:  *pollIntervalSeconds,
		})
	}
	return a.runSerialDaemon(ctx, daemonParallelConfig{
		opts:                 opts,
		flags:                flags,
		runID:                fmt.Sprintf("run-%d", time.Now().UTC().UnixNano()),
		state:                *state,
		limit:                *limit,
		maxParallelTasks:     1,
		base:                 base,
		includeEmpty:         *includeEmpty,
		stopOnError:          *stopOnError,
		failOnExisting:       *failOnExisting,
		forceIssueFlow:       *forceIssueFlow,
		skipIfPRExists:       *skipIfPRExists,
		noSkipIfPRExists:     *noSkipIfPRExists,
		skipIfBranchExists:   *skipIfBranchExists,
		noSkipIfBranchExists: *noSkipIfBranchExists,
		forceReprocess:       *forceReprocess,
		syncReusedBranch:     *syncReusedBranch,
		noSyncReusedBranch:   *noSyncReusedBranch,
		syncStrategy:         *syncStrategy,
		postBatchVerify:      *postBatchVerify,
		createFollowupIssue:  *createFollowupIssue,
		workerDir:            *workerDir,
		sessionPath:          sessionPath,
		effectiveMaxCycles:   effectiveMaxCycles,
		pollIntervalSeconds:  *pollIntervalSeconds,
	})
}

func (a *App) runBatch(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedRunBatchFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("run batch", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	ids := &issueIDListFlag{}
	fs.Var(ids, "ids", "comma-separated issue IDs")
	fs.Var(ids, "id", "issue ID; repeatable and accepts comma-separated values")
	fs.Var(ids, "issue", "compatibility alias for --id")
	base := ""
	fs.StringVar(&base, "base", "", "base branch mode: default or current")
	fs.StringVar(&base, "base-branch", "", "base branch mode: default or current")
	includeEmpty := fs.Bool("include-empty", false, "process issues even if body is empty")
	stopOnError := fs.Bool("stop-on-error", false, "stop after first failed issue launch")
	failOnExisting := fs.Bool("fail-on-existing", false, "fail if issue branch or PR already exists")
	forceIssueFlow := fs.Bool("force-issue-flow", false, "disable auto-switch to PR-review mode")
	skipIfPRExists := fs.Bool("skip-if-pr-exists", true, "skip issue processing when a linked open PR exists")
	noSkipIfPRExists := fs.Bool("no-skip-if-pr-exists", false, "do not skip issue processing when a linked open PR exists")
	skipIfBranchExists := fs.Bool("skip-if-branch-exists", true, "skip issue processing when deterministic issue branch exists on origin")
	noSkipIfBranchExists := fs.Bool("no-skip-if-branch-exists", false, "do not skip issue processing when deterministic issue branch exists on origin")
	forceReprocess := fs.Bool("force-reprocess", false, "override skip guards")
	conflictRecoveryOnly := fs.Bool("conflict-recovery-only", false, "sync an existing reused branch with base and stop before any agent work")
	syncReusedBranch := fs.Bool("sync-reused-branch", true, "sync reused issue branches before running the agent")
	noSyncReusedBranch := fs.Bool("no-sync-reused-branch", false, "disable sync for reused issue branches before the agent step")
	syncStrategy := fs.String("sync-strategy", "", "reused branch sync strategy: rebase or merge")
	detach := fs.Bool("detach", false, "start one detached worker per issue and write logs/state to predictable paths")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected run batch argument: %s\n", fs.Arg(0))
		return 2
	}
	if len(ids.ids) == 0 {
		_, _ = fmt.Fprintln(a.err, "run batch requires at least one issue id via --ids or repeated --id")
		return 2
	}
	if !*detach && !*opts.dryRun {
		_, _ = fmt.Fprintln(a.err, "run batch requires --detach for live launches; use --dry-run to preview without starting workers")
		return 2
	}

	flags := flagStateAdapter{fs: fs}
	lastCode := 0
	launchedStates := make([]detachedWorkerState, 0, len(ids.ids))
	for _, id := range ids.ids {
		issueOpts := opts
		workerPaths := detachedWorkerPaths{}
		clonePath := strings.TrimSpace(*opts.dir)
		if *detach {
			var err error
			workerPaths, err = resolveDetachedWorkerPaths(*workerDir, *opts.dir, "issue", strconv.Itoa(id))
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
				return 1
			}
			clonePath, err = a.prepareDetachedWorkerClone(*opts.dir, workerPaths)
			if err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to prepare detached worker clone for issue %d: %v\n", id, err)
				if *stopOnError {
					return 1
				}
				lastCode = 1
				continue
			}
			issueOpts = withCommonOptionsDir(issueOpts, clonePath)
		}
		command := a.buildBatchWorkerLaunchCommand(
			ctx,
			issueOpts,
			id,
			base,
			*includeEmpty,
			*stopOnError,
			*failOnExisting,
			*forceIssueFlow,
			*skipIfPRExists,
			*noSkipIfPRExists,
			*skipIfBranchExists,
			*noSkipIfBranchExists,
			*forceReprocess,
			*conflictRecoveryOnly,
			*syncReusedBranch,
			*noSyncReusedBranch,
			*syncStrategy,
			flags,
		)
		if command.fallbackReason != "" {
			_, _ = fmt.Fprintf(a.err, "orchestrator: falling back to python worker for issue #%d: %s\n", id, command.fallbackReason)
		}
		if *detach {
			state := detachedWorkerStateFromOptions(
				workerName("issue", strconv.Itoa(id)),
				"run batch",
				"issue",
				strconv.Itoa(id),
				issueOpts,
				append([]string{command.name}, command.args...),
				workerPaths,
			)
			state.WorkDir = clonePath
			startedState, code := a.startDetachedWorkerState(state)
			if code != 0 {
				if *stopOnError {
					return code
				}
				lastCode = code
				continue
			}
			launchedStates = append(launchedStates, startedState)
			launchedStates = withDetachedBatchMetadata(launchedStates, ids.ids)
			if err := writeDetachedBatchStates(launchedStates); err != nil {
				_, _ = fmt.Fprintf(a.err, "orchestrator: failed to write detached batch metadata: %v\n", err)
				if *stopOnError {
					return 1
				}
				lastCode = 1
			}
			continue
		}
		code := a.runSubprocess(ctx, workerRuntimeLabel(command.name), command.name, command.args)
		if code != 0 {
			if *stopOnError {
				return code
			}
			lastCode = code
		}
	}
	return lastCode
}

func (a *App) runIssue(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedRunIssueFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("run issue", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	id := fs.Int("id", 0, a.runtime.IssueFlagDescription())
	issue := fs.Int("issue", 0, "compatibility alias for --id")
	base := ""
	fs.StringVar(&base, "base", "", "base branch mode: default or current")
	fs.StringVar(&base, "base-branch", "", "base branch mode: default or current")
	includeEmpty := fs.Bool("include-empty", false, "process issues even if body is empty")
	stopOnError := fs.Bool("stop-on-error", false, "stop after first failed agent run")
	failOnExisting := fs.Bool("fail-on-existing", false, "fail if issue branch or PR already exists")
	forceIssueFlow := fs.Bool("force-issue-flow", false, "disable auto-switch to PR-review mode")
	skipIfPRExists := fs.Bool("skip-if-pr-exists", true, "skip issue processing when a linked open PR exists")
	noSkipIfPRExists := fs.Bool("no-skip-if-pr-exists", false, "do not skip issue processing when a linked open PR exists")
	skipIfBranchExists := fs.Bool("skip-if-branch-exists", true, "skip issue processing when deterministic issue branch exists on origin")
	noSkipIfBranchExists := fs.Bool("no-skip-if-branch-exists", false, "do not skip issue processing when deterministic issue branch exists on origin")
	forceReprocess := fs.Bool("force-reprocess", false, "override skip guards")
	conflictRecoveryOnly := fs.Bool("conflict-recovery-only", false, "sync an existing reused branch with base and stop before any agent work")
	syncReusedBranch := fs.Bool("sync-reused-branch", true, "sync reused issue branches before running the agent")
	noSyncReusedBranch := fs.Bool("no-sync-reused-branch", false, "disable sync for reused issue branches before the agent step")
	syncStrategy := fs.String("sync-strategy", "", "reused branch sync strategy: rebase or merge")
	detach := fs.Bool("detach", false, "start the worker in the background and write logs/state to a predictable path")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")
	_ = fs.String("autonomous-session-file", "", "internal autonomous session checkpoint path")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected run issue argument: %s\n", fs.Arg(0))
		return 2
	}
	if *id > 0 && *issue > 0 && *id != *issue {
		_, _ = fmt.Fprintln(a.err, "run issue received conflicting --id and --issue values")
		return 2
	}
	if *id == 0 {
		*id = *issue
	}
	if *id <= 0 {
		_, _ = fmt.Fprintln(a.err, "run issue requires --id N")
		return 2
	}
	if code, handled := a.tryRunNativeIssue(ctx, nativeIssueOptions{
		issueID:              *id,
		common:               opts,
		base:                 base,
		includeEmpty:         *includeEmpty,
		failOnExisting:       *failOnExisting,
		forceIssueFlow:       *forceIssueFlow,
		skipIfPRExists:       *skipIfPRExists,
		noSkipIfPRExists:     *noSkipIfPRExists,
		skipIfBranchExists:   *skipIfBranchExists,
		noSkipIfBranchExists: *noSkipIfBranchExists,
		forceReprocess:       *forceReprocess,
		conflictRecoveryOnly: *conflictRecoveryOnly,
		syncReusedBranch:     *syncReusedBranch,
		noSyncReusedBranch:   *noSyncReusedBranch,
		syncStrategy:         *syncStrategy,
		detach:               *detach,
	}); handled {
		return code
	}

	pythonArgs := buildIssuePythonArgs(
		a.runtime.RunnerScript(),
		opts,
		*id,
		base,
		*includeEmpty,
		*stopOnError,
		*failOnExisting,
		*forceIssueFlow,
		*skipIfPRExists,
		*noSkipIfPRExists,
		*skipIfBranchExists,
		*noSkipIfBranchExists,
		*forceReprocess,
		*conflictRecoveryOnly,
		*syncReusedBranch,
		*noSyncReusedBranch,
		*syncStrategy,
		flagStateAdapter{fs: fs},
	)
	if *detach {
		workerPaths, err := resolveDetachedWorkerPaths(*workerDir, *opts.dir, "issue", strconv.Itoa(*id))
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
			return 1
		}
		return a.startDetachedWorker(detachedWorkerStateFromOptions(
			workerName("issue", strconv.Itoa(*id)),
			"run issue",
			"issue",
			strconv.Itoa(*id),
			opts,
			append([]string{"python3"}, pythonArgs...),
			workerPaths,
		))
	}
	return a.runPython(ctx, pythonArgs)
}

func withCommonOptionsDir(opts commonOptions, dir string) commonOptions {
	updated := opts
	updated.dir = stringPtr(dir)
	return updated
}

func stringPtr(value string) *string {
	return &value
}

func (a *App) prepareDetachedWorkerClone(configuredDir string, workerPaths detachedWorkerPaths) (string, error) {
	if a.clone == nil {
		return "", fmt.Errorf("detached worker clone preparer is not configured")
	}
	sourceDir := "."
	if strings.TrimSpace(configuredDir) != "" {
		sourceDir = configuredDir
	}
	clonePath := filepath.Join(filepath.Dir(workerPaths.StatePath), "repo")
	return a.clone.Prepare(sourceDir, clonePath)
}

func (a *App) runPR(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedRunPRFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("run pr", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts, a.runtime)
	id := fs.Int("id", 0, a.runtime.PullRequestFlagDescription())
	pr := fs.Int("pr", 0, "compatibility alias for --id")
	_ = fs.Bool("from-review-comments", false, "compatibility no-op; PR review-comments mode is selected by the command")
	allowBranchSwitch := fs.Bool("allow-pr-branch-switch", false, "allow switching to the target PR branch")
	isolateWorktree := fs.Bool("isolate-worktree", false, "run in a temporary git worktree")
	postSummary := fs.Bool("post-pr-summary", false, "post a summary comment after success")
	followupPrefix := fs.String("pr-followup-branch-prefix", "", "optional follow-up branch prefix")
	conflictRecoveryOnly := fs.Bool("conflict-recovery-only", false, "sync the current PR branch with base and stop before any agent work")
	syncStrategy := fs.String("sync-strategy", "", "reused branch sync strategy: rebase or merge")
	detach := fs.Bool("detach", false, "start the worker in the background and write logs/state to a predictable path")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")
	_ = fs.String("autonomous-session-file", "", "internal autonomous session checkpoint path")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected run pr argument: %s\n", fs.Arg(0))
		return 2
	}
	if *id > 0 && *pr > 0 && *id != *pr {
		_, _ = fmt.Fprintln(a.err, "run pr received conflicting --id and --pr values")
		return 2
	}
	if *id == 0 {
		*id = *pr
	}
	if *id <= 0 {
		_, _ = fmt.Fprintln(a.err, "run pr requires --id N")
		return 2
	}
	if code, ok := a.tryRunNativePR(ctx, nativePROptions{
		prID:                 *id,
		common:               opts,
		allowBranchSwitch:    *allowBranchSwitch,
		isolateWorktree:      *isolateWorktree,
		postSummary:          *postSummary,
		followupPrefix:       *followupPrefix,
		conflictRecoveryOnly: *conflictRecoveryOnly,
		syncStrategy:         *syncStrategy,
		detach:               *detach,
	}); ok {
		return code
	}

	pythonArgs := buildPRPythonArgs(a.runtime.RunnerScript(), opts, *id, *allowBranchSwitch, *isolateWorktree, *postSummary, *followupPrefix, *conflictRecoveryOnly, *syncStrategy)
	if *detach {
		workerPaths, err := resolveDetachedWorkerPaths(*workerDir, *opts.dir, "pr", strconv.Itoa(*id))
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
			return 1
		}
		return a.startDetachedWorker(detachedWorkerStateFromOptions(
			workerName("pr", strconv.Itoa(*id)),
			"run pr",
			"pr",
			strconv.Itoa(*id),
			opts,
			append([]string{"python3"}, pythonArgs...),
			workerPaths,
		))
	}
	return a.runPython(ctx, pythonArgs)
}
