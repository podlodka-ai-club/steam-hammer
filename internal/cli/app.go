package cli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"
)

const runnerScript = "scripts/run_github_issues_to_opencode.py"

const defaultProjectConfigName = "project-config.json"
const defaultLocalConfigName = "local-config.json"

const projectConfigScaffold = `{
  "defaults": {
    "preset": "default",
    "runner": "opencode",
    "agent": "build",
    "model": "openai/gpt-4o",
    "track_tokens": false,
    "token_budget": 20000,
    "agent_timeout_seconds": 1200,
    "agent_idle_timeout_seconds": 180,
    "max_attempts": 2
  },
  "workflow": {
    "commands": {
      "setup": "python -m pip install -r requirements.txt",
      "test": "python -m unittest",
      "lint": null,
      "build": null,
      "e2e": null
    },
    "hooks": {
      "pre_agent": null,
      "post_agent": null,
      "pre_pr_update": null,
      "post_pr_update": null
    },
    "readiness": {
      "required_checks": [],
      "required_approvals": 1,
      "require_review": true,
      "require_mergeable": true,
      "require_required_file_evidence": true
    },
    "merge": {
      "auto": false,
      "method": "squash"
    }
  },
  "retry": {
    "max_attempts": 2,
    "escalate_to_preset": "hard"
  },
  "presets": {
    "cheap": {
      "runner": "opencode",
      "agent": "build",
      "model": "openai/gpt-4o-mini",
      "token_budget": 8000,
      "max_attempts": 1,
      "escalate_to_preset": "default"
    },
    "default": {
      "runner": "opencode",
      "agent": "build",
      "model": "openai/gpt-4o",
      "token_budget": 20000,
      "max_attempts": 2,
      "escalate_to_preset": "hard"
    },
    "hard": {
      "runner": "claude",
      "agent": "build",
      "model": "claude-sonnet-4-5",
      "token_budget": 40000,
      "max_attempts": 3,
      "escalate_to_preset": null
    }
  },
  "communication": {
    "verbosity": "normal"
  }
}
`

const localConfigScaffold = `{
  "preset": "default",
  "runner": "opencode",
  "agent": "build",
  "model": "openai/gpt-4o",
  "agent_timeout_seconds": 1200,
  "agent_idle_timeout_seconds": 180,
  "token_budget": 20000,
  "max_attempts": 2,
  "opencode_auto_approve": true,
  "fail_on_existing": false,
  "force_issue_flow": false,
  "skip_if_pr_exists": true,
  "skip_if_branch_exists": true,
  "force_reprocess": false,
  "sync_reused_branch": true,
  "sync_strategy": "rebase",
  "base_branch": "default",
  "create_child_issues": false
}
`

type Runner interface {
	Run(ctx context.Context, name string, args ...string) error
}

type ExecRunner struct {
	Stdout io.Writer
	Stderr io.Writer
}

func (r ExecRunner) Run(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout = r.Stdout
	cmd.Stderr = r.Stderr
	cmd.Stdin = os.Stdin
	return cmd.Run()
}

type App struct {
	out    io.Writer
	err    io.Writer
	runner Runner
}

func NewApp(out, err io.Writer) *App {
	return &App{
		out:    out,
		err:    err,
		runner: ExecRunner{Stdout: out, Stderr: err},
	}
}

func (a *App) SetRunner(r Runner) {
	a.runner = r
}

func (a *App) Run(args []string) int {
	return a.RunContext(context.Background(), args)
}

func (a *App) RunContext(ctx context.Context, args []string) int {
	if len(args) == 0 {
		_, _ = fmt.Fprint(a.err, usage())
		return 2
	}

	switch args[0] {
	case "-h", "--help", "help":
		_, _ = fmt.Fprint(a.out, usage())
		return 0
	case "init":
		return a.runInit(args[1:])
	case "doctor":
		return a.runDoctor(ctx, args[1:])
	case "autodoctor":
		return a.runAutoDoctor(ctx, args[1:])
	case "run":
		return a.runRun(ctx, args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown command %q\n\n%s", args[0], usage())
		return 2
	}
}

func (a *App) runInit(args []string) int {
	fs := newFlagSet("init", a.err)
	dir := fs.String("dir", ".", "directory to write config scaffolds into")
	project := fs.String("project-config", defaultProjectConfigName, "path to the repository project config scaffold")
	local := fs.String("local-config", defaultLocalConfigName, "path to the user-local config scaffold")
	force := fs.Bool("force", false, "overwrite existing scaffold files")
	skipProject := fs.Bool("skip-project-config", false, "do not create the project config scaffold")
	skipLocal := fs.Bool("skip-local-config", false, "do not create the local config scaffold")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected init argument: %s\n", fs.Arg(0))
		return 2
	}
	if *skipProject && *skipLocal {
		_, _ = fmt.Fprintln(a.err, "init has nothing to do: both scaffold outputs were skipped")
		return 2
	}

	targetDir := *dir
	if targetDir == "" {
		targetDir = "."
	}
	if err := os.MkdirAll(targetDir, 0o755); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create %s: %v\n", targetDir, err)
		return 1
	}

	var writes []scaffoldTarget
	if !*skipProject {
		writes = append(writes, scaffoldTarget{path: resolveScaffoldPath(targetDir, *project, defaultProjectConfigName), contents: projectConfigScaffold})
	}
	if !*skipLocal {
		writes = append(writes, scaffoldTarget{path: resolveScaffoldPath(targetDir, *local, defaultLocalConfigName), contents: localConfigScaffold})
	}

	for _, target := range writes {
		if err := writeScaffold(target.path, target.contents, *force); err != nil {
			_, _ = fmt.Fprintln(a.err, err.Error())
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "created %s\n", target.path)
	}

	return 0
}

func (a *App) runDoctor(ctx context.Context, args []string) int {
	if unsupported := firstUnsupportedFlag(args, unsupportedDoctorFlags); unsupported != "" {
		_, _ = fmt.Fprintln(a.err, unsupported)
		return 2
	}

	fs := newFlagSet("doctor", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	_ = fs.Bool("doctor", false, "compatibility no-op; doctor mode is selected by the command")
	doctorSmokeCheck := fs.Bool("doctor-smoke-check", false, "run a lightweight runner CLI smoke check")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected doctor argument: %s\n", fs.Arg(0))
		return 2
	}

	pythonArgs := []string{runnerScript, "--doctor"}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if *doctorSmokeCheck {
		pythonArgs = append(pythonArgs, "--doctor-smoke-check")
	}
	return a.runPython(ctx, pythonArgs)
}

func (a *App) runAutoDoctor(ctx context.Context, args []string) int {
	return a.runDoctor(ctx, args)
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
	case "daemon":
		return a.runDaemon(ctx, args[1:])
	case "pr":
		return a.runPR(ctx, args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown run target %q\n\n%s", args[0], runUsage())
		return 2
	}
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
	syncReusedBranch := fs.Bool("sync-reused-branch", true, "sync reused issue branches before running the agent")
	noSyncReusedBranch := fs.Bool("no-sync-reused-branch", false, "disable sync for reused issue branches before the agent step")
	syncStrategy := fs.String("sync-strategy", "", "reused branch sync strategy: rebase or merge")

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

	pythonArgs := []string{runnerScript, "--issue", strconv.Itoa(*id)}
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
	return a.runPython(ctx, pythonArgs)
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
	return a.runPython(ctx, pythonArgs)
}


func (a *App) runPython(ctx context.Context, args []string) int {
	if err := a.runner.Run(ctx, "python3", args...); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			_, _ = fmt.Fprintln(a.err, "orchestrator: python runner timed out")
			return 124
		}
		if errors.Is(ctx.Err(), context.Canceled) {
			_, _ = fmt.Fprintln(a.err, "orchestrator: python runner canceled")
			return 130
		}

		var exitErr interface{ ExitCode() int }
		if errors.As(err, &exitErr) {
			code := exitErr.ExitCode()
			if code >= 0 {
				_, _ = fmt.Fprintf(a.err, "orchestrator: python runner exited with code %d\n", code)
				return code
			}
		}

		_, _ = fmt.Fprintf(a.err, "orchestrator: python runner failed: %v\n", err)
		return 1
	}
	return 0
}

type commonOptions struct {
	repo     *string
	tracker  *string
	codehost *string
	dir      *string
	runner   *string
	agent    *string
	model    *string
	preset   *string
	autoYes  *bool
	branch   *string
	dryRun   *bool
	local    *string
	project  *string
	maxTry   *int
	timeout  *int
	idleTime *int
}

func addCommonFlags(fs *flag.FlagSet, opts *commonOptions) {
	opts.repo = fs.String("repo", "", "GitHub repo in owner/name format")
	opts.tracker = fs.String("tracker", "", "tracker provider: github or jira")
	opts.codehost = fs.String("codehost", "", "code host provider: github, bitbucket, or custom-proxy")
	opts.dir = fs.String("dir", "", "local git repository path")
	opts.runner = fs.String("runner", "", "AI runner: claude or opencode")
	opts.agent = fs.String("agent", "", "OpenCode agent name")
	opts.model = fs.String("model", "", "optional model override")
	opts.preset = fs.String("preset", "", "named project config preset")
	opts.autoYes = fs.Bool("opencode-auto-approve", false, "allow OpenCode to skip interactive approvals")
	opts.branch = fs.String("branch-prefix", "", "prefix for per-issue git branches")
	opts.dryRun = fs.Bool("dry-run", false, "print actions without running the agent")
	opts.local = fs.String("local-config", "", "path to local JSON config")
	opts.project = fs.String("project-config", "", "path to project JSON config")
	opts.maxTry = fs.Int("max-attempts", 0, "retry policy placeholder maximum")
	opts.timeout = fs.Int("agent-timeout-seconds", 0, "hard timeout for agent execution in seconds")
	opts.idleTime = fs.Int("agent-idle-timeout-seconds", 0, "abort if agent produces no output for this many seconds")
}

func appendCommonPythonArgs(args []string, opts commonOptions) []string {
	if *opts.repo != "" {
		args = append(args, "--repo", *opts.repo)
	}
	if *opts.tracker != "" {
		args = append(args, "--tracker", *opts.tracker)
	}
	if *opts.codehost != "" {
		args = append(args, "--codehost", *opts.codehost)
	}
	if *opts.dir != "" {
		args = append(args, "--dir", *opts.dir)
	}
	if *opts.runner != "" {
		args = append(args, "--runner", *opts.runner)
	}
	if *opts.agent != "" {
		args = append(args, "--agent", *opts.agent)
	}
	if *opts.model != "" {
		args = append(args, "--model", *opts.model)
	}
	if *opts.preset != "" {
		args = append(args, "--preset", *opts.preset)
	}
	if *opts.autoYes {
		args = append(args, "--opencode-auto-approve")
	}
	if *opts.branch != "" {
		args = append(args, "--branch-prefix", *opts.branch)
	}
	if *opts.dryRun {
		args = append(args, "--dry-run")
	}
	if *opts.local != "" {
		args = append(args, "--local-config", *opts.local)
	}
	if *opts.project != "" {
		args = append(args, "--project-config", *opts.project)
	}
	if *opts.maxTry > 0 {
		args = append(args, "--max-attempts", strconv.Itoa(*opts.maxTry))
	}
	if *opts.timeout > 0 {
		args = append(args, "--agent-timeout-seconds", strconv.Itoa(*opts.timeout))
	}
	if *opts.idleTime > 0 {
		args = append(args, "--agent-idle-timeout-seconds", strconv.Itoa(*opts.idleTime))
	}
	return args
}

func newFlagSet(name string, err io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(err)
	return fs
}

func flagExitCode(err error) int {
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	return 2
}

func flagWasPassed(fs *flag.FlagSet, name string) bool {
	passed := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == name {
			passed = true
		}
	})
	return passed
}

type scaffoldTarget struct {
	path     string
	contents string
}

func resolveScaffoldPath(dir, configuredPath, defaultName string) string {
	if configuredPath == "" {
		return filepath.Join(dir, defaultName)
	}
	if filepath.IsAbs(configuredPath) {
		return configuredPath
	}
	return filepath.Join(dir, configuredPath)
}

func writeScaffold(path, contents string, force bool) error {
	if !force {
		if _, err := os.Stat(path); err == nil {
			return fmt.Errorf("orchestrator: %s already exists (use --force to overwrite)", path)
		} else if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("orchestrator: failed to inspect %s: %w", path, err)
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("orchestrator: failed to create %s: %w", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		return fmt.Errorf("orchestrator: failed to write %s: %w", path, err)
	}
	return nil
}

func sleepContext(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			return nil
		}
	}

	timer := time.NewTimer(delay)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

var unsupportedDoctorFlags = map[string]string{
	"issue":                "use `orchestrator run issue --id N` instead",
	"pr":                   "use `orchestrator run pr --id N` instead",
	"from-review-comments": "PR review-comments mode is selected by `orchestrator run pr`",
	"limit":                "batch issue selection is not exposed by the Go wrapper yet",
	"state":                "batch issue selection is not exposed by the Go wrapper yet",
}

var unsupportedRunIssueFlags = map[string]string{
	"pr":                   "use `orchestrator run pr --id N` instead",
	"from-review-comments": "use `orchestrator run pr --id N` instead",
	"doctor":               "use `orchestrator doctor` instead",
	"doctor-smoke-check":   "use `orchestrator doctor --doctor-smoke-check` instead",
	"limit":                "batch issue selection is not exposed by `orchestrator run issue`; use `--id N`",
	"state":                "batch issue selection is not exposed by `orchestrator run issue`; use `--id N`",
}

var unsupportedRunPRFlags = map[string]string{
	"issue":              "use `orchestrator run issue --id N` instead",
	"doctor":             "use `orchestrator doctor` instead",
	"doctor-smoke-check": "use `orchestrator doctor --doctor-smoke-check` instead",
	"limit":              "batch issue selection is not exposed by the Go wrapper yet",
	"state":              "batch issue selection is not exposed by the Go wrapper yet",
	"base":               "issue-flow base selection only applies to `orchestrator run issue`",
	"base-branch":        "issue-flow base selection only applies to `orchestrator run issue`",
}

var unsupportedRunDaemonFlags = map[string]string{
	"issue":                     "use `orchestrator run issue --id N` for a one-shot issue run",
	"pr":                        "use `orchestrator run pr --id N` for PR review mode",
	"from-review-comments":      "PR review-comments mode is selected by `orchestrator run pr`",
	"doctor":                    "use `orchestrator doctor` instead",
	"doctor-smoke-check":        "use `orchestrator doctor --doctor-smoke-check` instead",
	"id":                        "daemon mode polls issue batches; use `orchestrator run issue --id N` for a single issue",
	"allow-pr-branch-switch":    "PR-only flag; use `orchestrator run pr` instead",
	"isolate-worktree":          "PR-only flag; use `orchestrator run pr` instead",
	"post-pr-summary":           "PR-only flag; use `orchestrator run pr` instead",
	"pr-followup-branch-prefix": "PR-only flag; use `orchestrator run pr` instead",
}

func firstUnsupportedFlag(args []string, unsupported map[string]string) string {
	for _, arg := range args {
		if arg == "--" {
			return ""
		}
		name, ok := flagName(arg)
		if !ok {
			continue
		}
		if message, found := unsupported[name]; found {
			return fmt.Sprintf("unsupported flag --%s for this command: %s", name, message)
		}
	}
	return ""
}

func flagName(arg string) (string, bool) {
	if len(arg) < 3 || arg[0:2] != "--" {
		return "", false
	}
	name := arg[2:]
	for i, r := range name {
		if r == '=' {
			name = name[:i]
			break
		}
	}
	return name, name != ""
}

func usage() string {
	return `Usage:
	  orchestrator init [flags]
	  orchestrator doctor [flags]
	  orchestrator autodoctor [flags]
	  orchestrator run issue --id N [flags]
	  orchestrator run pr --id N [flags]
	  orchestrator run daemon [flags]

	Commands:
	  init       Create local/project config scaffolds.
	  doctor     Run environment diagnostics via the current Python runner.
	  autodoctor Run doctor diagnostics with the same current checks.
	  run issue  Run issue orchestration via the current Python runner.
	  run pr     Run PR review-comment orchestration via the current Python runner.
	  run daemon Poll for issue work via the current Python runner.

Use "orchestrator <command> --help" for command flags.
`
}

func runUsage() string {
	return `Usage:
	  orchestrator run issue --id N [flags]
	  orchestrator run pr --id N [flags]
	  orchestrator run daemon [flags]
`
}
