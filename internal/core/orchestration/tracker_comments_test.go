package orchestration

import (
	"strings"
	"testing"
)

func TestParseOrchestrationStateCommentBodyParsesRealisticComment(t *testing.T) {
	body := strings.Join([]string{
		"## Orchestration State",
		"",
		"Runner state update for issue #74.",
		OrchestrationStateMarker,
		"```json",
		`{"status":"ready-for-review","task_type":"issue","issue":74,"branch":"issue-fix/74-state-v1","stage":"agent_run","next_action":"await_human_review"}`,
		"```",
	}, "\n")

	payload, err := ParseOrchestrationStateCommentBody(body)
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if payload == nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() = nil, want payload")
	}
	if payload.Status != StatusReadyForReview {
		t.Fatalf("status = %q, want %q", payload.Status, StatusReadyForReview)
	}
	if payload.TaskType != "issue" {
		t.Fatalf("task type = %q, want issue", payload.TaskType)
	}
	if payload.Issue == nil || *payload.Issue != 74 {
		t.Fatalf("issue = %v, want 74", payload.Issue)
	}
	if payload.Branch != "issue-fix/74-state-v1" {
		t.Fatalf("branch = %q, want issue-fix/74-state-v1", payload.Branch)
	}
	if payload.NextAction != "await_human_review" {
		t.Fatalf("next action = %q, want await_human_review", payload.NextAction)
	}
}

func TestSelectLatestParseableOrchestrationStateIgnoresMalformedComments(t *testing.T) {
	comments := []TrackerComment{
		{ID: 1, CreatedAt: "2026-04-25T10:00:00Z", HTMLURL: "https://example/1", Body: OrchestrationStateMarker + "\n```json\n{not-json}\n```"},
		{ID: 2, CreatedAt: "2026-04-25T11:00:00Z", HTMLURL: "https://example/2", Body: OrchestrationStateMarker + "\n```json\n{\"status\":\"failed\",\"error\":\"test failure\"}\n```"},
		{ID: 3, CreatedAt: "2026-04-25T12:00:00Z", HTMLURL: "https://example/3", Body: OrchestrationStateMarker + "\n```json\n{\"status\":\"waiting-for-author\",\"error\":\"need clarification\"}\n```"},
	}

	latest, warnings := SelectLatestParseableOrchestrationState(comments, "issue #45")
	if latest == nil {
		t.Fatalf("SelectLatestParseableOrchestrationState() = nil, want payload")
	}
	if latest.Status != StatusWaitingForAuthor {
		t.Fatalf("status = %q, want %q", latest.Status, StatusWaitingForAuthor)
	}
	if latest.CommentID != 3 {
		t.Fatalf("comment id = %d, want 3", latest.CommentID)
	}
	if len(warnings) != 1 {
		t.Fatalf("warnings = %v, want 1 warning", warnings)
	}
	if !strings.Contains(warnings[0], "ignoring malformed orchestration state comment") {
		t.Fatalf("warning = %q, want malformed state warning", warnings[0])
	}
}

func TestSelectLatestParseableOrchestrationClaimParsesClaimComments(t *testing.T) {
	comments := []TrackerComment{
		{ID: 8, CreatedAt: "2026-04-28T10:00:00Z", HTMLURL: "https://example/claim-1", Body: OrchestrationClaimMarker + "\n```json\n{\"status\":\"released\",\"run_id\":\"run-0\"}\n```"},
		{ID: 9, CreatedAt: "2026-04-28T10:05:00Z", HTMLURL: "https://example/claim-2", Body: strings.Join([]string{"Claim refresh", OrchestrationClaimMarker, "```json", `{"status":"claimed","issue":93,"run_id":"run-1","worker":"pid-123","claimed_at":"2026-04-28T10:05:00Z","expires_at":"2026-04-28T10:06:00Z"}`, "```"}, "\n")},
	}

	latest, warnings := SelectLatestParseableOrchestrationClaim(comments, "issue #93")
	if latest == nil {
		t.Fatalf("SelectLatestParseableOrchestrationClaim() = nil, want payload")
	}
	if len(warnings) != 0 {
		t.Fatalf("warnings = %v, want none", warnings)
	}
	if latest.Status != "claimed" {
		t.Fatalf("status = %q, want claimed", latest.Status)
	}
	if got, ok := latest.Payload["issue"].(float64); !ok || int(got) != 93 {
		t.Fatalf("issue = %#v, want 93", latest.Payload["issue"])
	}
	if latest.CommentID != 9 {
		t.Fatalf("comment id = %d, want 9", latest.CommentID)
	}
}

func TestSelectLatestParseableDecompositionPlanParsesLatestPlan(t *testing.T) {
	comments := []TrackerComment{
		{ID: 12, CreatedAt: "2026-04-27T12:00:00Z", HTMLURL: "https://example/plan-1", Body: OrchestrationDecompositionMarker + "\n```json\n{\"status\":\"proposed\",\"parent_issue\":105,\"proposed_children\":[]}\n```"},
		{ID: 13, CreatedAt: "2026-04-27T12:05:00Z", HTMLURL: "https://example/plan-2", Body: strings.Join([]string{
			"Planning-only decomposition update",
			OrchestrationDecompositionMarker,
			"```json",
			`{"status":"children_created","parent_issue":105,"proposed_children":[{"order":1,"title":"Child implementation","depends_on":[],"status":"created","issue_number":201}],"created_children":[{"order":1,"title":"Child implementation","issue_number":201,"issue_url":"https://example/issues/201","status":"created"}]}`,
			"```",
		}, "\n")},
	}

	latest, warnings := SelectLatestParseableDecompositionPlan(comments, "issue #105")
	if latest == nil {
		t.Fatalf("SelectLatestParseableDecompositionPlan() = nil, want payload")
	}
	if len(warnings) != 0 {
		t.Fatalf("warnings = %v, want none", warnings)
	}
	if latest.Status != "children_created" {
		t.Fatalf("status = %q, want children_created", latest.Status)
	}
	children, ok := latest.Payload["proposed_children"].([]any)
	if !ok || len(children) != 1 {
		t.Fatalf("proposed_children = %#v, want one child", latest.Payload["proposed_children"])
	}
	if latest.URL != "https://example/plan-2" {
		t.Fatalf("url = %q, want https://example/plan-2", latest.URL)
	}
	if latest.CommentID != 13 {
		t.Fatalf("comment id = %d, want 13", latest.CommentID)
	}
}
