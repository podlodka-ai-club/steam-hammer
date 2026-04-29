package orchestration

import (
	"encoding/json"
	"os"
	"strconv"
	"strings"
)

type State struct {
	ProcessedIssues map[string]json.RawMessage `json:"processed_issues"`
	Checkpoint      *Checkpoint                `json:"checkpoint,omitempty"`
}

type Checkpoint struct {
	RunID          string               `json:"run_id,omitempty"`
	Phase          string               `json:"phase,omitempty"`
	BatchIndex     int                  `json:"batch_index,omitempty"`
	TotalBatches   int                  `json:"total_batches,omitempty"`
	Counts         Counts               `json:"counts,omitempty"`
	Done           []string             `json:"done,omitempty"`
	Current        string               `json:"current,omitempty"`
	Next           []string             `json:"next,omitempty"`
	IssuePRActions []string             `json:"issue_pr_actions,omitempty"`
	InProgress     []string             `json:"in_progress,omitempty"`
	Blockers       []string             `json:"blockers,omitempty"`
	NextCheckpoint string               `json:"next_checkpoint,omitempty"`
	UpdatedAt      string               `json:"updated_at,omitempty"`
	Verification   *VerificationVerdict `json:"verification,omitempty"`
}

type Counts struct {
	Processed                  int `json:"processed,omitempty"`
	Failures                   int `json:"failures,omitempty"`
	SkippedExistingPR          int `json:"skipped_existing_pr,omitempty"`
	SkippedExistingBranch      int `json:"skipped_existing_branch,omitempty"`
	SkippedBlockedDependencies int `json:"skipped_blocked_dependencies,omitempty"`
	SkippedOutOfScope          int `json:"skipped_out_of_scope,omitempty"`
}

func LoadState(path string) (State, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return State{}, err
	}
	return ParseState(data)
}

func ParseState(data []byte) (State, error) {
	var state State
	if err := json.Unmarshal(data, &state); err != nil {
		return State{}, err
	}
	if state.ProcessedIssues == nil {
		state.ProcessedIssues = map[string]json.RawMessage{}
	}
	return state, nil
}

func (s State) Summary() string {
	if s.Checkpoint == nil {
		return strings.Join([]string{
			"Autonomous session status",
			"Done: processed issues recorded=" + itoa(len(s.ProcessedIssues)),
			"Current: no active checkpoint has been recorded yet",
			"Next: start or resume the autonomous batch loop",
			"Issue/PR actions: none",
			"In progress: none",
			"Blockers: none",
			"Next checkpoint: when the first batch starts",
		}, "\n")
	}

	checkpoint := s.Checkpoint
	phase := optionalString(checkpoint.Phase)
	if phase == "" {
		phase = "running"
	}
	updatedAt := optionalString(checkpoint.UpdatedAt)
	if updatedAt == "" {
		updatedAt = "unknown"
	}
	current := optionalString(checkpoint.Current)
	if current == "" {
		current = "idle"
	}
	nextCheckpoint := optionalString(checkpoint.NextCheckpoint)
	if nextCheckpoint == "" {
		nextCheckpoint = "after the next autonomous batch"
	}

	lines := []string{
		"Autonomous session status: " + phase,
		checkpoint.batchLine(),
		"Done: " + joinOrFallback(checkpoint.Done, "none yet"),
		"Current: " + current,
		"Next: " + joinOrFallback(checkpoint.Next, "no queued batches"),
		"Issue/PR actions: " + joinOrFallback(checkpoint.IssuePRActions, "none"),
		"In progress: " + joinOrFallback(checkpoint.InProgress, "none"),
		"Blockers: " + joinOrFallback(checkpoint.Blockers, "none"),
		"Next checkpoint: " + nextCheckpoint,
		"Counts: " + checkpoint.Counts.compactSummary(),
		"Updated: " + updatedAt,
	}
	if checkpoint.Verification != nil {
		lines = append(lines, checkpoint.Verification.summaryLine())
	}
	return strings.Join(lines, "\n")
}

func (c Checkpoint) batchLine() string {
	if c.TotalBatches > 0 {
		return "Batch: " + itoa(c.BatchIndex) + "/" + itoa(c.TotalBatches)
	}
	return "Batch: not started"
}

func (c Counts) compactSummary() string {
	parts := []string{
		"processed=" + itoa(c.Processed),
		"failures=" + itoa(c.Failures),
	}
	if c.SkippedExistingPR > 0 {
		parts = append(parts, "existing-pr="+itoa(c.SkippedExistingPR))
	}
	if c.SkippedExistingBranch > 0 {
		parts = append(parts, "existing-branch="+itoa(c.SkippedExistingBranch))
	}
	if c.SkippedBlockedDependencies > 0 {
		parts = append(parts, "blocked-dependencies="+itoa(c.SkippedBlockedDependencies))
	}
	if c.SkippedOutOfScope > 0 {
		parts = append(parts, "out-of-scope="+itoa(c.SkippedOutOfScope))
	}
	return strings.Join(parts, ", ")
}

func optionalString(value string) string {
	return strings.TrimSpace(value)
}

func joinOrFallback(values []string, fallback string) string {
	trimmed := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		trimmed = append(trimmed, value)
	}
	if len(trimmed) == 0 {
		return fallback
	}
	return strings.Join(trimmed, "; ")
}

func itoa(value int) string {
	return strconv.Itoa(value)
}
