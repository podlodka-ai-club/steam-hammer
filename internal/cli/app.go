package cli

import (
	"context"
	"fmt"
	"io"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/agentexec"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/githublifecycle"
)

type App struct {
	out            io.Writer
	err            io.Writer
	runner         Runner
	shell          shellExecutor
	start          DetachedStarter
	clone          BatchClonePreparer
	daemon         daemonLifecycle
	issueLifecycle issueLifecycle
	prLifecycle    prReviewLifecycle
	agentRunner    issueAgentRunner
	runtime        runtimeProvider
}

func NewApp(out, err io.Writer) *App {
	adapter := githublifecycle.NewAdapter(nil)
	return &App{
		out:            out,
		err:            err,
		runner:         ExecRunner{Stdout: out, Stderr: err},
		shell:          execShellExecutor{},
		start:          ExecDetachedStarter{},
		clone:          ExecBatchClonePreparer{},
		daemon:         adapter,
		issueLifecycle: adapter,
		prLifecycle:    adapter,
		agentRunner:    nativeIssueAgentRunner{},
		runtime:        defaultRuntimeProvider(),
	}
}

func (a *App) SetRunner(r Runner) {
	a.runner = r
}

func (a *App) SetDetachedStarter(starter DetachedStarter) {
	a.start = starter
}

func (a *App) SetBatchClonePreparer(preparer BatchClonePreparer) {
	a.clone = preparer
}

func (a *App) SetDaemonLifecycle(lifecycle daemonLifecycle) {
	a.daemon = lifecycle
}

func (a *App) SetIssueLifecycle(lifecycle issueLifecycle) {
	a.issueLifecycle = lifecycle
}

func (a *App) SetPRLifecycle(lifecycle prReviewLifecycle) {
	a.prLifecycle = lifecycle
}

func (a *App) SetIssueAgentRunner(runner issueAgentRunner) {
	if runner == nil {
		a.agentRunner = nativeIssueAgentRunner{}
		return
	}
	a.agentRunner = runner
}

type issueAgentRunner interface {
	Run(ctx context.Context, itemLabel string, req agentexec.Request) (*agentexec.Result, error)
}

type nativeIssueAgentRunner struct{}

func (nativeIssueAgentRunner) Run(ctx context.Context, itemLabel string, req agentexec.Request) (*agentexec.Result, error) {
	return agentexec.Run(ctx, itemLabel, req)
}

func (a *App) SetRuntimeProvider(provider runtimeProvider) {
	if provider == nil {
		a.runtime = defaultRuntimeProvider()
		return
	}
	a.runtime = provider
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
