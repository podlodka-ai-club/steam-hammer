package orchestration

import (
	"encoding/json"
	"testing"
)

func TestSummarizeVerificationResults(t *testing.T) {
	tests := []struct {
		name    string
		results []VerificationCommandResult
		want    string
	}{
		{name: "empty", want: "passed (0/0 commands)"},
		{name: "passed", results: []VerificationCommandResult{{Name: "go-test", Command: "go test ./...", Status: "passed"}}, want: "passed (1/1 commands)"},
		{name: "failed", results: []VerificationCommandResult{{Name: "go-test", Command: "go test ./...", Status: "passed"}, {Name: "lint", Command: "make lint", Status: StatusFailed}}, want: "failed (1/2 passed; failed: lint)"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := SummarizeVerificationResults(tt.results); got != tt.want {
				t.Fatalf("SummarizeVerificationResults() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestVerificationFollowUpIssueRequestExtractsRequestFields(t *testing.T) {
	issue := VerificationFollowUpIssue{
		Status: "recommended",
		FollowUpIssueRequest: FollowUpIssueRequest{
			Title: "Post-batch verification failed: go-test",
			Body:  "Please fix it.",
		},
		IssueURL: "https://github.com/owner/repo/issues/164",
	}

	request := issue.Request()

	if request.Title != "Post-batch verification failed: go-test" {
		t.Fatalf("request.Title = %q", request.Title)
	}
	if request.Body != "Please fix it." {
		t.Fatalf("request.Body = %q", request.Body)
	}
}

func TestVerificationFollowUpIssueUnmarshalPreservesStringIssueRef(t *testing.T) {
	var issue VerificationFollowUpIssue
	if err := json.Unmarshal([]byte(`{"status":"created","title":"verification","issue_number":"PROJ-164"}`), &issue); err != nil {
		t.Fatalf("Unmarshal() error = %v", err)
	}
	if issue.IssueRef != "PROJ-164" {
		t.Fatalf("IssueRef = %q, want PROJ-164", issue.IssueRef)
	}
	if issue.IssueNumber != nil {
		t.Fatalf("IssueNumber = %#v, want nil", issue.IssueNumber)
	}
}

func TestVerificationResultSummaryLineUsesCreatedFollowUpIssueRef(t *testing.T) {
	result := VerificationResult{
		Status:  StatusFailed,
		Summary: "failed (1/1 passed; failed: verify)",
		FollowUpIssue: &VerificationFollowUpIssue{
			Status:   "created",
			IssueRef: "PROJ-164",
		},
	}

	if got := result.SummaryLine(); got != "Verification: failed (1/1 passed; failed: verify); follow-up issue PROJ-164 created" {
		t.Fatalf("SummaryLine() = %q", got)
	}
}
