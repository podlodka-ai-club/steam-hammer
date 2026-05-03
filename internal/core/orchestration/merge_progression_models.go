package orchestration

import (
	"path/filepath"
	"sort"
	"strings"
)

const (
	MergeVerificationReasonNoChangedFiles     = "no-changed-files"
	MergeVerificationReasonDocsOnly           = "docs-only"
	MergeVerificationReasonCentralRunnerFiles = "central-runner-files"
	MergeVerificationReasonOverlappingOpenPRs = "overlapping-open-prs"
	MergeVerificationReasonNonOverlapping     = "non-overlapping"
	MergeQueueActionAwaitTurn                 = "await_merge_queue_turn"
	MergeQueueActionExecuteVerifiedMerge      = "execute_verified_merge"
	MergeQueueActionAwaitAutoMerge            = "await_github_auto_merge"
	MergeQueueActionManualMerge               = "merge_manually_or_enable_auto_merge"
	ReviewFeedbackActionAddressRemaining      = "address_remaining_review_feedback"
)

var centralRunnerPathPrefixes = []string{
	"scripts/",
	"cmd/orchestrator/",
	"internal/cli/",
	"internal/core/",
}

var docsOnlyPathPrefixes = []string{
	"docs/",
	"retro/",
}

var docsOnlyFileExtensions = map[string]struct{}{
	".md":  {},
	".rst": {},
	".txt": {},
}

type PullRequestFileChange struct {
	Path string `json:"path,omitempty"`
}

type OverlappingPullRequest struct {
	Number  int      `json:"number,omitempty"`
	HeadRef string   `json:"head_ref,omitempty"`
	Files   []string `json:"files,omitempty"`
}

type MergeResultVerificationNeed struct {
	Required       bool                     `json:"required,omitempty"`
	Reason         string                   `json:"reason,omitempty"`
	Summary        string                   `json:"summary,omitempty"`
	ChangedFiles   []string                 `json:"changed_files,omitempty"`
	OverlappingPRs []OverlappingPullRequest `json:"overlapping_prs,omitempty"`
}

type ReviewFeedbackLoopDecision struct {
	Continue    bool
	Status      string
	Stage       string
	NextAction  string
	Attempt     int
	MaxAttempts int
	Actionable  int
	Summary     string
}

type SafeMergeExecutionDecision struct {
	Execute    bool
	Queued     bool
	NextAction string
	Reason     string
}

type MergeAttemptResult struct {
	Accepted bool
	Status   string
	Error    string
}

type PolicyDrivenMergeQueueDecision struct {
	Status         string
	Stage          string
	Execute        bool
	Queued         bool
	NextAction     string
	Reason         string
	MergeAttempted bool
}

type OpenPullRequestCandidate struct {
	Number       int
	HeadRefName  string
	BaseRefName  string
	ChangedFiles []string
}

func PullRequestChangedPaths(files []PullRequestFileChange) []string {
	changedPaths := make([]string, 0, len(files))
	seen := make(map[string]struct{}, len(files))
	for _, item := range files {
		path := strings.TrimSpace(item.Path)
		if path == "" {
			continue
		}
		if _, ok := seen[path]; ok {
			continue
		}
		seen[path] = struct{}{}
		changedPaths = append(changedPaths, path)
	}
	return changedPaths
}

func IsDocsOnlyPath(path string) bool {
	normalized := strings.ToLower(strings.TrimSpace(path))
	if normalized == "" {
		return false
	}
	for _, prefix := range docsOnlyPathPrefixes {
		if strings.HasPrefix(normalized, prefix) {
			return true
		}
	}
	_, extension := filepath.Ext(normalized), filepath.Ext(normalized)
	_, ok := docsOnlyFileExtensions[extension]
	return ok
}

func TouchesCentralRunnerFiles(changedPaths []string) bool {
	for _, path := range changedPaths {
		for _, prefix := range centralRunnerPathPrefixes {
			if strings.HasPrefix(path, prefix) {
				return true
			}
		}
	}
	return false
}

func DetermineMergeResultVerificationNeed(prNumber int, baseBranch string, changedPaths []string, openPRs []OpenPullRequestCandidate) MergeResultVerificationNeed {
	changedPaths = append([]string(nil), changedPaths...)
	if len(changedPaths) == 0 {
		return MergeResultVerificationNeed{
			Required:       false,
			Reason:         MergeVerificationReasonNoChangedFiles,
			Summary:        "skipped (no changed files reported)",
			ChangedFiles:   []string{},
			OverlappingPRs: []OverlappingPullRequest{},
		}
	}

	allDocsOnly := true
	for _, path := range changedPaths {
		if !IsDocsOnlyPath(path) {
			allDocsOnly = false
			break
		}
	}
	if allDocsOnly {
		return MergeResultVerificationNeed{
			Required:       false,
			Reason:         MergeVerificationReasonDocsOnly,
			Summary:        "skipped (docs-only PR)",
			ChangedFiles:   changedPaths,
			OverlappingPRs: []OverlappingPullRequest{},
		}
	}

	if TouchesCentralRunnerFiles(changedPaths) {
		return MergeResultVerificationNeed{
			Required:       true,
			Reason:         MergeVerificationReasonCentralRunnerFiles,
			Summary:        "required (touches central runner files)",
			ChangedFiles:   changedPaths,
			OverlappingPRs: []OverlappingPullRequest{},
		}
	}

	currentPaths := make(map[string]struct{}, len(changedPaths))
	for _, path := range changedPaths {
		currentPaths[path] = struct{}{}
	}

	overlaps := make([]OverlappingPullRequest, 0)
	for _, candidate := range openPRs {
		if candidate.Number == prNumber || candidate.Number <= 0 {
			continue
		}
		if strings.TrimSpace(baseBranch) != "" && strings.TrimSpace(candidate.BaseRefName) != strings.TrimSpace(baseBranch) {
			continue
		}
		overlapFiles := make([]string, 0)
		seen := map[string]struct{}{}
		for _, path := range candidate.ChangedFiles {
			path = strings.TrimSpace(path)
			if path == "" {
				continue
			}
			if _, ok := currentPaths[path]; !ok {
				continue
			}
			if _, ok := seen[path]; ok {
				continue
			}
			seen[path] = struct{}{}
			overlapFiles = append(overlapFiles, path)
		}
		if len(overlapFiles) == 0 {
			continue
		}
		sort.Strings(overlapFiles)
		overlaps = append(overlaps, OverlappingPullRequest{
			Number:  candidate.Number,
			HeadRef: strings.TrimSpace(candidate.HeadRefName),
			Files:   overlapFiles,
		})
	}

	if len(overlaps) > 0 {
		numbers := make([]string, 0, len(overlaps))
		for _, overlap := range overlaps {
			numbers = append(numbers, "#"+itoa(overlap.Number))
		}
		return MergeResultVerificationNeed{
			Required:       true,
			Reason:         MergeVerificationReasonOverlappingOpenPRs,
			Summary:        "required (overlaps with open PRs: " + strings.Join(numbers, ", ") + ")",
			ChangedFiles:   changedPaths,
			OverlappingPRs: overlaps,
		}
	}

	return MergeResultVerificationNeed{
		Required:       false,
		Reason:         MergeVerificationReasonNonOverlapping,
		Summary:        "skipped (no overlap and no central runner files)",
		ChangedFiles:   changedPaths,
		OverlappingPRs: []OverlappingPullRequest{},
	}
}

func EvaluateReviewFeedbackLoop(actionableCount, attempt, maxAttempts int) ReviewFeedbackLoopDecision {
	if maxAttempts <= 0 {
		maxAttempts = 1
	}
	if attempt <= 0 {
		attempt = 1
	}
	if actionableCount <= 0 {
		return ReviewFeedbackLoopDecision{
			Continue:    false,
			Status:      StatusWaitingForCI,
			Stage:       "changes_pushed",
			NextAction:  NextActionWaitForCI,
			Attempt:     attempt,
			MaxAttempts: maxAttempts,
			Actionable:  0,
			Summary:     "no remaining actionable review items",
		}
	}
	if attempt < maxAttempts {
		return ReviewFeedbackLoopDecision{
			Continue:    true,
			Status:      StatusInProgress,
			Stage:       "review_feedback",
			NextAction:  ReviewFeedbackActionAddressRemaining,
			Attempt:     attempt,
			MaxAttempts: maxAttempts,
			Actionable:  actionableCount,
			Summary:     "actionable review feedback remains",
		}
	}
	return ReviewFeedbackLoopDecision{
		Continue:    false,
		Status:      StatusBlocked,
		Stage:       "review_feedback",
		NextAction:  ReviewFeedbackActionAddressRemaining,
		Attempt:     attempt,
		MaxAttempts: maxAttempts,
		Actionable:  actionableCount,
		Summary:     "review feedback persisted past retry limit",
	}
}

func EvaluateSafeMergeExecution(readiness PRMergeReadiness, mergeInFlight bool) SafeMergeExecutionDecision {
	if readiness.Status != StatusReadyToMerge {
		nextAction := optionalString(readiness.NextAction)
		if nextAction == "" {
			nextAction = "inspect_merge_requirements"
		}
		return SafeMergeExecutionDecision{
			Execute:    false,
			Queued:     false,
			NextAction: nextAction,
			Reason:     optionalString(readiness.Error),
		}
	}
	if mergeInFlight {
		return SafeMergeExecutionDecision{
			Execute:    false,
			Queued:     true,
			NextAction: MergeQueueActionAwaitTurn,
			Reason:     "another merge is already in flight",
		}
	}
	return SafeMergeExecutionDecision{
		Execute:    true,
		Queued:     false,
		NextAction: MergeQueueActionExecuteVerifiedMerge,
		Reason:     "merge gate passed and queue is clear",
	}
}

func EvaluatePolicyDrivenMergeQueue(readiness PRMergeReadiness, mergeInFlight bool, autoMergeEnabled bool, attempt *MergeAttemptResult) PolicyDrivenMergeQueueDecision {
	base := EvaluateSafeMergeExecution(readiness, mergeInFlight)
	if !base.Execute {
		status := strings.TrimSpace(readiness.Status)
		if status == "" {
			status = StatusBlocked
		}
		stage := "merge_gate"
		if base.Queued {
			status = StatusReadyToMerge
			stage = "merge_queue"
		}
		return PolicyDrivenMergeQueueDecision{
			Status:     status,
			Stage:      stage,
			Execute:    false,
			Queued:     base.Queued,
			NextAction: base.NextAction,
			Reason:     base.Reason,
		}
	}

	if !autoMergeEnabled {
		return PolicyDrivenMergeQueueDecision{
			Status:     StatusReadyToMerge,
			Stage:      "merge_gate",
			Execute:    false,
			Queued:     false,
			NextAction: "ready_for_merge",
			Reason:     "merge gate passed and auto-merge is disabled",
		}
	}

	if attempt == nil {
		return PolicyDrivenMergeQueueDecision{
			Status:         StatusReadyToMerge,
			Stage:          "merge_execution",
			Execute:        true,
			Queued:         false,
			NextAction:     MergeQueueActionExecuteVerifiedMerge,
			Reason:         "merge gate passed and policy allows autonomous merge",
			MergeAttempted: false,
		}
	}

	attemptStatus := strings.ToLower(strings.TrimSpace(attempt.Status))
	if attempt.Accepted {
		return PolicyDrivenMergeQueueDecision{
			Status:         StatusReadyToMerge,
			Stage:          "merge_execution",
			Execute:        false,
			Queued:         false,
			NextAction:     MergeQueueActionAwaitAutoMerge,
			Reason:         optionalString(attempt.Error),
			MergeAttempted: true,
		}
	}

	nextAction := "inspect_merge_requirements"
	status := StatusBlocked
	if attemptStatus == StatusReadyToMerge {
		nextAction = MergeQueueActionManualMerge
		status = StatusWaitingForAuthor
	}
	if override := optionalString(attempt.Error); override != "" {
		return PolicyDrivenMergeQueueDecision{
			Status:         status,
			Stage:          "merge_gate",
			Execute:        false,
			Queued:         false,
			NextAction:     nextAction,
			Reason:         override,
			MergeAttempted: true,
		}
	}
	return PolicyDrivenMergeQueueDecision{
		Status:         status,
		Stage:          "merge_gate",
		Execute:        false,
		Queued:         false,
		NextAction:     nextAction,
		Reason:         "merge request was not accepted by policy or code host",
		MergeAttempted: true,
	}
}
