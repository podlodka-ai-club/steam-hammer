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
	"strconv"
	"strings"
	"syscall"
	"time"
)

const runnerScript = "scripts/run_github_issues_to_opencode.py"

const defaultProjectConfigName = "project-config.json"
const defaultLocalConfigName = "local-config.json"

const projectConfigScaffold = `{
  "defaults": {
    "preset": "default",
    "tracker": "github",
    "codehost": "github",
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
  "scope": {
    "defaults": {
      "labels": {
        "allow": ["autonomous", "bug"],
        "deny": ["manual-only"]
      },
      "assignees": {
        "deny": ["human-only"]
      },
      "priority": {
        "allow": ["priority:high", "priority:medium"],
        "order": ["priority:high", "priority:medium", "priority:low"]
      },
      "freshness": {
        "max_age_days": 30,
        "max_idle_days": 14
      }
    }
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
  "tracker": "github",
  "codehost": "github",
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

type DetachedStarter interface {
	Start(req DetachedRequest) (DetachedProcess, error)
}

type DetachedRequest struct {
	Name    string
	Args    []string
	Dir     string
	LogPath string
}

type DetachedProcess struct {
	PID int
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

type ExecDetachedStarter struct{}

func (ExecDetachedStarter) Start(req DetachedRequest) (DetachedProcess, error) {
	if err := os.MkdirAll(filepath.Dir(req.LogPath), 0o755); err != nil {
		return DetachedProcess{}, fmt.Errorf("failed to create log directory: %w", err)
	}
	logFile, err := os.OpenFile(req.LogPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return DetachedProcess{}, fmt.Errorf("failed to open log file: %w", err)
	}

	cmd := exec.Command(req.Name, req.Args...)
	cmd.Dir = req.Dir
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.Stdin = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		_ = logFile.Close()
		return DetachedProcess{}, err
	}
	_ = logFile.Close()
	return DetachedProcess{PID: cmd.Process.Pid}, nil
}

type App struct {
	out    io.Writer
	err    io.Writer
	runner Runner
	start  DetachedStarter
}

func NewApp(out, err io.Writer) *App {
	return &App{
		out:    out,
		err:    err,
		runner: ExecRunner{Stdout: out, Stderr: err},
		start:  ExecDetachedStarter{},
	}
}

func (a *App) SetRunner(r Runner) {
	a.runner = r
}

func (a *App) SetDetachedStarter(starter DetachedStarter) {
	a.start = starter
}

type detachedWorkerState struct {
	Name        string   `json:"name"`
	Mode        string   `json:"mode"`
	TargetKind  string   `json:"target_kind"`
	TargetID    string   `json:"target_id,omitempty"`
	Repo        string   `json:"repo,omitempty"`
	Command     []string `json:"command"`
	StartedAt   string   `json:"started_at"`
	PID         int      `json:"pid"`
	LogPath     string   `json:"log_path"`
	SessionPath string   `json:"session_path,omitempty"`
	StatePath   string   `json:"state_path"`
	WorkDir     string   `json:"work_dir"`
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
	case "verify":
		return a.runVerify(ctx, args[1:])
	case "status":
		return a.runStatus(ctx, args[1:])
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

func (a *App) runVerify(ctx context.Context, args []string) int {
	fs := newFlagSet("verify", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	createFollowupIssue := fs.Bool("create-followup-issue", false, "create a GitHub follow-up issue automatically when verification fails")

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected verify argument: %s\n", fs.Arg(0))
		return 2
	}

	pythonArgs := []string{runnerScript, "--post-batch-verify"}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if *createFollowupIssue {
		pythonArgs = append(pythonArgs, "--create-followup-issue")
	}
	return a.runPython(ctx, pythonArgs)
}

func (a *App) runStatus(ctx context.Context, args []string) int {
	fs := newFlagSet("status", a.err)
	opts := commonOptions{}
	addCommonFlags(fs, &opts)
	issue := fs.Int("issue", 0, "GitHub issue number")
	pr := fs.Int("pr", 0, "GitHub pull request number")
	worker := fs.String("worker", "", "detached worker name: issue-N, pr-N, or daemon")
	workerDir := fs.String("worker-dir", "", "directory that stores detached worker state")
	autonomousSessionFile := fs.String("autonomous-session-file", "", "read daemon batch status from a session checkpoint file")

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
	if targets != 1 {
		_, _ = fmt.Fprintln(a.err, "status requires exactly one of --issue N, --pr N, --worker NAME, or --autonomous-session-file PATH")
		return 2
	}
	if strings.TrimSpace(*worker) != "" {
		return a.runDetachedStatus(*workerDir, *worker)
	}

	pythonArgs := []string{runnerScript, "--status"}
	if *issue > 0 {
		pythonArgs = append(pythonArgs, "--issue", strconv.Itoa(*issue))
	} else if *pr > 0 {
		pythonArgs = append(pythonArgs, "--pr", strconv.Itoa(*pr))
	} else {
		pythonArgs = append(pythonArgs, "--autonomous-session-file", *autonomousSessionFile)
	}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
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
		pythonArgs = append(pythonArgs, "--autonomous-session-file", workerPaths.sessionPath)
		if *postBatchVerify {
			pythonArgs = append(pythonArgs, "--post-batch-verify")
		}
		if *createFollowupIssue {
			pythonArgs = append(pythonArgs, "--create-followup-issue")
		}
		return a.startDetachedWorker(detachedWorkerState{
			Name:        "daemon",
			Mode:        "run daemon",
			TargetKind:  "daemon",
			Repo:        strings.TrimSpace(*opts.repo),
			Command:     append([]string{"python3"}, pythonArgs...),
			LogPath:     workerPaths.logPath,
			SessionPath: workerPaths.sessionPath,
			StatePath:   workerPaths.statePath,
			WorkDir:     workerPaths.workDir,
		})
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
	if *conflictRecoveryOnly {
		pythonArgs = append(pythonArgs, "--conflict-recovery-only")
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
	if *detach {
		workerPaths, err := resolveDetachedWorkerPaths(*workerDir, *opts.dir, "issue", strconv.Itoa(*id))
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
			return 1
		}
		return a.startDetachedWorker(detachedWorkerState{
			Name:       workerName("issue", strconv.Itoa(*id)),
			Mode:       "run issue",
			TargetKind: "issue",
			TargetID:   strconv.Itoa(*id),
			Repo:       strings.TrimSpace(*opts.repo),
			Command:    append([]string{"python3"}, pythonArgs...),
			LogPath:    workerPaths.logPath,
			StatePath:  workerPaths.statePath,
			WorkDir:    workerPaths.workDir,
		})
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
		return a.startDetachedWorker(detachedWorkerState{
			Name:       workerName("pr", strconv.Itoa(*id)),
			Mode:       "run pr",
			TargetKind: "pr",
			TargetID:   strconv.Itoa(*id),
			Repo:       strings.TrimSpace(*opts.repo),
			Command:    append([]string{"python3"}, pythonArgs...),
			LogPath:    workerPaths.logPath,
			StatePath:  workerPaths.statePath,
			WorkDir:    workerPaths.workDir,
		})
	}
	return a.runPython(ctx, pythonArgs)
}

type detachedWorkerPaths struct {
	statePath   string
	logPath     string
	sessionPath string
	workDir     string
}

func resolveDetachedWorkerPaths(configuredRoot, configuredWorkDir, targetKind, targetID string) (detachedWorkerPaths, error) {
	workDir := "."
	if strings.TrimSpace(configuredWorkDir) != "" {
		workDir = configuredWorkDir
	}
	absWorkDir, err := filepath.Abs(workDir)
	if err != nil {
		return detachedWorkerPaths{}, err
	}

	root := strings.TrimSpace(configuredRoot)
	if root == "" {
		root = filepath.Join(absWorkDir, ".orchestrator", "workers")
	} else if !filepath.IsAbs(root) {
		root = filepath.Join(absWorkDir, root)
	}

	name := workerName(targetKind, targetID)
	workerBase := filepath.Join(root, name)
	paths := detachedWorkerPaths{
		statePath: filepath.Join(workerBase, "worker.json"),
		logPath:   filepath.Join(workerBase, "worker.log"),
		workDir:   absWorkDir,
	}
	if targetKind == "daemon" {
		paths.sessionPath = filepath.Join(workerBase, "session.json")
	}
	return paths, nil
}

func workerName(targetKind, targetID string) string {
	if targetID == "" {
		return targetKind
	}
	return targetKind + "-" + targetID
}

func (a *App) startDetachedWorker(state detachedWorkerState) int {
	if a.start == nil {
		_, _ = fmt.Fprintln(a.err, "orchestrator: detached worker starter is not configured")
		return 1
	}
	if err := ensureDetachedWorkerWritable(state.StatePath); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return 1
	}
	if err := os.MkdirAll(filepath.Dir(state.StatePath), 0o755); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create worker directory: %v\n", err)
		return 1
	}
	if state.SessionPath != "" {
		if err := os.MkdirAll(filepath.Dir(state.SessionPath), 0o755); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create session directory: %v\n", err)
			return 1
		}
	}
	process, err := a.start.Start(DetachedRequest{
		Name:    state.Command[0],
		Args:    state.Command[1:],
		Dir:     state.WorkDir,
		LogPath: state.LogPath,
	})
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to start detached worker: %v\n", err)
		return 1
	}
	state.PID = process.PID
	state.StartedAt = time.Now().UTC().Format(time.RFC3339)
	if err := writeDetachedWorkerState(state); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to write detached worker state: %v\n", err)
		return 1
	}
	_, _ = fmt.Fprintf(a.out, "started detached worker %s\n", state.Name)
	_, _ = fmt.Fprintf(a.out, "pid: %d\n", state.PID)
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	_, _ = fmt.Fprintf(a.out, "next: orchestrator status --worker %s\n", state.Name)
	return 0
}

func ensureDetachedWorkerWritable(statePath string) error {
	state, err := readDetachedWorkerState(statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("failed to inspect detached worker state: %w", err)
	}
	if state.PID > 0 {
		running, _ := processRunning(state.PID)
		if running {
			return fmt.Errorf("detached worker %s is already running with pid %d (see %s)", state.Name, state.PID, state.LogPath)
		}
	}
	return nil
}

func writeDetachedWorkerState(state detachedWorkerState) error {
	payload, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	payload = append(payload, '\n')
	return os.WriteFile(state.StatePath, payload, 0o644)
}

func readDetachedWorkerState(path string) (detachedWorkerState, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return detachedWorkerState{}, err
	}
	var state detachedWorkerState
	if err := json.Unmarshal(data, &state); err != nil {
		return detachedWorkerState{}, err
	}
	return state, nil
}

func (a *App) runDetachedStatus(configuredRoot, name string) int {
	workerPaths, err := resolveDetachedWorkerPaths(configuredRoot, ".", normalizeWorkerLookupName(name), workerLookupID(name))
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
		return 1
	}
	state, err := readDetachedWorkerState(workerPaths.statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			_, _ = fmt.Fprintf(a.err, "orchestrator: detached worker state not found: %s\n", workerPaths.statePath)
			return 1
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to read detached worker state: %v\n", err)
		return 1
	}
	running, runErr := processRunning(state.PID)
	processStatus := "stopped"
	if running {
		processStatus = "running"
	} else if runErr != nil {
		processStatus = "unknown"
	}
	_, _ = fmt.Fprintf(a.out, "worker: %s\n", state.Name)
	if state.TargetKind == "daemon" {
		_, _ = fmt.Fprintln(a.out, "target: daemon")
	} else {
		_, _ = fmt.Fprintf(a.out, "target: %s #%s\n", state.TargetKind, state.TargetID)
	}
	if state.Repo != "" {
		_, _ = fmt.Fprintf(a.out, "repo: %s\n", state.Repo)
	}
	_, _ = fmt.Fprintf(a.out, "process: %s\n", processStatus)
	_, _ = fmt.Fprintf(a.out, "pid: %d\n", state.PID)
	_, _ = fmt.Fprintf(a.out, "started: %s\n", state.StartedAt)
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	_, _ = fmt.Fprintf(a.out, "next: %s\n", detachedWorkerNextAction(state, processStatus))
	return 0
}

func normalizeWorkerLookupName(name string) string {
	trimmed := strings.TrimSpace(name)
	if trimmed == "" {
		return ""
	}
	parts := strings.SplitN(trimmed, "-", 2)
	return parts[0]
}

func workerLookupID(name string) string {
	trimmed := strings.TrimSpace(name)
	parts := strings.SplitN(trimmed, "-", 2)
	if len(parts) != 2 {
		return ""
	}
	return parts[1]
}

func processRunning(pid int) (bool, error) {
	if pid <= 0 {
		return false, nil
	}
	err := syscall.Kill(pid, 0)
	if err == nil {
		return true, nil
	}
	if errors.Is(err, syscall.ESRCH) {
		return false, nil
	}
	return false, err
}

func detachedWorkerNextAction(state detachedWorkerState, processStatus string) string {
	if processStatus == "running" {
		if state.TargetKind == "daemon" && state.SessionPath != "" {
			return fmt.Sprintf("tail -f %s or run orchestrator status --autonomous-session-file %s", state.LogPath, state.SessionPath)
		}
		return fmt.Sprintf("tail -f %s or run %s", state.LogPath, detachedTargetStatusCommand(state))
	}
	if state.TargetKind == "daemon" && state.SessionPath != "" {
		return fmt.Sprintf("inspect %s and, if needed, run orchestrator status --autonomous-session-file %s", state.LogPath, state.SessionPath)
	}
	return fmt.Sprintf("inspect %s and, if needed, run %s", state.LogPath, detachedTargetStatusCommand(state))
}

func detachedTargetStatusCommand(state detachedWorkerState) string {
	if state.TargetKind == "issue" {
		if state.Repo != "" {
			return fmt.Sprintf("orchestrator status --issue %s --repo %s", state.TargetID, state.Repo)
		}
		return fmt.Sprintf("orchestrator status --issue %s", state.TargetID)
	}
	if state.TargetKind == "pr" {
		if state.Repo != "" {
			return fmt.Sprintf("orchestrator status --pr %s --repo %s", state.TargetID, state.Repo)
		}
		return fmt.Sprintf("orchestrator status --pr %s", state.TargetID)
	}
	return fmt.Sprintf("orchestrator status --worker %s", state.Name)
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
	  orchestrator verify [flags]
	  orchestrator status (--issue N | --pr N | --worker NAME | --autonomous-session-file PATH) [flags]
	  orchestrator run issue --id N [flags]
	  orchestrator run pr --id N [flags]
	  orchestrator run daemon [flags]

	Commands:
	  init       Create local/project config scaffolds.
	  doctor     Run environment diagnostics via the current Python runner.
	  autodoctor Run doctor diagnostics with the same current checks.
	  verify     Run post-batch repository verification checks.
	  status     Print a concise orchestration status summary.
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
