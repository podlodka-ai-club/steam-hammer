package orchestration

import (
	"sort"
	"strings"
)

const (
	NextActionInspectFailingCIChecks      = "inspect_failing_ci_checks"
	NextActionWaitForCI                   = "wait_for_ci"
	NextActionUpdatePRWithRequiredFiles   = "update_pr_with_required_files"
	NextActionResolveMergeabilityBlockers = "resolve_mergeability_blockers"
	NextActionWaitForReview               = "wait_for_review"
)

var mergeableReadyStates = map[string]struct{}{
	"CLEAN":     {},
	"HAS_HOOKS": {},
	"UNSTABLE":  {},
}

type PRReadinessPolicy struct {
	RequireGreenChecks       bool
	RequiredChecks           []string
	SkipRequiredFileEvidence bool
	RequireMergeable         bool
	RequiredApprovals        int
	RequireReview            bool
	ReviewPendingAction      string
}

type PRReadinessFacts struct {
	MergeStateStatus string
	CIOverall        string
	CIChecks         []PRCICheck
	ApprovedCount    int
}

type PRCICheck struct {
	Name       string `json:"name,omitempty"`
	State      string `json:"state,omitempty"`
	URL        string `json:"url,omitempty"`
	Conclusion string `json:"conclusion,omitempty"`
}

type PRRequiredFileValidation struct {
	Status            string   `json:"status,omitempty"`
	RequiredFileCount int      `json:"required_file_count,omitempty"`
	RequiredFiles     []string `json:"required_files,omitempty"`
	MatchedFiles      []string `json:"matched_files,omitempty"`
	MissingFiles      []string `json:"missing_files,omitempty"`
	ChangedFileCount  int      `json:"changed_file_count,omitempty"`
}

type PRReadiness struct {
	Status                string      `json:"status,omitempty"`
	NextAction            string      `json:"next_action,omitempty"`
	Error                 string      `json:"error,omitempty"`
	ApprovedCount         int         `json:"approved_count,omitempty"`
	RequiredApprovals     int         `json:"required_approvals,omitempty"`
	MissingRequiredChecks []string    `json:"missing_required_checks,omitempty"`
	PendingRequiredChecks []string    `json:"pending_required_checks,omitempty"`
	FailingChecks         []PRCICheck `json:"failing_checks,omitempty"`
}

type PullRequestReview struct {
	State       string
	SubmittedAt string
	AuthorLogin string
}

type PRApprovalSummary struct {
	ApprovedCount      int
	ApprovedBy         []string
	LatestReviewStates map[string]string
}

func EvaluatePRReadiness(facts PRReadinessFacts, policy PRReadinessPolicy, requiredFileValidation PRRequiredFileValidation) PRReadiness {
	ciChecks := facts.CIChecks
	ciOverall := strings.TrimSpace(strings.ToLower(facts.CIOverall))
	ciChecksByName := make(map[string]PRCICheck, len(ciChecks))
	for _, check := range ciChecks {
		key := normalizeCheckName(check.Name)
		if key == "" {
			continue
		}
		ciChecksByName[key] = check
	}

	if policy.RequireGreenChecks {
		failingChecks := checksWithState(ciChecks, "failure")
		pendingChecks := checksWithState(ciChecks, "pending")
		if ciOverall == "failure" || len(failingChecks) > 0 {
			return PRReadiness{
				Status:        StatusBlocked,
				NextAction:    NextActionInspectFailingCIChecks,
				Error:         FormatFailingCIChecksSummary(failingChecks, 5),
				FailingChecks: failingChecks,
			}
		}
		if ciOverall == "pending" || len(pendingChecks) > 0 {
			return PRReadiness{
				Status:     StatusWaitingForCI,
				NextAction: NextActionWaitForCI,
				Error:      "Waiting for CI checks to finish",
			}
		}
		if len(ciChecks) == 0 {
			return PRReadiness{
				Status:     StatusWaitingForCI,
				NextAction: NextActionWaitForCI,
				Error:      "Waiting for CI checks to start",
			}
		}
	}

	matchedRequiredChecks := make([]PRCICheck, 0, len(policy.RequiredChecks))
	missingRequiredChecks := make([]string, 0)
	for _, requiredName := range policy.RequiredChecks {
		matched, ok := ciChecksByName[normalizeCheckName(requiredName)]
		if !ok {
			missingRequiredChecks = append(missingRequiredChecks, requiredName)
			continue
		}
		matchedRequiredChecks = append(matchedRequiredChecks, matched)
	}

	failingRequiredChecks := checksWithState(matchedRequiredChecks, "failure")
	if len(failingRequiredChecks) > 0 {
		return PRReadiness{
			Status:        StatusBlocked,
			NextAction:    NextActionInspectFailingCIChecks,
			Error:         FormatFailingCIChecksSummary(failingRequiredChecks, 5),
			FailingChecks: failingRequiredChecks,
		}
	}

	pendingRequiredChecks := checksWithState(matchedRequiredChecks, "pending")
	if len(missingRequiredChecks) > 0 || len(pendingRequiredChecks) > 0 {
		waitingParts := make([]string, 0, 2)
		if len(missingRequiredChecks) > 0 {
			waitingParts = append(waitingParts, "missing required checks: "+strings.Join(missingRequiredChecks, ", "))
		}
		if len(pendingRequiredChecks) > 0 {
			pendingNames := make([]string, 0, len(pendingRequiredChecks))
			for _, check := range pendingRequiredChecks {
				name := strings.TrimSpace(check.Name)
				if name == "" {
					name = "unknown-check"
				}
				pendingNames = append(pendingNames, name)
			}
			waitingParts = append(waitingParts, "pending required checks: "+strings.Join(pendingNames, ", "))
		}
		return PRReadiness{
			Status:                StatusWaitingForCI,
			NextAction:            NextActionWaitForCI,
			Error:                 strings.Join(waitingParts, "; "),
			MissingRequiredChecks: append([]string(nil), missingRequiredChecks...),
			PendingRequiredChecks: checkNames(pendingRequiredChecks),
		}
	}

	if !policy.SkipRequiredFileEvidence && strings.EqualFold(strings.TrimSpace(requiredFileValidation.Status), StatusBlocked) {
		missingFiles := append([]string(nil), requiredFileValidation.MissingFiles...)
		sort.Strings(missingFiles)
		return PRReadiness{
			Status:     StatusBlocked,
			NextAction: NextActionUpdatePRWithRequiredFiles,
			Error:      "Missing required file evidence: " + strings.Join(missingFiles, ", "),
		}
	}

	if policy.RequireMergeable {
		mergeState := strings.TrimSpace(strings.ToUpper(facts.MergeStateStatus))
		if mergeState != "" {
			if _, ok := mergeableReadyStates[mergeState]; !ok {
				return PRReadiness{
					Status:     StatusBlocked,
					NextAction: NextActionResolveMergeabilityBlockers,
					Error:      "PR merge state is not ready: " + mergeState,
				}
			}
		}
	}

	requiredApprovals := policy.RequiredApprovals
	if policy.RequireReview && requiredApprovals < 1 {
		requiredApprovals = 1
	}
	if requiredApprovals > facts.ApprovedCount {
		return PRReadiness{
			Status:            StatusReadyForReview,
			NextAction:        reviewPendingAction(policy.ReviewPendingAction),
			Error:             "Waiting for required approvals: " + itoa(facts.ApprovedCount) + "/" + itoa(requiredApprovals),
			ApprovedCount:     facts.ApprovedCount,
			RequiredApprovals: requiredApprovals,
		}
	}

	return PRReadiness{
		Status:            StatusReadyToMerge,
		NextAction:        "ready_for_merge",
		ApprovedCount:     facts.ApprovedCount,
		RequiredApprovals: requiredApprovals,
	}
}

func CountApprovingReviews(reviews []PullRequestReview, prAuthorLogin string) PRApprovalSummary {
	prAuthorLogin = strings.TrimSpace(strings.ToLower(prAuthorLogin))
	sortedReviews := append([]PullRequestReview(nil), reviews...)
	sort.SliceStable(sortedReviews, func(i, j int) bool {
		return sortedReviews[i].SubmittedAt < sortedReviews[j].SubmittedAt
	})

	latestByAuthor := make(map[string]string, len(sortedReviews))
	for _, review := range sortedReviews {
		authorLogin := strings.TrimSpace(strings.ToLower(review.AuthorLogin))
		if authorLogin == "" || authorLogin == prAuthorLogin {
			continue
		}
		latestByAuthor[authorLogin] = strings.TrimSpace(strings.ToUpper(review.State))
	}

	approvedBy := make([]string, 0, len(latestByAuthor))
	latestStates := make(map[string]string, len(latestByAuthor))
	for authorLogin, state := range latestByAuthor {
		latestStates[authorLogin] = state
		if state == "APPROVED" {
			approvedBy = append(approvedBy, authorLogin)
		}
	}
	sort.Strings(approvedBy)

	return PRApprovalSummary{
		ApprovedCount:      len(approvedBy),
		ApprovedBy:         approvedBy,
		LatestReviewStates: latestStates,
	}
}

func FormatFailingCIChecksSummary(failingChecks []PRCICheck, maxItems int) string {
	if len(failingChecks) == 0 {
		return "No failing CI checks reported"
	}
	if maxItems <= 0 {
		maxItems = 5
	}

	renderedItems := make([]string, 0, minInt(len(failingChecks), maxItems))
	for _, check := range failingChecks[:minInt(len(failingChecks), maxItems)] {
		name := strings.TrimSpace(check.Name)
		if name == "" {
			name = "unknown-check"
		}
		url := strings.TrimSpace(check.URL)
		if url != "" {
			renderedItems = append(renderedItems, name+" ("+url+")")
			continue
		}
		renderedItems = append(renderedItems, name)
	}

	remaining := len(failingChecks) - len(renderedItems)
	if remaining > 0 {
		renderedItems = append(renderedItems, "and "+itoa(remaining)+" more")
	}

	return "CI failing checks: " + strings.Join(renderedItems, "; ")
}

func IsAutonomousReadyStatus(status string) bool {
	switch strings.TrimSpace(strings.ToLower(status)) {
	case StatusReadyForReview, StatusWaitingForCI, StatusReadyToMerge:
		return true
	default:
		return false
	}
}

func reviewPendingAction(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return NextActionWaitForReview
	}
	return value
}

func normalizeCheckName(value string) string {
	return strings.TrimSpace(strings.ToLower(value))
}

func checksWithState(checks []PRCICheck, state string) []PRCICheck {
	state = strings.TrimSpace(strings.ToLower(state))
	filtered := make([]PRCICheck, 0)
	for _, check := range checks {
		if strings.TrimSpace(strings.ToLower(check.State)) == state {
			filtered = append(filtered, check)
		}
	}
	return filtered
}

func checkNames(checks []PRCICheck) []string {
	if len(checks) == 0 {
		return nil
	}
	names := make([]string, 0, len(checks))
	for _, check := range checks {
		name := strings.TrimSpace(check.Name)
		if name == "" {
			name = "unknown-check"
		}
		names = append(names, name)
	}
	return names
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
