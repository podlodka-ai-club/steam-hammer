package orchestration

import (
	"reflect"
	"strings"
	"testing"
)

func TestIsActionableReviewFeedback(t *testing.T) {
	if IsActionableReviewFeedback("looks good") {
		t.Fatal("looks good should not be actionable")
	}
	if !IsActionableReviewFeedback("Please update the retry logic") {
		t.Fatal("expected actionable feedback")
	}
	if !IsActionableReviewFeedback("Use `strings.TrimSpace` here") {
		t.Fatal("code-formatted feedback should be actionable")
	}
	if IsActionableReviewFeedback("Orchestration state update\n\n<!-- orchestration-state:v1 -->\n```json\n{\"status\":\"failed\"}\n```") {
		t.Fatal("orchestration marker comment should not be actionable")
	}
}

func TestNormalizeReviewFeedback(t *testing.T) {
	items, stats := NormalizeReviewFeedback(
		[]ReviewThread{{
			Comments: []ReviewThreadComment{{Author: "reviewer", Body: "Please rename this", Path: "app.go", Line: 12, URL: "https://example/1"}},
		}},
		[]PullRequestReview{
			{AuthorLogin: "reviewer", SubmittedAt: "2026-04-28T10:00:00Z", State: "COMMENTED", Body: "looks good"},
			{AuthorLogin: "reviewer2", SubmittedAt: "2026-04-28T11:00:00Z", State: "APPROVED", Body: "Please add tests", URL: "https://example/2"},
			{AuthorLogin: "reviewer2", SubmittedAt: "2026-04-28T09:00:00Z", State: "COMMENTED", Body: "older"},
		},
		[]ConversationComment{{Author: "reviewer", Body: "Please rename this", URL: "https://example/3"}, {Author: "pr-owner", Body: "thanks"}},
		"pr-owner",
	)

	if len(items) != 2 {
		t.Fatalf("len(items) = %d, want 2 (%#v)", len(items), items)
	}
	wantTypes := []string{"review_comment", "review_summary"}
	gotTypes := []string{items[0].Type, items[1].Type}
	if !reflect.DeepEqual(gotTypes, wantTypes) {
		t.Fatalf("types = %#v, want %#v", gotTypes, wantTypes)
	}
	if stats.CommentsDuplicates != 0 {
		t.Fatalf("CommentsDuplicates = %d, want 0", stats.CommentsDuplicates)
	}
	if stats.ConversationDuplicates != 1 {
		t.Fatalf("ConversationDuplicates = %d, want 1", stats.ConversationDuplicates)
	}
	if stats.ReviewsSuperseded != 1 {
		t.Fatalf("ReviewsSuperseded = %d, want 1", stats.ReviewsSuperseded)
	}
	if stats.ReviewsNonActionable != 1 {
		t.Fatalf("ReviewsNonActionable = %d, want 1", stats.ReviewsNonActionable)
	}
	if stats.ConversationNonActionable != 1 {
		t.Fatalf("ConversationNonActionable = %d, want 1", stats.ConversationNonActionable)
	}
	if items[1].Author != "reviewer2" || items[1].State != "APPROVED" {
		t.Fatalf("review summary = %#v", items[1])
	}
}

func TestFormatReviewFeedbackStats(t *testing.T) {
	summary := FormatReviewFeedbackStats(ReviewFeedbackStats{ThreadsTotal: 1, CommentsTotal: 2, CommentsUsed: 1, ReviewsTotal: 1, ConversationTotal: 1})
	for _, want := range []string{"threads=total:1", "inline=total:2", "review_summaries=total:1", "conversation=total:1"} {
		if !strings.Contains(summary, want) {
			t.Fatalf("summary = %q, want substring %q", summary, want)
		}
	}
}
