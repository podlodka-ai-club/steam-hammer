package orchestration

import (
	"encoding/json"
	"strings"
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

func TestConfiguredWorkflowCommandsUsesWorkflowCheckOrder(t *testing.T) {
	commands := ConfiguredWorkflowCommands(map[string]any{
		"workflow": map[string]any{
			"commands": map[string]any{
				"build": "make build",
				"test":  "make test",
				"lint":  "make lint",
				"setup": "make setup",
			},
		},
	})
	if len(commands) != 3 {
		t.Fatalf("len(commands) = %d, want 3", len(commands))
	}
	if commands[0].Name != "test" || commands[1].Name != "lint" || commands[2].Name != "build" {
		t.Fatalf("commands = %#v", commands)
	}
}

func TestWorkflowOutputExcerptCompactsWhitespace(t *testing.T) {
	got := WorkflowOutputExcerpt("line 1\n\n  line 2\tline 3  ", 600)
	if got != "line 1 line 2 line 3" {
		t.Fatalf("WorkflowOutputExcerpt() = %q", got)
	}
}

func TestRecommendedPostBatchFollowUpIssueIncludesEvidence(t *testing.T) {
	issue := RecommendedPostBatchFollowUpIssue("owner/repo", VerificationResult{
		Status:     StatusFailed,
		Summary:    "failed (1/2 passed; failed: go-test)",
		NextAction: "create_follow_up_issue_and_fix_regression",
		Commands: []VerificationCommandResult{{
			Name:          "go-test",
			Command:       "go test ./...",
			Status:        StatusFailed,
			StderrExcerpt: "go test failed",
		}},
	}, []string{"https://github.com/owner/repo/pull/12"})

	if issue.Title != "Post-batch verification failed: go-test" {
		t.Fatalf("Title = %q", issue.Title)
	}
	for _, want := range []string{
		"Repository: owner/repo",
		"Touched PRs:",
		"https://github.com/owner/repo/pull/12",
		"evidence: go test failed",
	} {
		if !strings.Contains(issue.Body, want) {
			t.Fatalf("Body missing %q\n%s", want, issue.Body)
		}
	}
}
