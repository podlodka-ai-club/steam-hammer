package orchestration

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestEvaluateDaemonTaskSelectionSkipsWaitingForAuthorWithoutApprovedPlan(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:       249,
		LatestStateStatus: StatusWaitingForAuthor,
		LatestDecomposition: map[string]any{
			"status": "proposed",
		},
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if decision.Eligible {
		t.Fatalf("Eligible = true, want false")
	}
	if decision.Reason != "waiting for decomposition approval" {
		t.Fatalf("Reason = %q", decision.Reason)
	}
	if decision.Signature != "decomposition:proposed" {
		t.Fatalf("Signature = %q", decision.Signature)
	}
}

func TestEvaluateDaemonTaskSelectionAllowsApprovedDecompositionRerun(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:       249,
		LatestStateStatus: StatusWaitingForAuthor,
		LatestDecomposition: map[string]any{
			"status": "approved",
		},
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if !decision.Eligible {
		t.Fatalf("Eligible = false, want true (%q)", decision.Reason)
	}
	if decision.Signature != "decomposition:approved" {
		t.Fatalf("Signature = %q", decision.Signature)
	}
}

func TestEvaluateDaemonTaskSelectionSkipsActiveForeignClaim(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:       249,
		RunID:             "run-2",
		LatestStateStatus: StatusReadyForReview,
		LatestClaim: map[string]any{
			"status":     "claimed",
			"run_id":     "run-1",
			"expires_at": "2026-05-01T12:05:00Z",
		},
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if decision.Eligible {
		t.Fatalf("Eligible = true, want false")
	}
	if decision.Reason != "actively claimed by another daemon worker" {
		t.Fatalf("Reason = %q", decision.Reason)
	}
}

func TestEvaluateDaemonTaskSelectionUsesFixedNowForExpiredForeignClaim(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:       249,
		RunID:             "run-2",
		LatestStateStatus: StatusReadyForReview,
		LatestClaim: map[string]any{
			"status":     "claimed",
			"run_id":     "run-1",
			"expires_at": "2026-05-01T12:05:00Z",
		},
	}, time.Date(2026, 5, 1, 12, 6, 0, 0, time.UTC))

	if !decision.Eligible {
		t.Fatalf("Eligible = false, want true (%q)", decision.Reason)
	}
	if decision.Signature != "state:ready-for-review" {
		t.Fatalf("Signature = %q", decision.Signature)
	}
}

func TestEvaluateDaemonTaskSelectionSkipsAlreadyHandledSignature(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:          249,
		LatestStateStatus:    StatusReadyForReview,
		LastHandledSignature: "state:ready-for-review",
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if decision.Eligible {
		t.Fatalf("Eligible = true, want false")
	}
	if decision.Reason != "already handled in this daemon session" {
		t.Fatalf("Reason = %q", decision.Reason)
	}
}

func TestEvaluateDaemonTaskSelectionSkipsOpenDependencies(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:        249,
		LatestStateStatus:  StatusReadyForReview,
		OpenDependencyRefs: []string{"326", "327", "326"},
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if decision.Eligible {
		t.Fatalf("Eligible = true, want false")
	}
	if decision.Reason != "blocked by open dependencies: #326, #327" {
		t.Fatalf("Reason = %q", decision.Reason)
	}
	if decision.Signature != "state:ready-for-review" {
		t.Fatalf("Signature = %q", decision.Signature)
	}
}

func TestEvaluateDaemonTaskSelectionAllowsClosedDependencies(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:       249,
		LatestStateStatus: StatusReadyForReview,
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if !decision.Eligible {
		t.Fatalf("Eligible = false, want true (%q)", decision.Reason)
	}
}

func TestEvaluateDaemonTaskSelectionPrefersReviewFeedbackSignal(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:          249,
		LatestStateStatus:    StatusReadyForReview,
		ReviewFeedbackSignal: "pr-101:actionable",
		LastHandledSignature: "state:ready-for-review",
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if !decision.Eligible {
		t.Fatalf("Eligible = false, want true (%q)", decision.Reason)
	}
	if decision.Signature != "review:pr-101:actionable" {
		t.Fatalf("Signature = %q", decision.Signature)
	}
}

func TestEvaluateDaemonTaskSelectionPrefersConflictRecoverySignal(t *testing.T) {
	decision := EvaluateDaemonTaskSelection(DaemonTaskSnapshot{
		IssueNumber:          249,
		LatestStateStatus:    StatusReadyForReview,
		PRConflictSignal:     "pr-101:DIRTY:CONFLICTING",
		ReviewFeedbackSignal: "pr-101:actionable",
		LastHandledSignature: "review:pr-101:actionable",
	}, time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC))

	if !decision.Eligible {
		t.Fatalf("Eligible = false, want true (%q)", decision.Reason)
	}
	if decision.Signature != "conflict-recovery:pr-101:DIRTY:CONFLICTING" {
		t.Fatalf("Signature = %q", decision.Signature)
	}
}

func TestBuildDaemonClaimAndReleaseCommentsUseStableMarker(t *testing.T) {
	claimed := BuildDaemonClaimComment(249, "run-1", "daemon-1", time.Date(2026, 5, 1, 12, 0, 0, 0, time.UTC), time.Date(2026, 5, 1, 12, 5, 0, 0, time.UTC))
	released := BuildDaemonReleaseComment(249, "run-1", "daemon-1", time.Date(2026, 5, 1, 12, 2, 0, 0, time.UTC))

	for _, body := range []string{claimed, released} {
		if !strings.Contains(body, OrchestrationClaimMarker) {
			t.Fatalf("claim comment missing marker: %q", body)
		}
	}
}

func TestProcessedIssueStatusReadsStatusField(t *testing.T) {
	raw := json.RawMessage(`{"status":"waiting-for-ci"}`)
	if got := ProcessedIssueStatus(raw); got != StatusWaitingForCI {
		t.Fatalf("ProcessedIssueStatus() = %q, want %q", got, StatusWaitingForCI)
	}
}

func TestProcessedIssueSignatureIncludesTaskType(t *testing.T) {
	raw := json.RawMessage(`{"status":"waiting-for-ci","task_type":"pr"}`)
	if got := ProcessedIssueSignature(raw); got != "state:pr:waiting-for-ci" {
		t.Fatalf("ProcessedIssueSignature() = %q, want %q", got, "state:pr:waiting-for-ci")
	}
}
