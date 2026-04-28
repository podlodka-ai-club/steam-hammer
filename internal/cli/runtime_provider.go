package cli

const githubRunnerScript = "scripts/run_github_issues_to_opencode.py"
const runnerScript = githubRunnerScript

// runtimeProvider isolates assumptions that still belong to the current
// GitHub-backed Python runtime so the orchestration CLI can stop hard-coding
// provider-specific wording in command handlers.
type runtimeProvider interface {
	RunnerScript() string
	RepoFlagDescription() string
	IssueFlagDescription() string
	PullRequestFlagDescription() string
	FollowUpIssueFlagDescription(scope string) string
}

type githubRuntimeProvider struct{}

func defaultRuntimeProvider() runtimeProvider {
	return githubRuntimeProvider{}
}

func (githubRuntimeProvider) RunnerScript() string {
	return githubRunnerScript
}

func (githubRuntimeProvider) RepoFlagDescription() string {
	return "repository in owner/name format for the current runtime"
}

func (githubRuntimeProvider) IssueFlagDescription() string {
	return "tracker issue number"
}

func (githubRuntimeProvider) PullRequestFlagDescription() string {
	return "code host pull request number"
}

func (githubRuntimeProvider) FollowUpIssueFlagDescription(scope string) string {
	if scope == "post-batch verification" {
		return "create a tracker follow-up issue automatically when post-batch verification fails"
	}
	return "create a tracker follow-up issue automatically when verification fails"
}
