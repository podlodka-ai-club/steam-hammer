package orchestration

import (
	"reflect"
	"testing"
)

func TestCountApprovingReviews(t *testing.T) {
	summary := CountApprovingReviews([]PullRequestReview{
		{State: "COMMENTED", SubmittedAt: "2026-04-28T09:00:00Z", AuthorLogin: "reviewer1"},
		{State: "APPROVED", SubmittedAt: "2026-04-28T10:00:00Z", AuthorLogin: "reviewer1"},
		{State: "APPROVED", SubmittedAt: "2026-04-28T11:00:00Z", AuthorLogin: "author"},
		{State: "CHANGES_REQUESTED", SubmittedAt: "2026-04-28T12:00:00Z", AuthorLogin: "reviewer2"},
	}, "author")

	if summary.ApprovedCount != 1 {
		t.Fatalf("ApprovedCount = %d, want 1", summary.ApprovedCount)
	}
	if !reflect.DeepEqual(summary.ApprovedBy, []string{"reviewer1"}) {
		t.Fatalf("ApprovedBy = %#v, want [reviewer1]", summary.ApprovedBy)
	}
	if got := summary.LatestReviewStates["reviewer2"]; got != "CHANGES_REQUESTED" {
		t.Fatalf("LatestReviewStates[reviewer2] = %q, want CHANGES_REQUESTED", got)
	}
}

func TestEvaluatePRReadiness(t *testing.T) {
	tests := []struct {
		name         string
		facts        PRReadinessFacts
		policy       PRReadinessPolicy
		files        PRRequiredFileValidation
		wantStatus   string
		wantAction   string
		wantError    string
		wantMissing  []string
		wantPending  []string
		wantApproved int
		wantRequired int
	}{
		{
			name: "failing green checks block",
			facts: PRReadinessFacts{
				CIOverall: "failure",
				CIChecks:  []PRCICheck{{Name: "ci/test", State: "failure", URL: "https://example/check/1"}},
			},
			policy:     PRReadinessPolicy{RequireGreenChecks: true},
			wantStatus: StatusBlocked,
			wantAction: NextActionInspectFailingCIChecks,
			wantError:  "CI failing checks: ci/test (https://example/check/1)",
		},
		{
			name: "required check presence waits for ci",
			facts: PRReadinessFacts{
				CIOverall: "success",
				CIChecks:  []PRCICheck{{Name: "ci / lint", State: "success"}},
			},
			policy:      PRReadinessPolicy{RequiredChecks: []string{"ci / test"}},
			wantStatus:  StatusWaitingForCI,
			wantAction:  NextActionWaitForCI,
			wantError:   "missing required checks: ci / test",
			wantMissing: []string{"ci / test"},
		},
		{
			name: "pending required checks wait for ci",
			facts: PRReadinessFacts{
				CIOverall: "success",
				CIChecks:  []PRCICheck{{Name: "ci / test", State: "pending"}},
			},
			policy:      PRReadinessPolicy{RequiredChecks: []string{"ci / test"}},
			wantStatus:  StatusWaitingForCI,
			wantAction:  NextActionWaitForCI,
			wantError:   "pending required checks: ci / test",
			wantPending: []string{"ci / test"},
		},
		{
			name:       "required file evidence blocks",
			facts:      PRReadinessFacts{CIOverall: "success"},
			files:      PRRequiredFileValidation{Status: StatusBlocked, MissingFiles: []string{"b.md", "a.md"}},
			wantStatus: StatusBlocked,
			wantAction: NextActionUpdatePRWithRequiredFiles,
			wantError:  "Missing required file evidence: a.md, b.md",
		},
		{
			name:       "mergeability blocker blocks",
			facts:      PRReadinessFacts{MergeStateStatus: "DIRTY"},
			policy:     PRReadinessPolicy{RequireMergeable: true},
			wantStatus: StatusBlocked,
			wantAction: NextActionResolveMergeabilityBlockers,
			wantError:  "PR merge state is not ready: DIRTY",
		},
		{
			name: "required approval waits for review",
			facts: PRReadinessFacts{
				CIOverall:     "success",
				CIChecks:      []PRCICheck{{Name: "ci / test", State: "success"}},
				ApprovedCount: 0,
			},
			policy:       PRReadinessPolicy{RequiredChecks: []string{"ci / test"}, RequiredApprovals: 1},
			wantStatus:   StatusReadyForReview,
			wantAction:   NextActionWaitForReview,
			wantError:    "Waiting for required approvals: 0/1",
			wantApproved: 0,
			wantRequired: 1,
		},
		{
			name: "review-required compatibility action",
			facts: PRReadinessFacts{
				CIOverall:     "success",
				CIChecks:      []PRCICheck{{Name: "ci / test", State: "success"}},
				ApprovedCount: 0,
			},
			policy: PRReadinessPolicy{
				RequiredChecks:      []string{"ci / test"},
				RequireReview:       true,
				ReviewPendingAction: "await_required_approval",
			},
			wantStatus:   StatusReadyForReview,
			wantAction:   "await_required_approval",
			wantError:    "Waiting for required approvals: 0/1",
			wantApproved: 0,
			wantRequired: 1,
		},
		{
			name: "ready to merge when gates pass",
			facts: PRReadinessFacts{
				CIOverall:     "success",
				CIChecks:      []PRCICheck{{Name: "ci / test", State: "success"}},
				ApprovedCount: 1,
			},
			policy:       PRReadinessPolicy{RequiredChecks: []string{"ci / test"}, RequiredApprovals: 1},
			wantStatus:   StatusReadyToMerge,
			wantAction:   "ready_for_merge",
			wantApproved: 1,
			wantRequired: 1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := EvaluatePRReadiness(tt.facts, tt.policy, tt.files)
			if got.Status != tt.wantStatus {
				t.Fatalf("status = %q, want %q", got.Status, tt.wantStatus)
			}
			if got.NextAction != tt.wantAction {
				t.Fatalf("next action = %q, want %q", got.NextAction, tt.wantAction)
			}
			if got.Error != tt.wantError {
				t.Fatalf("error = %q, want %q", got.Error, tt.wantError)
			}
			if !reflect.DeepEqual(got.MissingRequiredChecks, tt.wantMissing) {
				t.Fatalf("MissingRequiredChecks = %#v, want %#v", got.MissingRequiredChecks, tt.wantMissing)
			}
			if !reflect.DeepEqual(got.PendingRequiredChecks, tt.wantPending) {
				t.Fatalf("PendingRequiredChecks = %#v, want %#v", got.PendingRequiredChecks, tt.wantPending)
			}
			if got.ApprovedCount != tt.wantApproved {
				t.Fatalf("ApprovedCount = %d, want %d", got.ApprovedCount, tt.wantApproved)
			}
			if got.RequiredApprovals != tt.wantRequired {
				t.Fatalf("RequiredApprovals = %d, want %d", got.RequiredApprovals, tt.wantRequired)
			}
		})
	}
}

func TestFormatFailingCIChecksSummary(t *testing.T) {
	summary := FormatFailingCIChecksSummary([]PRCICheck{
		{Name: "first", URL: "https://example/1"},
		{Name: "second"},
		{Name: "third"},
	}, 2)

	want := "CI failing checks: first (https://example/1); second; and 1 more"
	if summary != want {
		t.Fatalf("FormatFailingCIChecksSummary() = %q, want %q", summary, want)
	}
}

func TestIsAutonomousReadyStatus(t *testing.T) {
	for _, status := range []string{StatusReadyForReview, StatusWaitingForCI, StatusReadyToMerge} {
		if !IsAutonomousReadyStatus(status) {
			t.Fatalf("IsAutonomousReadyStatus(%q) = false, want true", status)
		}
	}
	if IsAutonomousReadyStatus(StatusBlocked) {
		t.Fatalf("IsAutonomousReadyStatus(%q) = true, want false", StatusBlocked)
	}
}
