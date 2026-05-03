package cli

import (
	"context"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/lifecycle"
)

type issueLifecycle interface {
	FetchIssue(ctx context.Context, repo string, number int) (lifecycle.Issue, error)
	ListIssueComments(ctx context.Context, repo string, number int) ([]lifecycle.IssueComment, error)
	DefaultBranch(ctx context.Context, repo string) (string, error)
	FindOpenPullRequestForIssue(ctx context.Context, repo string, issue lifecycle.Issue) (*lifecycle.PullRequest, error)
	CommentOnIssue(ctx context.Context, repo string, number int, body string) error
	CreatePullRequest(ctx context.Context, req lifecycle.CreatePullRequestRequest) (string, error)
}
