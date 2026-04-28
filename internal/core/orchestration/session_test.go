package orchestration

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestParseStatePreservesCheckpointVerification(t *testing.T) {
	raw := []byte(`{
  "processed_issues": {"71": {"status": "ready-for-review"}},
  "checkpoint": {
    "run_id": "run-1",
    "phase": "completed",
    "batch_index": 2,
    "total_batches": 2,
    "counts": {"processed": 2, "failures": 1, "skipped_existing_pr": 1},
    "done": ["Autonomous batch loop finished across 2 issue(s)"],
    "current": "Idle between autonomous runs",
    "next": [],
    "issue_pr_actions": ["Touched 1 PR(s)"],
    "in_progress": [],
    "blockers": ["failed (1/2 passed; failed: go-test)"],
    "next_checkpoint": "when the next autonomous invocation starts",
    "updated_at": "2026-04-28T12:10:00+00:00",
    "verification": {
      "status": "failed",
      "summary": "failed (1/2 passed; failed: go-test)",
      "next_action": "create_follow_up_issue_and_fix_regression",
      "commands": [
        {"name": "go-test", "command": "go test ./...", "status": "failed", "exit_code": 1}
      ],
      "follow_up_issue": {"status": "recommended"}
    }
  }
}`)

	state, err := ParseState(raw)
	if err != nil {
		t.Fatalf("ParseState() error = %v", err)
	}
	if got := len(state.ProcessedIssues); got != 1 {
		t.Fatalf("processed issues len = %d, want 1", got)
	}
	if state.Checkpoint == nil {
		t.Fatal("checkpoint = nil")
	}
	if got := state.Checkpoint.Counts.SkippedExistingPR; got != 1 {
		t.Fatalf("SkippedExistingPR = %d, want 1", got)
	}
	if state.Checkpoint.Verification == nil {
		t.Fatal("verification = nil")
	}
	if got := state.Checkpoint.Verification.FollowUpIssue.Status; got != "recommended" {
		t.Fatalf("follow-up status = %q, want recommended", got)
	}
	if got := state.Checkpoint.Verification.Commands[0].ExitCode; got == nil || *got != 1 {
		t.Fatalf("exit code = %#v, want 1", got)
	}
}

func TestStateSummaryMatchesPythonCheckpointFormat(t *testing.T) {
	issueNumber := 164
	state := State{
		ProcessedIssues: map[string]json.RawMessage{"71": json.RawMessage(`{"status":"ready-for-review"}`)},
		Checkpoint: &Checkpoint{
			Phase:          "completed",
			BatchIndex:     2,
			TotalBatches:   2,
			Counts:         Counts{Processed: 2, Failures: 1, SkippedExistingPR: 1},
			Done:           []string{"Autonomous batch loop finished across 2 issue(s)"},
			Current:        "Idle between autonomous runs",
			IssuePRActions: []string{"Touched 1 PR(s)", "Recommended a verification follow-up issue"},
			Blockers:       []string{"failed (1/2 passed; failed: go-test)"},
			NextCheckpoint: "when the next autonomous invocation starts",
			UpdatedAt:      "2026-04-28T12:10:00+00:00",
			Verification: &VerificationResult{
				Status:  "failed",
				Summary: "failed (1/2 passed; failed: go-test)",
				FollowUpIssue: &FollowUpIssue{
					Status:      "created",
					IssueNumber: &issueNumber,
				},
			},
		},
	}

	summary := state.Summary()

	for _, want := range []string{
		"Autonomous session status: completed",
		"Batch: 2/2",
		"Done: Autonomous batch loop finished across 2 issue(s)",
		"Current: Idle between autonomous runs",
		"Next: no queued batches",
		"Issue/PR actions: Touched 1 PR(s); Recommended a verification follow-up issue",
		"In progress: none",
		"Blockers: failed (1/2 passed; failed: go-test)",
		"Next checkpoint: when the next autonomous invocation starts",
		"Counts: processed=2, failures=1, existing-pr=1",
		"Updated: 2026-04-28T12:10:00+00:00",
		"Verification: failed (1/2 passed; failed: go-test); follow-up issue #164 created",
	} {
		if !strings.Contains(summary, want) {
			t.Fatalf("Summary() missing %q\n%s", want, summary)
		}
	}
}

func TestStateSummaryWithoutCheckpointUsesProcessedIssueCount(t *testing.T) {
	state := State{ProcessedIssues: map[string]json.RawMessage{
		"71": json.RawMessage(`{"status":"ready-for-review"}`),
		"72": json.RawMessage(`{"status":"failed"}`),
	}}

	summary := state.Summary()

	if !strings.Contains(summary, "Done: processed issues recorded=2") {
		t.Fatalf("Summary() = %q", summary)
	}
}

func TestJoinOrFallbackDropsEmptyItems(t *testing.T) {
	got := joinOrFallback([]string{" first ", "", " second "}, "fallback")
	if got != "first; second" {
		t.Fatalf("joinOrFallback() = %q", got)
	}

	got = joinOrFallback(nil, "fallback")
	if got != "fallback" {
		t.Fatalf("joinOrFallback(nil) = %q", got)
	}
}

func TestParseStateInitializesProcessedIssuesMap(t *testing.T) {
	state, err := ParseState([]byte(`{"checkpoint":{"phase":"running"}}`))
	if err != nil {
		t.Fatalf("ParseState() error = %v", err)
	}
	if state.ProcessedIssues == nil {
		t.Fatal("ProcessedIssues = nil")
	}
	if !reflect.DeepEqual(state.ProcessedIssues, map[string]json.RawMessage{}) {
		t.Fatalf("ProcessedIssues = %#v", state.ProcessedIssues)
	}
}
