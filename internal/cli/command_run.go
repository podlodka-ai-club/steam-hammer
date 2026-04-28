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
	"time"
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

func buildIssuePythonArgs(opts commonOptions, id int, base string, includeEmpty, stopOnError, failOnExisting, forceIssueFlow, skipIfPRExists, noSkipIfPRExists, skipIfBranchExists, noSkipIfBranchExists, forceReprocess, conflictRecoveryOnly, syncReusedBranch, noSyncReusedBranch bool, syncStrategy string, fs flagState) []string {
	pythonArgs := []string{runnerScript, "--issue", strconv.Itoa(id)}
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
	if conflictRecoveryOnly {
		pythonArgs = append(pythonArgs, "--conflict-recovery-only")
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
	return pythonArgs
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

func (a *App) runDaemon(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedRunDaemonFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("run daemon", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	state := fs.String("state", "open", "issue state to poll: open, closed, or all")
	limit := fs.Int("limit", 10, "maximum number of issues to scan per poll")
	pollIntervalSeconds := fs.Int("poll-interval-seconds", 120, "delay between autonomous polls")
	maxParallelTasks := fs.Int("max-parallel-tasks", 1, "maximum parallel tasks; only 1 is supported currently")
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
	createFollowupIssue := fs.Bool("create-followup-issue", false, "create a GitHub follow-up issue automatically when post-batch verification fails")
	detach := fs.Bool("detach", false, "start the worker in the background and write logs/state to a predictable path")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")

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
	if *maxParallelTasks != 1 {
		_, _ = fmt.Fprintln(a.err, "run daemon currently supports only --max-parallel-tasks=1")
		return 2
	}
	if *state != "open" && *state != "closed" && *state != "all" {
		_, _ = fmt.Fprintln(a.err, "run daemon requires --state to be one of: open, closed, all")
		return 2
	}

	pythonArgs := []string{runnerScript, "--autonomous", "--state", *state, "--limit", strconv.Itoa(*limit)}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if base != "" {
		pythonArgs = append(pythonArgs, "--base", base)
	}
	if *includeEmpty {
		pythonArgs = append(pythonArgs, "--include-empty")
	}
	if *stopOnError {
		pythonArgs = append(pythonArgs, "--stop-on-error")
	}
	if *failOnExisting {
		pythonArgs = append(pythonArgs, "--fail-on-existing")
	}
	if *forceIssueFlow {
		pythonArgs = append(pythonArgs, "--force-issue-flow")
	}
	if *noSkipIfPRExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-pr-exists")
	} else if flagWasPassed(fs, "skip-if-pr-exists") && *skipIfPRExists {
		pythonArgs = append(pythonArgs, "--skip-if-pr-exists")
	} else if !*skipIfPRExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-pr-exists")
	}
	if *noSkipIfBranchExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-branch-exists")
	} else if flagWasPassed(fs, "skip-if-branch-exists") && *skipIfBranchExists {
		pythonArgs = append(pythonArgs, "--skip-if-branch-exists")
	} else if !*skipIfBranchExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-branch-exists")
	}
	if *forceReprocess {
		pythonArgs = append(pythonArgs, "--force-reprocess")
	}
	if *noSyncReusedBranch {
		pythonArgs = append(pythonArgs, "--no-sync-reused-branch")
	} else if flagWasPassed(fs, "sync-reused-branch") && *syncReusedBranch {
		pythonArgs = append(pythonArgs, "--sync-reused-branch")
	} else if !*syncReusedBranch {
		pythonArgs = append(pythonArgs, "--no-sync-reused-branch")
	}
	if *syncStrategy != "" {
		pythonArgs = append(pythonArgs, "--sync-strategy", *syncStrategy)
	}
	effectiveMaxCycles := *maxCycles
	if *opts.dryRun && effectiveMaxCycles == 0 {
		effectiveMaxCycles = 1
	}
	if *detach && effectiveMaxCycles == 0 {
		effectiveMaxCycles = 1
	}

	if *detach {
		workerPaths, err := resolveDetachedWorkerPaths(*workerDir, *opts.dir, "daemon", "")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
			return 1
		}
		pythonArgs = append(pythonArgs, "--autonomous-session-file", workerPaths.SessionPath)
		if *postBatchVerify {
			pythonArgs = append(pythonArgs, "--post-batch-verify")
		}
		if *createFollowupIssue {
			pythonArgs = append(pythonArgs, "--create-followup-issue")
		}
		return a.startDetachedWorker(detachedWorkerStateFromOptions(
			"daemon",
			"run daemon",
			"daemon",
			"",
			opts,
			append([]string{"python3"}, pythonArgs...),
			workerPaths,
		))
	}

	sessionFile, err := os.CreateTemp("", "orchestrator-daemon-session-*.json")
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create daemon session file: %v\n", err)
		return 1
	}
	sessionPath := sessionFile.Name()
	_ = sessionFile.Close()
	defer os.Remove(sessionPath)
	pythonArgs = append(pythonArgs, "--autonomous-session-file", sessionPath)
	if *postBatchVerify {
		pythonArgs = append(pythonArgs, "--post-batch-verify")
	}
	if *createFollowupIssue {
		pythonArgs = append(pythonArgs, "--create-followup-issue")
	}

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

func (a *App) runBatch(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedRunBatchFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("run batch", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
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
			clonePath, err = a.prepareDetachedBatchClone(*opts.dir, workerPaths)
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
		pythonArgs := buildIssuePythonArgs(
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
		if *detach {
			state := detachedWorkerStateFromOptions(
				workerName("issue", strconv.Itoa(id)),
				"run batch",
				"issue",
				strconv.Itoa(id),
				issueOpts,
				append([]string{"python3"}, pythonArgs...),
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
		code := a.runPython(ctx, pythonArgs)
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
	addCommonFlags(fs, &opts)
	id := fs.Int("id", 0, "GitHub issue number")
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

	pythonArgs := buildIssuePythonArgs(
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

func (a *App) prepareDetachedBatchClone(configuredDir string, workerPaths detachedWorkerPaths) (string, error) {
	if a.clone == nil {
		return "", fmt.Errorf("detached batch clone preparer is not configured")
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
	addCommonFlags(fs, &opts)
	id := fs.Int("id", 0, "GitHub pull request number")
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

	pythonArgs := []string{runnerScript, "--pr", strconv.Itoa(*id), "--from-review-comments"}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if *allowBranchSwitch {
		pythonArgs = append(pythonArgs, "--allow-pr-branch-switch")
	}
	if *isolateWorktree {
		pythonArgs = append(pythonArgs, "--isolate-worktree")
	}
	if *postSummary {
		pythonArgs = append(pythonArgs, "--post-pr-summary")
	}
	if *followupPrefix != "" {
		pythonArgs = append(pythonArgs, "--pr-followup-branch-prefix", *followupPrefix)
	}
	if *syncStrategy != "" {
		pythonArgs = append(pythonArgs, "--sync-strategy", *syncStrategy)
	}
	if *conflictRecoveryOnly {
		pythonArgs = append(pythonArgs, "--conflict-recovery-only")
	}
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
