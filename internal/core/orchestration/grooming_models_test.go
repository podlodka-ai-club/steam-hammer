package orchestration

import (
	"strings"
	"testing"
)

func TestBuildAndParseGroomingComment(t *testing.T) {
	comment, err := BuildGroomingComment(GroomingSummary{
		Status:             "PLAN_READY",
		Goal:               "Introduce grooming comments without changing execution.",
		NonGoals:           []string{"Do not run grooming workers"},
		Assumptions:        []string{"Tracker comments remain the recovery source"},
		Risks:              []string{"Future payload changes need additive fields"},
		Dependencies:       []string{"Parent issue #339"},
		AcceptanceCriteria: []string{"Formatting and parsing are covered by tests"},
		TouchedAreas:       []string{"internal/core/orchestration"},
		ImplementationPlan: []string{"Add model", "Add comment helpers"},
		ValidationPlan:     []string{"go test ./..."},
	})
	if err != nil {
		t.Fatalf("BuildGroomingComment() error = %v", err)
	}
	if !strings.Contains(comment, OrchestrationGroomingMarker) {
		t.Fatalf("comment missing grooming marker: %s", comment)
	}
	if !strings.Contains(comment, "Status: plan-ready") || !strings.Contains(comment, "Implementation plan:") {
		t.Fatalf("comment is not human-readable enough: %s", comment)
	}

	parsed, err := ParseGroomingCommentBody(comment)
	if err != nil {
		t.Fatalf("ParseGroomingCommentBody() error = %v", err)
	}
	if parsed.Status != GroomingStatusPlanReady {
		t.Fatalf("status = %q, want %q", parsed.Status, GroomingStatusPlanReady)
	}
	if parsed.Goal != "Introduce grooming comments without changing execution." {
		t.Fatalf("goal = %q", parsed.Goal)
	}
	if len(parsed.ImplementationPlan) != 2 || parsed.ImplementationPlan[1] != "Add comment helpers" {
		t.Fatalf("implementation plan = %#v", parsed.ImplementationPlan)
	}
}

func TestParseGroomingCommentBodyMalformedPayload(t *testing.T) {
	body := strings.Join([]string{
		OrchestrationGroomingMarker,
		"```json",
		"{not-json}",
		"```",
	}, "\n")

	parsed, err := ParseGroomingCommentBody(body)
	if err == nil {
		t.Fatalf("ParseGroomingCommentBody() error = nil, want malformed payload error")
	}
	if parsed != nil {
		t.Fatalf("ParseGroomingCommentBody() = %#v, want nil", parsed)
	}
}

func TestSelectLatestParseableGroomingCommentIgnoresMalformedComments(t *testing.T) {
	comments := []TrackerComment{
		{ID: 1, CreatedAt: "2026-05-01T10:00:00Z", HTMLURL: "https://example/1", Body: OrchestrationGroomingMarker + "\n```json\n{not-json}\n```"},
		{ID: 2, CreatedAt: "2026-05-01T11:00:00Z", HTMLURL: "https://example/2", Body: OrchestrationGroomingMarker + "\n```json\n{\"status\":\"questions_ready\",\"goal\":\"Clarify scope\"}\n```"},
		{ID: 3, CreatedAt: "2026-05-01T12:00:00Z", HTMLURL: "https://example/3", Body: "regular human comment"},
		{ID: 4, CreatedAt: "2026-05-01T13:00:00Z", HTMLURL: "https://example/4", Body: OrchestrationGroomingMarker + "\n```json\n{\"status\":\"approved\",\"goal\":\"Ready to implement\"}\n```"},
	}

	latest, warnings := SelectLatestParseableGroomingComment(comments, "issue #340")
	if latest == nil {
		t.Fatalf("SelectLatestParseableGroomingComment() = nil, want payload")
	}
	if latest.CommentID != 4 {
		t.Fatalf("comment id = %d, want 4", latest.CommentID)
	}
	if latest.Status != GroomingStatusApproved {
		t.Fatalf("status = %q, want %q", latest.Status, GroomingStatusApproved)
	}
	if latest.Payload.Goal != "Ready to implement" {
		t.Fatalf("goal = %q", latest.Payload.Goal)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "ignoring malformed grooming comment") {
		t.Fatalf("warnings = %#v, want one malformed grooming warning", warnings)
	}
}

func TestNormalizeGroomingStatus(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{name: "not required", in: " Not_Required ", want: GroomingStatusNotRequired},
		{name: "in progress", in: "IN PROGRESS", want: GroomingStatusInProgress},
		{name: "questions ready", in: "question-ready", want: GroomingStatusQuestionsReady},
		{name: "plan ready", in: "planned", want: GroomingStatusPlanReady},
		{name: "approved", in: "approved", want: GroomingStatusApproved},
		{name: "blocked", in: "BLOCKED", want: GroomingStatusBlocked},
		{name: "unknown preserved normalized", in: "Needs Review", want: "needs-review"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := NormalizeGroomingStatus(tt.in); got != tt.want {
				t.Fatalf("NormalizeGroomingStatus(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}
