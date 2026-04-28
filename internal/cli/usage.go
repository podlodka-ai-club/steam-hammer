package cli

func usage() string {
	return `Usage:
	  orchestrator init [flags]
	  orchestrator doctor [flags]
	  orchestrator autodoctor [flags]
	  orchestrator verify [flags]
	  orchestrator status (--issue N | --pr N | --worker NAME | --autonomous-session-file PATH) [flags]
	  orchestrator run issue --id N [flags]
	  orchestrator run batch --ids N[,M...] [flags]
	  orchestrator run pr --id N [flags]
	  orchestrator run daemon [flags]

	Commands:
	  init       Create local/project config scaffolds.
	  doctor     Run environment diagnostics via the current Python runner.
	  autodoctor Run doctor diagnostics with the same current checks.
	  verify     Run post-batch repository verification checks.
	  status     Print a concise orchestration status summary.
	  run issue  Run issue orchestration via the current Python runner.
	  run batch  Launch explicit issue batches via the current Python runner.
	  run pr     Run PR review-comment orchestration via the current Python runner.
	  run daemon Poll for issue work via the current Python runner.

	Use "orchestrator <command> --help" for command flags.
`
}

func runUsage() string {
	return `Usage:
	  orchestrator run issue --id N [flags]
	  orchestrator run batch --ids N[,M...] [flags]
	  orchestrator run pr --id N [flags]
	  orchestrator run daemon [flags]
`
}
