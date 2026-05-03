package orchestration

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const (
	DaemonClaimStatusClaimed  = "claimed"
	DaemonClaimStatusReleased = "released"
)

type DaemonTaskSnapshot struct {
	IssueNumber          int
	RunID                string
	ForceReprocess       bool
	PRConflictSignal     string
	ReviewFeedbackSignal string
	LatestStateStatus    string
	LatestStateTaskType  string
	LatestClaim          map[string]any
	LatestDecomposition  map[string]any
	OpenDependencyRefs   []string
	LastHandledSignature string
}

type DaemonTaskDecision struct {
	Eligible  bool
	Reason    string
	Signature string
}

func EvaluateDaemonTaskSelection(snapshot DaemonTaskSnapshot, now time.Time) DaemonTaskDecision {
	signature := daemonTaskSignature(snapshot)
	if signature == "" {
		signature = "state:new"
	}

	if len(snapshot.OpenDependencyRefs) > 0 {
		return DaemonTaskDecision{Reason: "blocked by open dependencies: " + formatDependencyRefs(snapshot.OpenDependencyRefs), Signature: signature}
	}
	if snapshot.LastHandledSignature != "" && snapshot.LastHandledSignature == signature {
		return DaemonTaskDecision{Reason: "already handled in this daemon session", Signature: signature}
	}

	claimStatus := normalizePayloadStatus(snapshot.LatestClaim, "status")
	claimRunID := strings.TrimSpace(payloadString(snapshot.LatestClaim, "run_id"))
	if claimStatus == DaemonClaimStatusClaimed {
		if claimRunID == strings.TrimSpace(snapshot.RunID) {
			return DaemonTaskDecision{Reason: "already claimed by this daemon run", Signature: signature}
		}
		if daemonClaimActive(snapshot.LatestClaim, now) {
			return DaemonTaskDecision{Reason: "actively claimed by another daemon worker", Signature: signature}
		}
	}

	stateStatus := strings.ToLower(strings.TrimSpace(snapshot.LatestStateStatus))
	decompositionStatus := normalizePayloadStatus(snapshot.LatestDecomposition, "status")

	if !snapshot.ForceReprocess {
		switch decompositionStatus {
		case "proposed":
			return DaemonTaskDecision{Reason: "waiting for decomposition approval", Signature: signature}
		case "children_created":
			return DaemonTaskDecision{Reason: "parent issue already decomposed into child issues", Signature: signature}
		}

		switch stateStatus {
		case StatusBlocked:
			return DaemonTaskDecision{Reason: "latest issue state is blocked", Signature: signature}
		case StatusWaitingForAuthor:
			if decompositionStatus == "approved" {
				return DaemonTaskDecision{Eligible: true, Signature: signature}
			}
			return DaemonTaskDecision{Reason: "latest issue state is waiting-for-author", Signature: signature}
		}
	}

	return DaemonTaskDecision{Eligible: true, Signature: signature}
}

func formatDependencyRefs(refs []string) string {
	parts := make([]string, 0, len(refs))
	seen := make(map[string]struct{}, len(refs))
	for _, ref := range refs {
		ref = strings.TrimSpace(ref)
		if ref == "" {
			continue
		}
		if !strings.HasPrefix(ref, "#") {
			ref = "#" + ref
		}
		if _, ok := seen[ref]; ok {
			continue
		}
		seen[ref] = struct{}{}
		parts = append(parts, ref)
	}
	return strings.Join(parts, ", ")
}

func BuildDaemonClaimComment(issueNumber int, runID, worker string, claimedAt, expiresAt time.Time) string {
	payload := map[string]any{
		"status":     DaemonClaimStatusClaimed,
		"issue":      issueNumber,
		"run_id":     strings.TrimSpace(runID),
		"worker":     strings.TrimSpace(worker),
		"claimed_at": claimedAt.UTC().Format(time.RFC3339),
		"expires_at": expiresAt.UTC().Format(time.RFC3339),
	}
	return formatDaemonClaimComment("Daemon claim", payload)
}

func BuildDaemonReleaseComment(issueNumber int, runID, worker string, releasedAt time.Time) string {
	payload := map[string]any{
		"status":      DaemonClaimStatusReleased,
		"issue":       issueNumber,
		"run_id":      strings.TrimSpace(runID),
		"worker":      strings.TrimSpace(worker),
		"released_at": releasedAt.UTC().Format(time.RFC3339),
	}
	return formatDaemonClaimComment("Daemon claim release", payload)
}

func ProcessedIssueStatus(raw json.RawMessage) string {
	state, ok := decodeProcessedTrackedState(raw)
	if !ok {
		return ""
	}
	return strings.ToLower(strings.TrimSpace(state.Status))
}

func ProcessedIssueSignature(raw json.RawMessage) string {
	state, ok := decodeProcessedTrackedState(raw)
	if !ok {
		return ""
	}
	return daemonTaskSignature(DaemonTaskSnapshot{
		LatestStateStatus:   state.Status,
		LatestStateTaskType: state.TaskType,
	})
}

func decodeProcessedTrackedState(raw json.RawMessage) (TrackedState, bool) {
	if len(raw) == 0 {
		return TrackedState{}, false
	}
	var payload TrackedState
	if err := json.Unmarshal(raw, &payload); err != nil {
		return TrackedState{}, false
	}
	if strings.TrimSpace(payload.Status) == "" {
		return TrackedState{}, false
	}
	return payload, true
}

func daemonTaskSignature(snapshot DaemonTaskSnapshot) string {
	conflictSignal := strings.TrimSpace(snapshot.PRConflictSignal)
	if conflictSignal != "" {
		return "conflict-recovery:" + conflictSignal
	}
	reviewSignal := strings.TrimSpace(snapshot.ReviewFeedbackSignal)
	if reviewSignal != "" {
		return "review:" + reviewSignal
	}
	decompositionStatus := normalizePayloadStatus(snapshot.LatestDecomposition, "status")
	stateStatus := strings.ToLower(strings.TrimSpace(snapshot.LatestStateStatus))
	stateTaskType := strings.ToLower(strings.TrimSpace(snapshot.LatestStateTaskType))
	if decompositionStatus == "proposed" || decompositionStatus == "approved" || decompositionStatus == "children_created" {
		return "decomposition:" + decompositionStatus
	}
	if stateStatus != "" {
		if stateTaskType != "" {
			return "state:" + stateTaskType + ":" + stateStatus
		}
		return "state:" + stateStatus
	}
	return "state:new"
}

func daemonClaimActive(payload map[string]any, now time.Time) bool {
	if normalizePayloadStatus(payload, "status") != DaemonClaimStatusClaimed {
		return false
	}
	expiresAt, ok := payloadTime(payload, "expires_at")
	if !ok {
		return true
	}
	return expiresAt.After(now)
}

func payloadString(payload map[string]any, key string) string {
	if payload == nil {
		return ""
	}
	value, _ := payload[key].(string)
	return strings.TrimSpace(value)
}

func payloadTime(payload map[string]any, key string) (time.Time, bool) {
	raw := payloadString(payload, key)
	if raw == "" {
		return time.Time{}, false
	}
	parsed, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, false
	}
	return parsed, true
}

func formatDaemonClaimComment(title string, payload map[string]any) string {
	encoded, _ := json.Marshal(payload)
	return fmt.Sprintf("## %s\n\n%s\n```json\n%s\n```", title, OrchestrationClaimMarker, string(encoded))
}
