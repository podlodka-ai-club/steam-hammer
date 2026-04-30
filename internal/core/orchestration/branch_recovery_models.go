package orchestration

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	TrackerGitHub = "github"
	TrackerJira   = "jira"
)

const (
	BranchLifecycleCreated = "created"
	BranchLifecycleReused  = "reused"
)

const (
	BranchSyncStatusDryRun         = "dry-run"
	BranchSyncStatusAlreadyCurrent = "already-current"
	BranchSyncStatusAutoResolved   = "auto-resolved"
	BranchSyncStatusSyncedCleanly  = "synced-cleanly"
)

const (
	FailureStageWorkflowSetup         = "workflow_setup"
	FailureStageWorkflowHooks         = "workflow_hooks"
	FailureStageWorkflowChecks        = "workflow_checks"
	FailureStageMergeExecution        = "merge_execution"
	FailureStageResidualUntracked     = "residual_untracked_validation"
	FailureStageTokenBudget           = "token_budget"
	FailureStageCostBudget            = "cost_budget"
	FailureStageBranchContextValidate = "branch_context_validation"
)

const (
	RecoveryActionFixWorkflowSetup       = "fix_workflow_setup_and_retry"
	RecoveryActionFixWorkflowHook        = "fix_workflow_hook_and_retry"
	RecoveryActionFixWorkflowChecks      = "fix_workflow_checks_and_retry"
	RecoveryActionInspectMergeExecution  = "inspect_merge_requirements_and_retry"
	RecoveryActionStageResidualFiles     = "stage_or-remove-residual-untracked-files"
	RecoveryActionRaiseTokenBudget       = "raise_token_budget_or_split_issue"
	RecoveryActionRaiseCostBudget        = "raise_cost_budget_or_split_issue"
	RecoveryActionRestoreBranchContext   = "restore_worker_branch_context_and_retry"
	RecoveryActionInspectErrorAndRetry   = "inspect_error_and_retry"
	RecoveryActionInspectVerification    = "inspect_recovery_verification"
	RecoverySummaryPassedZeroCommands    = "passed (0 commands)"
	SanitizedBranchPathFallback         = "pr-branch"
	IssueBranchSlugFallback             = "issue"
)

var nonAlphaNumericSlugRE = regexp.MustCompile(`[^a-zA-Z0-9]+`)
var nonBranchPathRuneRE = regexp.MustCompile(`[^a-zA-Z0-9._-]+`)

type BranchName struct {
	Prefix   string `json:"prefix,omitempty"`
	IssueRef string `json:"issue_ref,omitempty"`
	Slug     string `json:"slug,omitempty"`
	FullName string `json:"full_name,omitempty"`
}

func NewIssueBranchName(prefix, issueRef, title, tracker string) BranchName {
	normalizedRef := strings.TrimSpace(issueRef)
	if strings.EqualFold(strings.TrimSpace(tracker), TrackerJira) {
		normalizedRef = strings.ToLower(normalizedRef)
	}
	slug := slugifyBranchTitle(title)
	return BranchName{
		Prefix:   strings.TrimSpace(prefix),
		IssueRef: normalizedRef,
		Slug:     slug,
		FullName: strings.TrimSpace(prefix) + "/" + normalizedRef + "-" + slug,
	}
}

func SanitizeBranchForPath(branchName string) string {
	cleaned := nonBranchPathRuneRE.ReplaceAllString(strings.TrimSpace(branchName), "-")
	cleaned = strings.Trim(cleaned, "-")
	if cleaned == "" {
		return SanitizedBranchPathFallback
	}
	return cleaned
}

type ExpectedGitContext struct {
	Branch   string `json:"branch,omitempty"`
	RepoRoot string `json:"repo_root,omitempty"`
}

func (c ExpectedGitContext) Normalize() ExpectedGitContext {
	branch := strings.TrimSpace(c.Branch)
	repoRoot := strings.TrimSpace(c.RepoRoot)
	if repoRoot != "" {
		repoRoot = filepath.Clean(repoRoot)
	}
	return ExpectedGitContext{Branch: branch, RepoRoot: repoRoot}
}

func (c ExpectedGitContext) Validate(operation, actualBranch, actualRepoRoot string) error {
	expected := c.Normalize()
	actualBranch = strings.TrimSpace(actualBranch)
	actualRepoRoot = strings.TrimSpace(actualRepoRoot)
	if actualRepoRoot != "" {
		actualRepoRoot = filepath.Clean(actualRepoRoot)
	}
	if expected.Branch == "" && expected.RepoRoot == "" {
		return nil
	}
	if expected.Branch != "" && actualBranch != expected.Branch {
		return BranchContextMismatchError{
			Operation:        operation,
			ExpectedBranch:   expected.Branch,
			ActualBranch:     actualBranch,
			ExpectedRepoRoot: firstNonEmpty(expected.RepoRoot, actualRepoRoot),
			ActualRepoRoot:   actualRepoRoot,
		}
	}
	if expected.RepoRoot != "" && actualRepoRoot != expected.RepoRoot {
		return BranchContextMismatchError{
			Operation:        operation,
			ExpectedBranch:   firstNonEmpty(expected.Branch, actualBranch),
			ActualBranch:     actualBranch,
			ExpectedRepoRoot: expected.RepoRoot,
			ActualRepoRoot:   actualRepoRoot,
		}
	}
	return nil
}

type BranchContextMismatchError struct {
	Operation        string
	ExpectedBranch   string
	ActualBranch     string
	ExpectedRepoRoot string
	ActualRepoRoot   string
}

func (e BranchContextMismatchError) Error() string {
	return fmt.Sprintf(
		"Refusing to %s: expected branch '%s' in repo '%s', but current context is branch '%s' in repo '%s'",
		strings.TrimSpace(e.Operation),
		strings.TrimSpace(e.ExpectedBranch),
		strings.TrimSpace(e.ExpectedRepoRoot),
		strings.TrimSpace(e.ActualBranch),
		strings.TrimSpace(e.ActualRepoRoot),
	)
}

type ReusedBranchSyncVerdict struct {
	BranchName        string `json:"branch_name,omitempty"`
	RemoteBaseRef     string `json:"remote_base_ref,omitempty"`
	RequestedStrategy string `json:"requested_strategy,omitempty"`
	AppliedStrategy   string `json:"applied_strategy,omitempty"`
	Status            string `json:"status,omitempty"`
	Changed           bool   `json:"changed,omitempty"`
	AutoResolved      bool   `json:"auto_resolved,omitempty"`
}

func (v ReusedBranchSyncVerdict) Summary(dryRun bool) string {
	prefix := ""
	if dryRun {
		prefix = "[dry-run] "
	}
	branchName := strings.TrimSpace(v.BranchName)
	remoteBaseRef := strings.TrimSpace(v.RemoteBaseRef)
	appliedStrategy := strings.TrimSpace(v.AppliedStrategy)
	switch strings.TrimSpace(v.Status) {
	case BranchSyncStatusAlreadyCurrent:
		return fmt.Sprintf("%sConflict recovery result for branch '%s': already current with '%s'", prefix, branchName, remoteBaseRef)
	case BranchSyncStatusAutoResolved:
		return fmt.Sprintf("%sConflict recovery result for branch '%s': auto-resolved conflicts against '%s' via %s", prefix, branchName, remoteBaseRef, appliedStrategy)
	case BranchSyncStatusSyncedCleanly:
		return fmt.Sprintf("%sConflict recovery result for branch '%s': synced cleanly with '%s' via %s", prefix, branchName, remoteBaseRef, appliedStrategy)
	default:
		status := strings.TrimSpace(v.Status)
		if status == "" {
			status = "unknown"
		}
		return fmt.Sprintf("%sConflict recovery result for branch '%s': status=%s against '%s'", prefix, branchName, status, remoteBaseRef)
	}
}

func (v ReusedBranchSyncVerdict) PushSummary(dryRun bool) string {
	prefix := ""
	if dryRun {
		prefix = "[dry-run] "
	}
	forceWithLease := strings.TrimSpace(v.AppliedStrategy) == "rebase"
	return fmt.Sprintf(
		"%sConflict recovery push result for branch '%s': pushed (force-with-lease: %s)",
		prefix,
		strings.TrimSpace(v.BranchName),
		map[bool]string{true: "yes", false: "no"}[forceWithLease],
	)
}

type RecoveryVerificationFailure struct {
	Scope        string              `json:"scope,omitempty"`
	Verification VerificationVerdict `json:"verification,omitempty"`
}

func (e RecoveryVerificationFailure) Error() string {
	detail := optionalString(e.Verification.Error)
	if detail == "" {
		detail = optionalString(e.Verification.Summary)
	}
	if detail != "" {
		return detail
	}
	scope := strings.TrimSpace(e.Scope)
	if scope == "" {
		scope = "recovery"
	}
	return capitalize(scope) + " recovery verification failed"
}

func SummarizeRecoveryVerificationResults(results []VerificationStep) string {
	if len(results) == 0 {
		return RecoverySummaryPassedZeroCommands
	}
	failedNames := make([]string, 0)
	for _, result := range results {
		if strings.TrimSpace(result.Status) == StatusFailed {
			name := strings.TrimSpace(result.Name)
			if name == "" {
				name = "command"
			}
			failedNames = append(failedNames, name)
		}
	}
	if len(failedNames) > 0 {
		passedCount := len(results) - len(failedNames)
		return fmt.Sprintf("failed (%d/%d passed; failed: %s)", passedCount, len(results), strings.Join(failedNames, ", "))
	}
	return fmt.Sprintf("passed (%d/%d commands)", len(results), len(results))
}

func FailureStateForStage(failureStage string) string {
	switch strings.TrimSpace(failureStage) {
	case FailureStageWorkflowSetup, FailureStageWorkflowHooks, FailureStageWorkflowChecks, FailureStageResidualUntracked, FailureStageTokenBudget, FailureStageBranchContextValidate:
		return StatusBlocked
	default:
		return StatusFailed
	}
}

func RecoveryNextActionForStage(failureStage string) string {
	switch strings.TrimSpace(failureStage) {
	case FailureStageWorkflowSetup:
		return RecoveryActionFixWorkflowSetup
	case FailureStageWorkflowHooks:
		return RecoveryActionFixWorkflowHook
	case FailureStageWorkflowChecks:
		return RecoveryActionFixWorkflowChecks
	case FailureStageMergeExecution:
		return RecoveryActionInspectMergeExecution
	case FailureStageResidualUntracked:
		return RecoveryActionStageResidualFiles
	case FailureStageTokenBudget:
		return RecoveryActionRaiseTokenBudget
	case FailureStageCostBudget:
		return RecoveryActionRaiseCostBudget
	case FailureStageBranchContextValidate:
		return RecoveryActionRestoreBranchContext
	default:
		return RecoveryActionInspectErrorAndRetry
	}
}

func slugifyBranchTitle(text string) string {
	cleaned := nonAlphaNumericSlugRE.ReplaceAllString(strings.ToLower(strings.TrimSpace(text)), "-")
	cleaned = strings.Trim(cleaned, "-")
	if cleaned == "" {
		return IssueBranchSlugFallback
	}
	if len(cleaned) > 40 {
		return cleaned[:40]
	}
	return cleaned
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func capitalize(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	r, size := utf8.DecodeRuneInString(value)
	if r == utf8.RuneError && size == 0 {
		return ""
	}
	return string(unicode.ToUpper(r)) + value[size:]
}
