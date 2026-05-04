package cli

import (
	"errors"
	"flag"
	"fmt"
	"math"
	"strconv"
)

type commonOptions struct {
	mode        *string
	lightweight *bool
	repo        *string
	tracker     *string
	codehost    *string
	dir         *string
	runner      *string
	agent       *string
	model       *string
	preset      *string
	trackTokens *bool
	tokenBudget *int
	costBudget  *float64
	autoYes     *bool
	branch      *string
	dryRun      *bool
	local       *string
	project     *string
	maxTry      *int
	timeout     *int
	idleTime    *int
	groomMode   *string
	requirePlan *bool
	askQuestions *bool
	autoContinueAfterPlan *bool
	maxQuestions *int
	maxRounds *int
}

func cloneCommonOptions(opts commonOptions) commonOptions {
	return commonOptions{
		mode:        stringPtrValue(opts.mode),
		lightweight: boolPtrValue(opts.lightweight),
		repo:        stringPtrValue(opts.repo),
		tracker:     stringPtrValue(opts.tracker),
		codehost:    stringPtrValue(opts.codehost),
		dir:         stringPtrValue(opts.dir),
		runner:      stringPtrValue(opts.runner),
		agent:       stringPtrValue(opts.agent),
		model:       stringPtrValue(opts.model),
		preset:      stringPtrValue(opts.preset),
		trackTokens: boolPtrValue(opts.trackTokens),
		tokenBudget: intPtrValue(opts.tokenBudget),
		costBudget:  floatPtrValue(opts.costBudget),
		autoYes:     boolPtrValue(opts.autoYes),
		branch:      stringPtrValue(opts.branch),
		dryRun:      boolPtrValue(opts.dryRun),
		local:       stringPtrValue(opts.local),
		project:     stringPtrValue(opts.project),
		maxTry:      intPtrValue(opts.maxTry),
		timeout:     intPtrValue(opts.timeout),
		idleTime:    intPtrValue(opts.idleTime),
		groomMode:   stringPtrValue(opts.groomMode),
		requirePlan: boolPtrValue(opts.requirePlan),
		askQuestions: boolPtrValue(opts.askQuestions),
		autoContinueAfterPlan: boolPtrValue(opts.autoContinueAfterPlan),
		maxQuestions: intPtrValue(opts.maxQuestions),
		maxRounds: intPtrValue(opts.maxRounds),
	}
}

func stringPtrValue(value *string) *string {
	if value == nil {
		return stringPtr("")
	}
	return stringPtr(*value)
}

func boolPtrValue(value *bool) *bool {
	copy := false
	if value != nil {
		copy = *value
	}
	return &copy
}

func intPtrValue(value *int) *int {
	copy := 0
	if value != nil {
		copy = *value
	}
	return &copy
}

func floatPtrValue(value *float64) *float64 {
	copy := math.NaN()
	if value != nil {
		copy = *value
	}
	return &copy
}

func addCommonFlags(fs *flag.FlagSet, opts *commonOptions, runtime runtimeProvider) {
	opts.mode = fs.String("mode", "", "execution mode: full or lightweight")
	opts.lightweight = fs.Bool("lightweight", false, "shortcut for --mode lightweight")
	opts.repo = fs.String("repo", "", runtime.RepoFlagDescription())
	opts.tracker = fs.String("tracker", "", "tracker provider: github or jira")
	opts.codehost = fs.String("codehost", "", "code host provider override passed through to the current runtime")
	opts.dir = fs.String("dir", "", "local git repository path")
	opts.runner = fs.String("runner", "", "AI runner: claude or opencode")
	opts.agent = fs.String("agent", "", "OpenCode agent name")
	opts.model = fs.String("model", "", "optional model override")
	opts.preset = fs.String("preset", "", "named project config preset")
	opts.trackTokens = fs.Bool("track-tokens", false, "track token usage from runner output")
	opts.tokenBudget = fs.Int("token-budget", 0, "abort when tracked token usage exceeds this limit")
	costBudget := math.NaN()
	opts.costBudget = &costBudget
	opts.autoYes = fs.Bool("opencode-auto-approve", false, "allow OpenCode to skip interactive approvals")
	opts.branch = fs.String("branch-prefix", "", "prefix for per-issue git branches")
	opts.dryRun = fs.Bool("dry-run", false, "print actions without running the agent")
	opts.local = fs.String("local-config", "", "path to local JSON config")
	opts.project = fs.String("project-config", "", "path to project JSON config")
	opts.maxTry = fs.Int("max-attempts", 0, "retry policy placeholder maximum")
	opts.timeout = fs.Int("agent-timeout-seconds", 0, "hard timeout for agent execution in seconds")
	opts.idleTime = fs.Int("agent-idle-timeout-seconds", 0, "abort if agent produces no output for this many seconds")
	opts.groomMode = fs.String("grooming-mode", "", "grooming mode override: off, auto, always")
	opts.requirePlan = fs.Bool("grooming-require-plan-approval", false, "require explicit plan approval before implementation")
	opts.askQuestions = fs.Bool("grooming-ask-questions", false, "allow grooming follow-up questions")
	opts.autoContinueAfterPlan = fs.Bool("grooming-auto-continue-after-plan", false, "continue automatically after posting a plan")
	opts.maxQuestions = fs.Int("grooming-max-questions", 0, "maximum follow-up questions allowed during grooming")
	opts.maxRounds = fs.Int("grooming-max-rounds", 0, "maximum grooming rounds allowed")
}

func appendCommonPythonArgs(args []string, opts commonOptions) []string {
	if *opts.lightweight {
		args = append(args, "--lightweight")
	} else if *opts.mode != "" {
		args = append(args, "--mode", *opts.mode)
	}
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
	if *opts.trackTokens {
		args = append(args, "--track-tokens")
	}
	if *opts.tokenBudget > 0 {
		args = append(args, "--token-budget", strconv.Itoa(*opts.tokenBudget))
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
	if *opts.groomMode != "" {
		args = append(args, "--grooming-mode", *opts.groomMode)
	}
	if *opts.requirePlan {
		args = append(args, "--grooming-require-plan-approval")
	}
	if *opts.askQuestions {
		args = append(args, "--grooming-ask-questions")
	}
	if *opts.autoContinueAfterPlan {
		args = append(args, "--grooming-auto-continue-after-plan")
	}
	if *opts.maxQuestions > 0 {
		args = append(args, "--grooming-max-questions", strconv.Itoa(*opts.maxQuestions))
	}
	if *opts.maxRounds > 0 {
		args = append(args, "--grooming-max-rounds", strconv.Itoa(*opts.maxRounds))
	}
	return args
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
	"ids":                  "use `orchestrator run batch --ids N[,M...]` instead",
}

var unsupportedRunBatchFlags = map[string]string{
	"pr":                   "use `orchestrator run pr --id N` instead",
	"from-review-comments": "use `orchestrator run pr --id N` instead",
	"doctor":               "use `orchestrator doctor` instead",
	"doctor-smoke-check":   "use `orchestrator doctor --doctor-smoke-check` instead",
	"limit":                "use `orchestrator run daemon` for tracker-selected batches",
	"state":                "use `orchestrator run daemon` for tracker-selected batches",
	"autonomous":           "use `orchestrator run daemon` for autonomous batch polling",
}

var unsupportedRunPRFlags = map[string]string{
	"issue":              "use `orchestrator run issue --id N` instead",
	"ids":                "use `orchestrator run batch --ids N[,M...]` instead",
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
