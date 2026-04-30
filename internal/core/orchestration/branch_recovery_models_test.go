package orchestration

import "testing"

func TestNewIssueBranchName(t *testing.T) {
	tests := []struct {
		name    string
		tracker string
		issueRef string
		title   string
		want    string
	}{
		{name: "github issue branch", tracker: TrackerGitHub, issueRef: "245", title: "Extract branch and recovery execution model into Go", want: "issue-fix/245-extract-branch-and-recovery-execution-mo"},
		{name: "jira ref lowercased", tracker: TrackerJira, issueRef: "PLAT-245", title: "Recover CI Context", want: "issue-fix/plat-245-recover-ci-context"},
		{name: "empty title falls back", tracker: TrackerGitHub, issueRef: "7", title: "!!!", want: "issue-fix/7-issue"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			branch := NewIssueBranchName("issue-fix", tt.issueRef, tt.title, tt.tracker)
			if branch.FullName != tt.want {
				t.Fatalf("FullName = %q, want %q", branch.FullName, tt.want)
			}
		})
	}
}

func TestSanitizeBranchForPath(t *testing.T) {
	if got := SanitizeBranchForPath("feature/foo bar/#245"); got != "feature-foo-bar-245" {
		t.Fatalf("SanitizeBranchForPath() = %q", got)
	}
	if got := SanitizeBranchForPath("   "); got != SanitizedBranchPathFallback {
		t.Fatalf("SanitizeBranchForPath() blank = %q, want %q", got, SanitizedBranchPathFallback)
	}
}

func TestExpectedGitContextValidate(t *testing.T) {
	err := (ExpectedGitContext{Branch: "issue-fix/245-branch-model", RepoRoot: "/tmp/repo"}).Validate(
		"push branch",
		"main",
		"/tmp/repo",
	)
	if err == nil {
		t.Fatal("Validate() error = nil, want mismatch")
	}
	if got := err.Error(); got != "Refusing to push branch: expected branch 'issue-fix/245-branch-model' in repo '/tmp/repo', but current context is branch 'main' in repo '/tmp/repo'" {
		t.Fatalf("Validate() branch mismatch = %q", got)
	}

	err = (ExpectedGitContext{RepoRoot: "/tmp/expected"}).Validate(
		"commit changes",
		"issue-fix/245-branch-model",
		"/tmp/actual",
	)
	if err == nil {
		t.Fatal("Validate() repo mismatch = nil, want mismatch")
	}
	if got := err.Error(); got != "Refusing to commit changes: expected branch 'issue-fix/245-branch-model' in repo '/tmp/expected', but current context is branch 'issue-fix/245-branch-model' in repo '/tmp/actual'" {
		t.Fatalf("Validate() repo mismatch = %q", got)
	}
}

func TestReusedBranchSyncVerdictSummary(t *testing.T) {
	v := ReusedBranchSyncVerdict{
		BranchName:      "issue-fix/245-branch-model",
		RemoteBaseRef:   "origin/main",
		AppliedStrategy: "merge",
		Status:          BranchSyncStatusAutoResolved,
	}
	if got := v.Summary(false); got != "Conflict recovery result for branch 'issue-fix/245-branch-model': auto-resolved conflicts against 'origin/main' via merge" {
		t.Fatalf("Summary() = %q", got)
	}

	v.AppliedStrategy = "rebase"
	if got := v.PushSummary(false); got != "Conflict recovery push result for branch 'issue-fix/245-branch-model': pushed (force-with-lease: yes)" {
		t.Fatalf("PushSummary() = %q", got)
	}
}

func TestSummarizeRecoveryVerificationResults(t *testing.T) {
	results := []VerificationStep{{Name: "go-test", Status: "passed"}, {Name: "lint", Status: StatusFailed}}
	if got := SummarizeRecoveryVerificationResults(results); got != "failed (1/2 passed; failed: lint)" {
		t.Fatalf("SummarizeRecoveryVerificationResults() = %q", got)
	}
	if got := SummarizeRecoveryVerificationResults(nil); got != RecoverySummaryPassedZeroCommands {
		t.Fatalf("SummarizeRecoveryVerificationResults(nil) = %q", got)
	}
}

func TestRecoveryStageMappings(t *testing.T) {
	if got := FailureStateForStage(FailureStageWorkflowChecks); got != StatusBlocked {
		t.Fatalf("FailureStateForStage(workflow_checks) = %q", got)
	}
	if got := FailureStateForStage(FailureStageMergeExecution); got != StatusFailed {
		t.Fatalf("FailureStateForStage(merge_execution) = %q", got)
	}
	if got := RecoveryNextActionForStage(FailureStageBranchContextValidate); got != RecoveryActionRestoreBranchContext {
		t.Fatalf("RecoveryNextActionForStage(branch_context_validation) = %q", got)
	}
	if got := RecoveryNextActionForStage("unknown"); got != RecoveryActionInspectErrorAndRetry {
		t.Fatalf("RecoveryNextActionForStage(unknown) = %q", got)
	}
}

func TestRecoveryVerificationFailureError(t *testing.T) {
	err := RecoveryVerificationFailure{
		Scope: "focused",
		Verification: VerificationVerdict{Error: "Focused recovery verification failed: go test ./..."},
	}
	if got := err.Error(); got != "Focused recovery verification failed: go test ./..." {
		t.Fatalf("Error() = %q", got)
	}

	err = RecoveryVerificationFailure{Scope: "full-repo"}
	if got := err.Error(); got != "Full-repo recovery verification failed" {
		t.Fatalf("Error() fallback = %q", got)
	}
}
