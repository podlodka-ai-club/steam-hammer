package orchestration

import "strings"

const (
	StatusInProgress       = "in-progress"
	StatusReadyForReview   = "ready-for-review"
	StatusFailed           = "failed"
	StatusBlocked          = "blocked"
	StatusWaitingForAuthor = "waiting-for-author"
	StatusWaitingForCI     = "waiting-for-ci"
	StatusReadyToMerge     = "ready-to-merge"
)

const (
	MergeReadinessConflicting = "conflicting"
	MergeReadinessStale       = "stale"
	MergeReadinessClean       = "clean"
	MergeReadinessUnknown     = "unknown"
)

const (
	ReviewDecisionApproved         = "APPROVED"
	ReviewDecisionChangesRequested = "CHANGES_REQUESTED"
	ReviewDecisionReviewRequired   = "REVIEW_REQUIRED"
	ReviewDecisionUnknown          = "UNKNOWN"
)

// TrackedState matches the stable top-level issue/PR orchestration state shape
// that is persisted in tracker comments today. Optional nested payloads stay
// additive so Go can consume the Python-produced state without changing it.
type TrackedState struct {
	Status                 string             `json:"status,omitempty"`
	TaskType               string             `json:"task_type,omitempty"`
	Issue                  *int               `json:"issue,omitempty"`
	PR                     *int               `json:"pr,omitempty"`
	Branch                 string             `json:"branch,omitempty"`
	BaseBranch             string             `json:"base_branch,omitempty"`
	Runner                 string             `json:"runner,omitempty"`
	Agent                  string             `json:"agent,omitempty"`
	Model                  string             `json:"model,omitempty"`
	Attempt                int                `json:"attempt,omitempty"`
	Stage                  string             `json:"stage,omitempty"`
	NextAction             string             `json:"next_action,omitempty"`
	Error                  string             `json:"error,omitempty"`
	Timestamp              string             `json:"timestamp,omitempty"`
	WorkflowChecks         []VerificationStep `json:"workflow_checks,omitempty"`
	MergeReadiness         *PRMergeReadiness  `json:"merge_readiness,omitempty"`
	RequiredFileValidation map[string]any     `json:"required_file_validation,omitempty"`
	MergePolicy            map[string]any     `json:"merge_policy,omitempty"`
	CIChecks               []map[string]any   `json:"ci_checks,omitempty"`
	CIDiagnostics          map[string]any     `json:"ci_diagnostics,omitempty"`
	ResidualUntrackedFiles []string           `json:"residual_untracked_files,omitempty"`
	ResidualUntrackedCount int                `json:"residual_untracked_count,omitempty"`
	Stats                  map[string]any     `json:"stats,omitempty"`
	Decomposition          map[string]any     `json:"decomposition,omitempty"`
}

type VerificationVerdict struct {
	Status        string             `json:"status,omitempty"`
	Summary       string             `json:"summary,omitempty"`
	NextAction    string             `json:"next_action,omitempty"`
	Commands      []VerificationStep `json:"commands,omitempty"`
	FollowUpIssue *FollowUpIssue     `json:"follow_up_issue,omitempty"`
}

type VerificationStep struct {
	Name          string `json:"name,omitempty"`
	Command       string `json:"command,omitempty"`
	Status        string `json:"status,omitempty"`
	ExitCode      *int   `json:"exit_code,omitempty"`
	StdoutExcerpt string `json:"stdout_excerpt,omitempty"`
	StderrExcerpt string `json:"stderr_excerpt,omitempty"`
}

type FollowUpIssue struct {
	Status      string `json:"status,omitempty"`
	Title       string `json:"title,omitempty"`
	Body        string `json:"body,omitempty"`
	IssueNumber *int   `json:"issue_number,omitempty"`
	IssueURL    string `json:"issue_url,omitempty"`
}

// Compatibility aliases keep the current session checkpoint consumer stable
// while the reusable verification verdict model moves into the shared domain.
type VerificationResult = VerificationVerdict
type VerificationCommand = VerificationStep

type PullRequestFacts struct {
	MergeStateStatus string
	Mergeable        string
	ReviewDecision   string
	IsDraft          bool
}

type MergePolicy struct {
	Auto   bool   `json:"auto,omitempty"`
	Method string `json:"method,omitempty"`
}

type PRMergeReadiness struct {
	MergeStateStatus        string               `json:"merge_state_status,omitempty"`
	Mergeable               string               `json:"mergeable,omitempty"`
	MergeReadinessState     string               `json:"merge_readiness_state,omitempty"`
	ReviewDecision          string               `json:"review_decision,omitempty"`
	IsDraft                 bool                 `json:"is_draft,omitempty"`
	AutoMergeEnabled        bool                 `json:"auto_merge_enabled,omitempty"`
	MergeMethod             string               `json:"merge_method,omitempty"`
	Status                  string               `json:"status,omitempty"`
	Stage                   string               `json:"stage,omitempty"`
	NextAction              string               `json:"next_action,omitempty"`
	Error                   string               `json:"error,omitempty"`
	MergeResultVerification *VerificationVerdict `json:"merge_result_verification,omitempty"`
}

func EvaluatePRMergeReadiness(facts PullRequestFacts, policy MergePolicy, verification *VerificationVerdict) PRMergeReadiness {
	mergeState := strings.TrimSpace(strings.ToUpper(facts.MergeStateStatus))
	if mergeState == "" {
		mergeState = "UNKNOWN"
	}
	mergeable := strings.TrimSpace(strings.ToUpper(facts.Mergeable))
	if mergeable == "" {
		mergeable = "UNKNOWN"
	}
	reviewDecision := strings.TrimSpace(strings.ToUpper(facts.ReviewDecision))
	if reviewDecision == "" {
		reviewDecision = ReviewDecisionUnknown
	}
	mergeMethod := strings.TrimSpace(policy.Method)
	if mergeMethod == "" {
		mergeMethod = "squash"
	}

	readiness := PRMergeReadiness{
		MergeStateStatus:    mergeState,
		Mergeable:           mergeable,
		MergeReadinessState: ClassifyPRMergeReadinessState(mergeState, mergeable),
		ReviewDecision:      reviewDecision,
		IsDraft:             facts.IsDraft,
		AutoMergeEnabled:    policy.Auto,
		MergeMethod:         mergeMethod,
		Status:              StatusReadyToMerge,
		Stage:               "merge_gate",
		NextAction:          "ready_for_merge",
	}
	if verification != nil {
		copy := *verification
		readiness.MergeResultVerification = &copy
	}

	if readiness.IsDraft {
		readiness.Status = StatusWaitingForAuthor
		readiness.NextAction = "mark_pr_ready_for_review"
		readiness.Error = "PR is still marked as draft"
		return readiness
	}

	switch readiness.MergeReadinessState {
	case MergeReadinessConflicting:
		readiness.Status = StatusBlocked
		readiness.NextAction = "resolve_merge_conflicts"
		readiness.Error = "PR is not mergeable yet (mergeStateStatus=" + mergeState + ")"
		return readiness
	case MergeReadinessStale:
		readiness.Status = StatusBlocked
		readiness.NextAction = "sync_pr_with_base"
		readiness.Error = "PR branch is stale and must be synced with base (mergeStateStatus=" + mergeState + ")"
		return readiness
	}

	switch reviewDecision {
	case ReviewDecisionChangesRequested:
		readiness.Status = StatusWaitingForAuthor
		readiness.NextAction = "address_requested_changes"
		readiness.Error = "Review state still has requested changes"
		return readiness
	case ReviewDecisionReviewRequired:
		readiness.Status = StatusWaitingForAuthor
		readiness.NextAction = "await_required_approval"
		readiness.Error = "Required approving review is still missing"
		return readiness
	}

	if readiness.MergeReadinessState == MergeReadinessUnknown {
		readiness.Status = StatusBlocked
		readiness.NextAction = "inspect_merge_requirements"
		readiness.Error = "GitHub has not marked this PR mergeable yet (mergeStateStatus=" + mergeState + ")"
		return readiness
	}

	if verification != nil && strings.EqualFold(strings.TrimSpace(verification.Status), StatusFailed) {
		readiness.Status = StatusBlocked
		readiness.NextAction = "inspect_merge_result_verification"
		readiness.Error = optionalString(verification.Summary)
		if readiness.Error == "" {
			readiness.Error = "Merge-result verification failed"
		}
		return readiness
	}

	return readiness
}

func ClassifyPRMergeReadinessState(mergeState, mergeable string) string {
	normalizedMergeState := strings.TrimSpace(strings.ToUpper(mergeState))
	if normalizedMergeState == "" {
		normalizedMergeState = "UNKNOWN"
	}
	normalizedMergeable := strings.TrimSpace(strings.ToUpper(mergeable))
	if normalizedMergeable == "" {
		normalizedMergeable = "UNKNOWN"
	}
	if normalizedMergeable == "CONFLICTING" || normalizedMergeState == "DIRTY" || normalizedMergeState == "CONFLICTING" {
		return MergeReadinessConflicting
	}
	if normalizedMergeState == "BEHIND" {
		return MergeReadinessStale
	}
	if normalizedMergeable == "MERGEABLE" {
		return MergeReadinessClean
	}
	return MergeReadinessUnknown
}

func (v VerificationVerdict) summaryLine() string {
	summary := optionalString(v.Summary)
	if summary == "" {
		summary = optionalString(v.Status)
		if summary == "" {
			summary = "unknown"
		}
	}
	line := "Verification: " + summary
	if v.FollowUpIssue == nil {
		return line
	}
	status := optionalString(v.FollowUpIssue.Status)
	if status == "created" && v.FollowUpIssue.IssueNumber != nil {
		return line + "; follow-up issue #" + itoa(*v.FollowUpIssue.IssueNumber) + " created"
	}
	if status != "" {
		return line + "; follow-up=" + status
	}
	return line
}
