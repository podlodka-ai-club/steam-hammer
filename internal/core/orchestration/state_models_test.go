package orchestration

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestClassifyPRMergeReadinessState(t *testing.T) {
	tests := []struct {
		name       string
		mergeState string
		mergeable  string
		want       string
	}{
		{name: "conflicting mergeable", mergeState: "clean", mergeable: "CONFLICTING", want: MergeReadinessConflicting},
		{name: "dirty state", mergeState: "DIRTY", mergeable: "mergeable", want: MergeReadinessConflicting},
		{name: "behind state", mergeState: "BEHIND", mergeable: "mergeable", want: MergeReadinessStale},
		{name: "clean mergeable", mergeState: "CLEAN", mergeable: "MERGEABLE", want: MergeReadinessClean},
		{name: "unknown fallback", mergeState: "", mergeable: "", want: MergeReadinessUnknown},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ClassifyPRMergeReadinessState(tt.mergeState, tt.mergeable); got != tt.want {
				t.Fatalf("ClassifyPRMergeReadinessState() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestEvaluatePRMergeReadiness(t *testing.T) {
	tests := []struct {
		name         string
		facts        PullRequestFacts
		verification *VerificationVerdict
		wantStatus   string
		wantAction   string
		wantError    string
		wantState    string
	}{
		{
			name:       "ready to merge",
			facts:      PullRequestFacts{MergeStateStatus: "CLEAN", Mergeable: "MERGEABLE", ReviewDecision: ReviewDecisionApproved},
			wantStatus: StatusReadyToMerge,
			wantAction: "ready_for_merge",
			wantState:  MergeReadinessClean,
		},
		{
			name:       "draft waits for author",
			facts:      PullRequestFacts{MergeStateStatus: "DRAFT", Mergeable: "UNKNOWN", ReviewDecision: ReviewDecisionUnknown, IsDraft: true},
			wantStatus: StatusWaitingForAuthor,
			wantAction: "mark_pr_ready_for_review",
			wantError:  "PR is still marked as draft",
			wantState:  MergeReadinessUnknown,
		},
		{
			name:       "conflicting merge blocked",
			facts:      PullRequestFacts{MergeStateStatus: "DIRTY", Mergeable: "CONFLICTING", ReviewDecision: ReviewDecisionApproved},
			wantStatus: StatusBlocked,
			wantAction: "resolve_merge_conflicts",
			wantError:  "PR is not mergeable yet (mergeStateStatus=DIRTY)",
			wantState:  MergeReadinessConflicting,
		},
		{
			name:       "stale branch blocked",
			facts:      PullRequestFacts{MergeStateStatus: "BEHIND", Mergeable: "MERGEABLE", ReviewDecision: ReviewDecisionApproved},
			wantStatus: StatusBlocked,
			wantAction: "sync_pr_with_base",
			wantError:  "PR branch is stale and must be synced with base (mergeStateStatus=BEHIND)",
			wantState:  MergeReadinessStale,
		},
		{
			name:       "changes requested waits for author",
			facts:      PullRequestFacts{MergeStateStatus: "CLEAN", Mergeable: "MERGEABLE", ReviewDecision: ReviewDecisionChangesRequested},
			wantStatus: StatusWaitingForAuthor,
			wantAction: "address_requested_changes",
			wantError:  "Review state still has requested changes",
			wantState:  MergeReadinessClean,
		},
		{
			name:       "review required waits for author",
			facts:      PullRequestFacts{MergeStateStatus: "CLEAN", Mergeable: "MERGEABLE", ReviewDecision: ReviewDecisionReviewRequired},
			wantStatus: StatusWaitingForAuthor,
			wantAction: "await_required_approval",
			wantError:  "Required approving review is still missing",
			wantState:  MergeReadinessClean,
		},
		{
			name:       "unknown mergeability blocked",
			facts:      PullRequestFacts{MergeStateStatus: "UNKNOWN", Mergeable: "UNKNOWN", ReviewDecision: ReviewDecisionApproved},
			wantStatus: StatusBlocked,
			wantAction: "inspect_merge_requirements",
			wantError:  "GitHub has not marked this PR mergeable yet (mergeStateStatus=UNKNOWN)",
			wantState:  MergeReadinessUnknown,
		},
		{
			name:         "failed verification blocks merge",
			facts:        PullRequestFacts{MergeStateStatus: "CLEAN", Mergeable: "MERGEABLE", ReviewDecision: ReviewDecisionApproved},
			verification: &VerificationVerdict{Status: StatusFailed, Summary: "failed (1/2 passed; failed: go-test)"},
			wantStatus:   StatusBlocked,
			wantAction:   "inspect_merge_result_verification",
			wantError:    "failed (1/2 passed; failed: go-test)",
			wantState:    MergeReadinessClean,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := EvaluatePRMergeReadiness(tt.facts, MergePolicy{Auto: true, Method: "squash"}, tt.verification)
			if got.Status != tt.wantStatus {
				t.Fatalf("status = %q, want %q", got.Status, tt.wantStatus)
			}
			if got.NextAction != tt.wantAction {
				t.Fatalf("next action = %q, want %q", got.NextAction, tt.wantAction)
			}
			if got.Error != tt.wantError {
				t.Fatalf("error = %q, want %q", got.Error, tt.wantError)
			}
			if got.MergeReadinessState != tt.wantState {
				t.Fatalf("merge readiness state = %q, want %q", got.MergeReadinessState, tt.wantState)
			}
		})
	}
}

func TestTrackedStateJSONIncludesMergeReadinessAndVerificationShape(t *testing.T) {
	issueNumber := 71
	tracked := TrackedState{
		Status:     StatusBlocked,
		TaskType:   "pr",
		Issue:      &issueNumber,
		Stage:      "merge_gate",
		NextAction: "inspect_merge_result_verification",
		MergeReadiness: &PRMergeReadiness{
			Status:              StatusBlocked,
			MergeReadinessState: MergeReadinessClean,
			MergeResultVerification: &VerificationVerdict{
				Status:     StatusFailed,
				Summary:    "failed (1/2 passed; failed: go-test)",
				NextAction: "create_follow_up_issue_and_fix_regression",
				Commands: []VerificationStep{{
					Name:    "go-test",
					Command: "go test ./...",
					Status:  StatusFailed,
				}},
			},
		},
	}

	payload, err := json.Marshal(tracked)
	if err != nil {
		t.Fatalf("Marshal() error = %v", err)
	}
	text := string(payload)
	for _, want := range []string{
		`"status":"blocked"`,
		`"task_type":"pr"`,
		`"merge_readiness"`,
		`"merge_readiness_state":"clean"`,
		`"merge_result_verification"`,
		`"next_action":"create_follow_up_issue_and_fix_regression"`,
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("Marshal() missing %q in %s", want, text)
		}
	}
}
