package cli

import (
	"context"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/githublifecycle"
)

type daemonLifecycle interface {
	ListIssues(ctx context.Context, repo, state string, limit int) ([]githublifecycle.Issue, error)
	ListIssueComments(ctx context.Context, repo string, number int) ([]githublifecycle.IssueComment, error)
	FindOpenPullRequestForIssue(ctx context.Context, repo string, issue githublifecycle.Issue) (*githublifecycle.PullRequest, error)
	ReviewThreadsForPullRequest(ctx context.Context, repo string, number int) ([]githublifecycle.PullRequestReviewThread, error)
	ConversationCommentsForPullRequest(ctx context.Context, repo string, number int) ([]githublifecycle.PullRequestConversationComment, error)
	CommentOnIssue(ctx context.Context, repo string, number int, body string) error
	CreateIssue(ctx context.Context, req githublifecycle.CreateIssueRequest) (githublifecycle.Issue, error)
}
