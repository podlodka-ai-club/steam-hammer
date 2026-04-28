package cli

import (
	"context"
	"flag"
	"fmt"
	"io"
	"strconv"
	"strings"
)

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

	pythonArgs := []string{a.runtime.RunnerScript(), "--doctor"}
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
	addCommonFlags(fs, &opts, a.runtime)
	createFollowupIssue := fs.Bool("create-followup-issue", false, a.runtime.FollowUpIssueFlagDescription("verification"))

	if err := fs.Parse(args); err != nil {
		return flagExitCode(err)
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprintf(a.err, "unexpected verify argument: %s\n", fs.Arg(0))
		return 2
	}

	pythonArgs := []string{a.runtime.RunnerScript(), "--post-batch-verify"}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	if *createFollowupIssue {
		pythonArgs = append(pythonArgs, "--create-followup-issue")
	}
	return a.runPython(ctx, pythonArgs)
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

	pythonArgs := []string{a.runtime.RunnerScript(), "--status"}
	if *issue > 0 {
		pythonArgs = append(pythonArgs, "--issue", strconv.Itoa(*issue))
	} else if *pr > 0 {
		pythonArgs = append(pythonArgs, "--pr", strconv.Itoa(*pr))
	}
	pythonArgs = appendCommonPythonArgs(pythonArgs, opts)
	return a.runPython(ctx, pythonArgs)
}

func newFlagSet(name string, err io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(err)
	return fs
}
