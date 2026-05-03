package cli

import (
	"context"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/lifecycle"
)

type daemonLifecycle interface {
	ListIssues(ctx context.Context, repo, state string, limit int) ([]lifecycle.Issue, error)
	ListIssueComments(ctx context.Context, repo string, number int) ([]lifecycle.IssueComment, error)
	CommentOnIssue(ctx context.Context, repo string, number int, body string) error
	CreateIssue(ctx context.Context, req lifecycle.CreateIssueRequest) (lifecycle.Issue, error)
}
