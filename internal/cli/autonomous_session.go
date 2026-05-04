package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

type nativeSessionTracker struct {
	path  string
	label string
	key   string
}

func startNativeSessionTracker(path, label, key string) nativeSessionTracker {
	tracker := nativeSessionTracker{
		path:  strings.TrimSpace(path),
		label: strings.TrimSpace(label),
		key:   strings.TrimSpace(key),
	}
	if tracker.path != "" {
		tracker.write(nil, true, false)
	}
	return tracker
}

func (t nativeSessionTracker) finish(state *orchestration.TrackedState, exitCode int) {
	if t.path == "" {
		return
	}
	t.write(state, false, exitCode != 0)
}

func (t nativeSessionTracker) write(state *orchestration.TrackedState, running, failed bool) {
	if t.path == "" {
		return
	}
	persisted, err := loadAutonomousSessionState(t.path)
	if err != nil {
		persisted = orchestration.State{ProcessedIssues: map[string]json.RawMessage{}}
	}
	if persisted.ProcessedIssues == nil {
		persisted.ProcessedIssues = map[string]json.RawMessage{}
	}
	if persisted.Checkpoint == nil {
		persisted.Checkpoint = &orchestration.Checkpoint{}
	}
	checkpoint := persisted.Checkpoint
	checkpoint.UpdatedAt = time.Now().UTC().Format(time.RFC3339)

	if running {
		checkpoint.Phase = "running"
		checkpoint.Current = t.label
		checkpoint.Done = nil
		checkpoint.Next = []string{"wait for completion"}
		checkpoint.IssuePRActions = nil
		checkpoint.InProgress = []string{t.label}
		checkpoint.Blockers = nil
		checkpoint.NextCheckpoint = "when the current worker finishes"
		checkpoint.Counts.Processed = len(persisted.ProcessedIssues)
		_ = saveAutonomousSessionState(t.path, persisted)
		return
	}

	checkpoint.Phase = "completed"
	checkpoint.Current = "Idle between autonomous runs"
	checkpoint.InProgress = nil
	checkpoint.NextCheckpoint = "on the next worker run"
	checkpoint.Done = nil
	checkpoint.Next = nil
	checkpoint.IssuePRActions = nil
	checkpoint.Blockers = nil

	if state != nil {
		if raw, err := json.Marshal(state); err == nil {
			if t.key != "" {
				persisted.ProcessedIssues[t.key] = raw
			}
		}
		checkpoint.Done = []string{nativeSessionDoneSummary(t.label, *state)}
		if next := nativeSessionNextSummary(*state); next != "" {
			checkpoint.Next = []string{next}
		}
		if action := nativeSessionActionSummary(*state); action != "" {
			checkpoint.IssuePRActions = []string{action}
		}
		if blocker := strings.TrimSpace(state.Error); blocker != "" {
			checkpoint.Blockers = []string{blocker}
		}
	}
	checkpoint.Counts.Processed = len(persisted.ProcessedIssues)
	if failed {
		checkpoint.Counts.Failures = 1
	} else {
		checkpoint.Counts.Failures = 0
	}
	_ = saveAutonomousSessionState(t.path, persisted)
}

func loadAutonomousSessionState(path string) (orchestration.State, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return orchestration.State{ProcessedIssues: map[string]json.RawMessage{}}, nil
		}
		return orchestration.State{}, err
	}
	if len(strings.TrimSpace(string(raw))) == 0 {
		return orchestration.State{ProcessedIssues: map[string]json.RawMessage{}}, nil
	}
	return orchestration.ParseState(raw)
}

func saveAutonomousSessionState(path string, state orchestration.State) error {
	if strings.TrimSpace(path) == "" {
		return nil
	}
	if state.ProcessedIssues == nil {
		state.ProcessedIssues = map[string]json.RawMessage{}
	}
	directory := filepath.Dir(path)
	if directory == "" {
		directory = "."
	}
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return fmt.Errorf("failed to create autonomous session directory %s: %w", directory, err)
	}
	encoded, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode autonomous session file %s: %w", path, err)
	}
	encoded = append(encoded, '\n')
	return os.WriteFile(path, encoded, 0o644)
}

func autonomousSessionOwner(opts commonOptions) orchestration.CheckpointOwner {
	return orchestration.CheckpointOwner{
		Repo:     strings.TrimSpace(*opts.repo),
		Dir:      strings.TrimSpace(*opts.dir),
		Tracker:  strings.TrimSpace(*opts.tracker),
		CodeHost: strings.TrimSpace(*opts.codehost),
		Runner:   strings.TrimSpace(*opts.runner),
		Agent:    strings.TrimSpace(*opts.agent),
		Model:    strings.TrimSpace(*opts.model),
	}
}

func loadResumeAutonomousSessionState(path string, opts commonOptions) (orchestration.State, error) {
	trimmed := strings.TrimSpace(path)
	if trimmed == "" {
		return orchestration.State{}, fmt.Errorf("resume requires --resume-autonomous-session-file PATH")
	}
	state, err := orchestration.LoadState(trimmed)
	if err != nil {
		if os.IsNotExist(err) {
			return orchestration.State{}, fmt.Errorf("resume checkpoint %s does not exist", trimmed)
		}
		return orchestration.State{}, fmt.Errorf("failed to load resume checkpoint %s: %w", trimmed, err)
	}
	if state.Checkpoint == nil {
		return orchestration.State{}, fmt.Errorf("resume checkpoint %s does not contain checkpoint metadata", trimmed)
	}
	if err := validateAutonomousSessionOwner(state.Checkpoint.Owner, autonomousSessionOwner(opts)); err != nil {
		return orchestration.State{}, err
	}
	return state, nil
}

func ensureAutonomousSessionOwner(path string, opts commonOptions) error {
	trimmed := strings.TrimSpace(path)
	if trimmed == "" {
		return nil
	}
	state, err := loadAutonomousSessionState(trimmed)
	if err != nil {
		return err
	}
	if state.Checkpoint == nil {
		state.Checkpoint = &orchestration.Checkpoint{}
	}
	owner := autonomousSessionOwner(opts)
	if state.Checkpoint.Owner != nil && reflect.DeepEqual(*state.Checkpoint.Owner, owner) {
		return nil
	}
	state.Checkpoint.Owner = &owner
	return saveAutonomousSessionState(trimmed, state)
}

func validateAutonomousSessionOwner(saved *orchestration.CheckpointOwner, current orchestration.CheckpointOwner) error {
	if saved == nil {
		return nil
	}
	checks := []struct {
		name string
		want string
		got  string
	}{
		{name: "repo", want: saved.Repo, got: current.Repo},
		{name: "dir", want: saved.Dir, got: current.Dir},
		{name: "tracker", want: saved.Tracker, got: current.Tracker},
		{name: "codehost", want: saved.CodeHost, got: current.CodeHost},
		{name: "runner", want: saved.Runner, got: current.Runner},
		{name: "agent", want: saved.Agent, got: current.Agent},
		{name: "model", want: saved.Model, got: current.Model},
	}
	for _, check := range checks {
		want := strings.TrimSpace(check.want)
		if want == "" {
			continue
		}
		got := strings.TrimSpace(check.got)
		if got != want {
			return fmt.Errorf("resume checkpoint ownership mismatch for %s: checkpoint=%q current=%q", check.name, want, got)
		}
	}
	return nil
}

func nativeSessionDoneSummary(label string, state orchestration.TrackedState) string {
	status := strings.TrimSpace(state.Status)
	if status == "" {
		return label
	}
	return fmt.Sprintf("%s (%s)", label, status)
}

func nativeSessionNextSummary(state orchestration.TrackedState) string {
	return nativeSessionHumanize(state.NextAction)
}

func nativeSessionActionSummary(state orchestration.TrackedState) string {
	status := strings.TrimSpace(state.Status)
	taskType := strings.TrimSpace(state.TaskType)
	if taskType == "issue" && status == orchestration.StatusReadyForReview {
		if state.PR != nil && *state.PR > 0 {
			return fmt.Sprintf("prepared PR #%d for review", *state.PR)
		}
		return "prepared issue for review"
	}
	if taskType == "pr" && status == orchestration.StatusWaitingForCI {
		return "pushed PR updates and waiting for CI"
	}
	if status == "" {
		return ""
	}
	return nativeSessionHumanize(status)
}

func nativeSessionHumanize(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	value = strings.ReplaceAll(value, "_", " ")
	value = strings.ReplaceAll(value, "-", " ")
	return value
}
