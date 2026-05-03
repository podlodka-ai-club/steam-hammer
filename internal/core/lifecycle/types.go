package lifecycle

import "github.com/podlodka-ai-club/steam-hammer/internal/core/githublifecycle"

// Provider identifiers shared by tracker/code host runtime boundaries.
const (
	TrackerGitHub  = "github"
	TrackerJira    = "jira"
	CodeHostGitHub = "github"
)

// Type aliases keep the core lifecycle boundary provider-agnostic while the
// GitHub adapter remains the concrete implementation during migration.
type (
	Actor                          = githublifecycle.Actor
	Label                          = githublifecycle.Label
	Issue                          = githublifecycle.Issue
	CreateIssueRequest             = githublifecycle.CreateIssueRequest
	IssueComment                   = githublifecycle.IssueComment
	PullRequest                    = githublifecycle.PullRequest
	IssueReference                 = githublifecycle.IssueReference
	PullRequestReview              = githublifecycle.PullRequestReview
	PullRequestChangedFile         = githublifecycle.PullRequestChangedFile
	PullRequestReviewThread        = githublifecycle.PullRequestReviewThread
	PullRequestReviewComment       = githublifecycle.PullRequestReviewComment
	PullRequestConversationComment = githublifecycle.PullRequestConversationComment
	CreatePullRequestRequest       = githublifecycle.CreatePullRequestRequest
	PullRequestCheck               = githublifecycle.PullRequestCheck
	PullRequestReadiness           = githublifecycle.PullRequestReadiness
)
