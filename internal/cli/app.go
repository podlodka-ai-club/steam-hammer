package cli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
)

const runnerScript = "scripts/run_github_issues_to_opencode.py"

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
	case "doctor":
		return a.runDoctor(ctx, args[1:])
	case "run":
		return a.runRun(ctx, args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown command %q\n\n%s", args[0], usage())
		return 2
	}
}

func (a *App) runDoctor(ctx context.Context, args []string) int {
	fs := newFlagSet("doctor", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
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
	case "pr":
		return a.runPR(ctx, args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown run target %q\n\n%s", args[0], runUsage())
		return 2
	}
}

func (a *App) runIssue(ctx context.Context, args []string) int {
	fs := newFlagSet("run issue", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	id := fs.Int("id", 0, "GitHub issue number")
	issue := fs.Int("issue", 0, "GitHub issue number (alias for --id)")
	state := fs.String("state", "", "issue state for batch runs: open, closed, or all")
	limit := fs.Int("limit", 0, "maximum number of issues to process for batch runs")
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
	issueID := selectedID(*id, *issue)
	if issueID < 0 {
		_, _ = fmt.Fprintln(a.err, "use only one of --id or --issue")
		return 2
	}

	pythonArgs := []string{runnerScript}
	if issueID > 0 {
		pythonArgs = append(pythonArgs, "--issue", strconv.Itoa(issueID))
	}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if *state != "" {
		pythonArgs = append(pythonArgs, "--state", *state)
	}
	if *limit > 0 {
		pythonArgs = append(pythonArgs, "--limit", strconv.Itoa(*limit))
	}
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
	if flagPassed(fs, "skip-if-pr-exists") {
		pythonArgs = append(pythonArgs, "--skip-if-pr-exists")
	} else if !*skipIfPRExists || *noSkipIfPRExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-pr-exists")
	}
	if flagPassed(fs, "skip-if-branch-exists") {
		pythonArgs = append(pythonArgs, "--skip-if-branch-exists")
	} else if !*skipIfBranchExists || *noSkipIfBranchExists {
		pythonArgs = append(pythonArgs, "--no-skip-if-branch-exists")
	}
	if *forceReprocess {
		pythonArgs = append(pythonArgs, "--force-reprocess")
	}
	if flagPassed(fs, "sync-reused-branch") {
		pythonArgs = append(pythonArgs, "--sync-reused-branch")
	} else if !*syncReusedBranch || *noSyncReusedBranch {
		pythonArgs = append(pythonArgs, "--no-sync-reused-branch")
	}
	if *syncStrategy != "" {
		pythonArgs = append(pythonArgs, "--sync-strategy", *syncStrategy)
	}
	return a.runPython(ctx, pythonArgs)
}

func (a *App) runPR(ctx context.Context, args []string) int {
	fs := newFlagSet("run pr", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	id := fs.Int("id", 0, "GitHub pull request number")
	pr := fs.Int("pr", 0, "GitHub pull request number (alias for --id)")
	fs.Bool("from-review-comments", false, "accepted for compatibility; run pr always uses review comments")
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
	prID := selectedID(*id, *pr)
	if prID < 0 {
		_, _ = fmt.Fprintln(a.err, "use only one of --id or --pr")
		return 2
	}
	if prID <= 0 {
		_, _ = fmt.Fprintln(a.err, "run pr requires --id N")
		return 2
	}
	pythonArgs := []string{runnerScript, "--pr", strconv.Itoa(prID), "--from-review-comments"}
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
	dir      *string
	runner   *string
	agent    *string
	model    *string
	autoYes  *bool
	branch   *string
	dryRun   *bool
	local    *string
	project  *string
	timeout  *int
	idleTime *int
}

func addCommonFlags(fs *flag.FlagSet, opts *commonOptions) {
	opts.repo = fs.String("repo", "", "GitHub repo in owner/name format")
	opts.dir = fs.String("dir", "", "local git repository path")
	opts.runner = fs.String("runner", "", "AI runner: claude or opencode")
	opts.agent = fs.String("agent", "", "OpenCode agent name")
	opts.model = fs.String("model", "", "optional model override")
	opts.autoYes = fs.Bool("opencode-auto-approve", false, "allow OpenCode to skip interactive approvals")
	opts.branch = fs.String("branch-prefix", "", "prefix for per-issue git branches")
	opts.dryRun = fs.Bool("dry-run", false, "print actions without running the agent")
	opts.local = fs.String("local-config", "", "path to local JSON config")
	opts.project = fs.String("project-config", "", "path to project JSON config")
	opts.timeout = fs.Int("agent-timeout-seconds", 0, "hard timeout for agent execution in seconds")
	opts.idleTime = fs.Int("agent-idle-timeout-seconds", 0, "abort if agent produces no output for this many seconds")
}

func appendCommonPythonArgs(args []string, opts commonOptions) []string {
	if *opts.repo != "" {
		args = append(args, "--repo", *opts.repo)
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
	if *opts.timeout > 0 {
		args = append(args, "--agent-timeout-seconds", strconv.Itoa(*opts.timeout))
	}
	if *opts.idleTime > 0 {
		args = append(args, "--agent-idle-timeout-seconds", strconv.Itoa(*opts.idleTime))
	}
	return args
}

func selectedID(primary, alias int) int {
	if primary > 0 && alias > 0 {
		return -1
	}
	if primary > 0 {
		return primary
	}
	return alias
}

func flagPassed(fs *flag.FlagSet, name string) bool {
	found := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == name {
			found = true
		}
	})
	return found
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

func usage() string {
	return `Usage:
  orchestrator doctor [flags]
  orchestrator run issue --id N [flags]
  orchestrator run pr --id N [flags]

Commands:
  doctor     Run environment diagnostics via the current Python runner.
  run issue  Run issue orchestration via the current Python runner.
  run pr     Run PR review-comment orchestration via the current Python runner.

Use "orchestrator <command> --help" for command flags.
`
}

func runUsage() string {
	return `Usage:
  orchestrator run issue --id N [flags]
  orchestrator run pr --id N [flags]
`
}
