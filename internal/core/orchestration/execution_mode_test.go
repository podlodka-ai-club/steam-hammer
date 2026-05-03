package orchestration

import (
	"strings"
	"testing"
)

func TestChooseExecutionMode(t *testing.T) {
	tests := []struct {
		name                string
		issueNumber         int
		linkedOpenPR        *LinkedPullRequest
		forceIssueFlow      bool
		recoveredState      *TrackedState
		clarificationAnswer map[string]any
		wantMode            string
		wantReasonParts     []string
	}{
		{name: "issue flow when no open pr", issueNumber: 31, wantMode: ExecutionModeIssueFlow, wantReasonParts: []string{"no open PR linked"}},
		{name: "pr review when open pr exists", issueNumber: 31, linkedOpenPR: &LinkedPullRequest{Number: 120}, wantMode: ExecutionModePRReview, wantReasonParts: []string{"found linked open PR #120"}},
		{name: "force issue flow overrides auto switch", issueNumber: 31, linkedOpenPR: &LinkedPullRequest{Number: 120}, forceIssueFlow: true, wantMode: ExecutionModeIssueFlow, wantReasonParts: []string{"--force-issue-flow"}},
		{name: "force issue flow does not resume waiting for author", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, forceIssueFlow: true, recoveredState: &TrackedState{Status: StatusWaitingForAuthor}, wantMode: ExecutionModeSkip, wantReasonParts: []string{"waiting-for-author", "explicitly resumed"}},
		{name: "ready for review prefers pr review", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, recoveredState: &TrackedState{Status: StatusReadyForReview}, wantMode: ExecutionModePRReview, wantReasonParts: []string{"ready-for-review", "#144"}},
		{name: "waiting for author skips unless forced", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, recoveredState: &TrackedState{Status: StatusWaitingForAuthor}, wantMode: ExecutionModeSkip, wantReasonParts: []string{"waiting-for-author"}},
		{name: "blocked skips unless forced", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, recoveredState: &TrackedState{Status: StatusBlocked}, wantMode: ExecutionModeSkip, wantReasonParts: []string{"blocked"}},
		{name: "blocked conflicting pr resumes pr review", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144, MergeStateStatus: "DIRTY", Mergeable: "CONFLICTING"}, recoveredState: &TrackedState{Status: StatusBlocked}, wantMode: ExecutionModePRReview, wantReasonParts: []string{"blocked", "conflicting", "#144"}},
		{name: "waiting for ci prefers pr review", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, recoveredState: &TrackedState{Status: StatusWaitingForCI}, wantMode: ExecutionModePRReview, wantReasonParts: []string{"waiting-for-ci"}},
		{name: "ready to merge prefers pr review", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, recoveredState: &TrackedState{Status: StatusReadyToMerge}, wantMode: ExecutionModePRReview, wantReasonParts: []string{"ready-to-merge"}},
		{name: "clarification resumes pr review", issueNumber: 45, linkedOpenPR: &LinkedPullRequest{Number: 144}, recoveredState: &TrackedState{Status: StatusWaitingForAuthor, TaskType: TaskTypePR}, clarificationAnswer: map[string]any{"question": "ok?"}, wantMode: ExecutionModePRReview, wantReasonParts: []string{"newer author answer", "#144"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ChooseExecutionMode(tt.issueNumber, tt.linkedOpenPR, tt.forceIssueFlow, tt.recoveredState, tt.clarificationAnswer)
			if got.Mode != tt.wantMode {
				t.Fatalf("Mode = %q, want %q", got.Mode, tt.wantMode)
			}
			for _, want := range tt.wantReasonParts {
				if !strings.Contains(got.Reason, want) {
					t.Fatalf("Reason = %q, want substring %q", got.Reason, want)
				}
			}
		})
	}
}
