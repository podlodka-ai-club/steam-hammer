package cli

import (
	"context"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/githublifecycle"
)

type issueLifecycle interface {
	FetchIssue(ctx context.Context, repo string, number int) (githublifecycle.Issue, error)
	ListIssueComments(ctx context.Context, repo string, number int) ([]githublifecycle.IssueComment, error)
	DefaultBranch(ctx context.Context, repo string) (string, error)
	FindOpenPullRequestForIssue(ctx context.Context, repo string, issue githublifecycle.Issue) (*githublifecycle.PullRequest, error)
	CommentOnIssue(ctx context.Context, repo string, number int, body string) error
	CreatePullRequest(ctx context.Context, req githublifecycle.CreatePullRequestRequest) (string, error)
}
