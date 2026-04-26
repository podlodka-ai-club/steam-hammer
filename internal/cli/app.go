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
	if len(args) == 0 {
		_, _ = fmt.Fprint(a.err, usage())
		return 2
	}

	switch args[0] {
	case "-h", "--help", "help":
		_, _ = fmt.Fprint(a.out, usage())
		return 0
	case "doctor":
		return a.runDoctor(args[1:])
	case "run":
		return a.runRun(args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown command %q\n\n%s", args[0], usage())
		return 2
	}
}

func (a *App) runDoctor(args []string) int {
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
	return a.runPython(pythonArgs)
}

func (a *App) runRun(args []string) int {
	if len(args) == 0 {
		_, _ = fmt.Fprint(a.err, runUsage())
		return 2
	}

	switch args[0] {
	case "-h", "--help", "help":
		_, _ = fmt.Fprint(a.out, runUsage())
		return 0
	case "issue":
		return a.runIssue(args[1:])
	case "pr":
		return a.runPR(args[1:])
	default:
		_, _ = fmt.Fprintf(a.err, "unknown run target %q\n\n%s", args[0], runUsage())
		return 2
	}
}

func (a *App) runIssue(args []string) int {
	fs := newFlagSet("run issue", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	id := fs.Int("id", 0, "GitHub issue number")
	base := fs.String("base", "", "base branch mode: default or current")
	forceIssueFlow := fs.Bool("force-issue-flow", false, "disable auto-switch to PR-review mode")
	forceReprocess := fs.Bool("force-reprocess", false, "override skip guards")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected run issue argument: %s\n", fs.Arg(0))
		return 2
	}
	if *id <= 0 {
		_, _ = fmt.Fprintln(a.err, "run issue requires --id N")
		return 2
	}

	pythonArgs := []string{runnerScript, "--issue", strconv.Itoa(*id)}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if *base != "" {
		pythonArgs = append(pythonArgs, "--base", *base)
	}
	if *forceIssueFlow {
		pythonArgs = append(pythonArgs, "--force-issue-flow")
	}
	if *forceReprocess {
		pythonArgs = append(pythonArgs, "--force-reprocess")
	}
	return a.runPython(pythonArgs)
}

func (a *App) runPR(args []string) int {
	fs := newFlagSet("run pr", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	id := fs.Int("id", 0, "GitHub pull request number")
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
	return a.runPython(pythonArgs)
}

func (a *App) runPython(args []string) int {
	if err := a.runner.Run(context.Background(), "python3", args...); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
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
