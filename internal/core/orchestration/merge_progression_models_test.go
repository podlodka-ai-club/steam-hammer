package orchestration

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestFormatCIDiagnosticsSummary(t *testing.T) {
	summary := FormatCIDiagnosticsSummary(CIDiagnostics{
		FailingChecks: []CIFailingCheckDiagnostic{{
			Name: "ci/test",
			Classification: CIFailureClassification{
				Kind:   CIFailureKindTransient,
				Reason: "network timeout",
			},
		}},
	})

	if summary != "ci/test: transient (network timeout)" {
		t.Fatalf("FormatCIDiagnosticsSummary() = %q", summary)
	}
}

func TestEvaluateCIRecovery(t *testing.T) {
	transient := EvaluateCIRecovery(CIDiagnostics{
		OverallClassification: CIFailureKindTransient,
		FailingChecks:         []CIFailingCheckDiagnostic{{Name: "ci/test"}},
	})
	if !transient.Transient || transient.NextAction != CIRecoveryActionRetryTransientFailure {
		t.Fatalf("transient decision = %#v", transient)
	}

	real := EvaluateCIRecovery(CIDiagnostics{OverallClassification: CIFailureKindReal})
	if real.Transient || real.NextAction != NextActionInspectFailingCIChecks {
		t.Fatalf("real decision = %#v", real)
	}
}

func TestPullRequestChangedPathsDeduplicates(t *testing.T) {
	got := PullRequestChangedPaths([]PullRequestFileChange{{Path: "docs/runbook.md"}, {Path: "docs/runbook.md"}, {Path: "README.md"}})
	if !reflect.DeepEqual(got, []string{"docs/runbook.md", "README.md"}) {
		t.Fatalf("PullRequestChangedPaths() = %#v", got)
	}
}

func TestDetermineMergeResultVerificationNeed(t *testing.T) {
	docsOnly := DetermineMergeResultVerificationNeed(42, "main", []string{"docs/runbook.md", "README.md"}, nil)
	if docsOnly.Required || docsOnly.Reason != MergeVerificationReasonDocsOnly {
		t.Fatalf("docs-only decision = %#v", docsOnly)
	}

	central := DetermineMergeResultVerificationNeed(42, "main", []string{"internal/cli/app.go"}, nil)
	if !central.Required || central.Reason != MergeVerificationReasonCentralRunnerFiles {
		t.Fatalf("central-runner decision = %#v", central)
	}

	overlap := DetermineMergeResultVerificationNeed(
		42,
		"main",
		[]string{"pkg/service/handler.py"},
		[]OpenPullRequestCandidate{{Number: 77, HeadRefName: "issue-fix/77-overlap", BaseRefName: "main", ChangedFiles: []string{"pkg/service/handler.py"}}},
	)
	if !overlap.Required || overlap.Reason != MergeVerificationReasonOverlappingOpenPRs {
		t.Fatalf("overlap decision = %#v", overlap)
	}
	if len(overlap.OverlappingPRs) != 1 || overlap.OverlappingPRs[0].Number != 77 {
		t.Fatalf("overlapping PRs = %#v", overlap.OverlappingPRs)
	}
}

func TestEvaluateReviewFeedbackLoop(t *testing.T) {
	continueLoop := EvaluateReviewFeedbackLoop(1, 1, 2)
	if !continueLoop.Continue || continueLoop.Status != StatusInProgress {
		t.Fatalf("continueLoop = %#v", continueLoop)
	}

	blocked := EvaluateReviewFeedbackLoop(1, 2, 2)
	if blocked.Continue || blocked.Status != StatusBlocked || blocked.Stage != "review_feedback" {
		t.Fatalf("blocked = %#v", blocked)
	}

	waiting := EvaluateReviewFeedbackLoop(0, 1, 2)
	if waiting.Status != StatusWaitingForCI || waiting.NextAction != NextActionWaitForCI {
		t.Fatalf("waiting = %#v", waiting)
	}
}

func TestEvaluateSafeMergeExecution(t *testing.T) {
	queued := EvaluateSafeMergeExecution(PRMergeReadiness{Status: StatusReadyToMerge}, true)
	if !queued.Queued || queued.NextAction != MergeQueueActionAwaitTurn {
		t.Fatalf("queued decision = %#v", queued)
	}

	execute := EvaluateSafeMergeExecution(PRMergeReadiness{Status: StatusReadyToMerge}, false)
	if !execute.Execute || execute.NextAction != MergeQueueActionExecuteVerifiedMerge {
		t.Fatalf("execute decision = %#v", execute)
	}

	blocked := EvaluateSafeMergeExecution(PRMergeReadiness{Status: StatusBlocked, NextAction: "inspect_merge_requirements", Error: "merge blocked"}, false)
	if blocked.Execute || blocked.Queued || blocked.NextAction != "inspect_merge_requirements" {
		t.Fatalf("blocked decision = %#v", blocked)
	}
}

func TestEvaluatePolicyDrivenMergeQueue(t *testing.T) {
	queued := EvaluatePolicyDrivenMergeQueue(PRMergeReadiness{Status: StatusReadyToMerge}, true, true, nil)
	if !queued.Queued || queued.Status != StatusReadyToMerge || queued.Stage != "merge_queue" {
		t.Fatalf("queued decision = %#v", queued)
	}

	execute := EvaluatePolicyDrivenMergeQueue(PRMergeReadiness{Status: StatusReadyToMerge}, false, true, nil)
	if !execute.Execute || execute.NextAction != MergeQueueActionExecuteVerifiedMerge {
		t.Fatalf("execute decision = %#v", execute)
	}

	accepted := EvaluatePolicyDrivenMergeQueue(
		PRMergeReadiness{Status: StatusReadyToMerge},
		false,
		true,
		&MergeAttemptResult{Accepted: true, Status: StatusReadyToMerge},
	)
	if accepted.Execute || accepted.NextAction != MergeQueueActionAwaitAutoMerge || accepted.Stage != "merge_execution" {
		t.Fatalf("accepted decision = %#v", accepted)
	}

	policyBlocked := EvaluatePolicyDrivenMergeQueue(
		PRMergeReadiness{Status: StatusReadyToMerge},
		false,
		true,
		&MergeAttemptResult{Accepted: false, Status: StatusReadyToMerge, Error: "policy rejected auto-merge"},
	)
	if policyBlocked.Status != StatusWaitingForAuthor || policyBlocked.NextAction != MergeQueueActionManualMerge {
		t.Fatalf("policy blocked decision = %#v", policyBlocked)
	}

	hardBlocked := EvaluatePolicyDrivenMergeQueue(
		PRMergeReadiness{Status: StatusReadyToMerge},
		false,
		true,
		&MergeAttemptResult{Accepted: false, Status: StatusBlocked, Error: "required checks missing"},
	)
	if hardBlocked.Status != StatusBlocked || hardBlocked.NextAction != "inspect_merge_requirements" {
		t.Fatalf("hard blocked decision = %#v", hardBlocked)
	}
}

func TestTrackedStateParsesTypedPRProgressionPayloads(t *testing.T) {
	body := strings.Join([]string{
		OrchestrationStateMarker,
		"```json",
		`{"status":"blocked","task_type":"pr","ci_checks":[{"name":"ci/test","state":"failure","url":"https://ci.example/test"}],"ci_diagnostics":{"overall_classification":"transient","failing_checks":[{"name":"ci/test","classification":{"kind":"transient","reason":"network timeout"}}]},"required_file_validation":{"status":"blocked","missing_files":["docs/README.md"]},"merge_policy":{"auto":true,"method":"squash"}}`,
		"```",
	}, "\n")

	payload, err := ParseOrchestrationStateCommentBody(body)
	if err != nil {
		t.Fatalf("ParseOrchestrationStateCommentBody() error = %v", err)
	}
	if payload == nil || payload.CIDiagnostics == nil || payload.RequiredFileValidation == nil || payload.MergePolicy == nil {
		t.Fatalf("typed payloads missing: %#v", payload)
	}
	if payload.CIDiagnostics.OverallClassification != CIFailureKindTransient {
		t.Fatalf("OverallClassification = %q", payload.CIDiagnostics.OverallClassification)
	}
	if payload.RequiredFileValidation.MissingFiles[0] != "docs/README.md" {
		t.Fatalf("MissingFiles = %#v", payload.RequiredFileValidation.MissingFiles)
	}
	if !payload.MergePolicy.Auto || payload.MergePolicy.Method != "squash" {
		t.Fatalf("MergePolicy = %#v", payload.MergePolicy)
	}
	if len(payload.CIChecks) != 1 || payload.CIChecks[0].Name != "ci/test" {
		t.Fatalf("CIChecks = %#v", payload.CIChecks)
	}

	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("Marshal() error = %v", err)
	}
	text := string(encoded)
	for _, want := range []string{"\"ci_diagnostics\"", "\"required_file_validation\"", "\"merge_policy\""} {
		if !strings.Contains(text, want) {
			t.Fatalf("marshal missing %q in %s", want, text)
		}
	}
}
