package cli

import (
	"context"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/lifecycle"
)

type daemonLifecycle interface {
	FetchIssue(ctx context.Context, repo string, number int) (lifecycle.Issue, error)
	ListIssues(ctx context.Context, repo, state string, limit int) ([]lifecycle.Issue, error)
	ListIssueComments(ctx context.Context, repo string, number int) ([]lifecycle.IssueComment, error)
	FindOpenPullRequestForIssue(ctx context.Context, repo string, issue lifecycle.Issue) (*lifecycle.PullRequest, error)
	ReviewThreadsForPullRequest(ctx context.Context, repo string, number int) ([]lifecycle.PullRequestReviewThread, error)
	ConversationCommentsForPullRequest(ctx context.Context, repo string, number int) ([]lifecycle.PullRequestConversationComment, error)
	CommentOnIssue(ctx context.Context, repo string, number int, body string) error
	CreateIssue(ctx context.Context, req lifecycle.CreateIssueRequest) (lifecycle.Issue, error)
}
