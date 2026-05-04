package workers

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

type State struct {
	Name        string         `json:"name"`
	Mode        string         `json:"mode"`
	TargetKind  string         `json:"target_kind"`
	TargetID    string         `json:"target_id,omitempty"`
	Repo        string         `json:"repo,omitempty"`
	Tracker     string         `json:"tracker,omitempty"`
	CodeHost    string         `json:"codehost,omitempty"`
	Runner      string         `json:"runner,omitempty"`
	Agent       string         `json:"agent,omitempty"`
	Model       string         `json:"model,omitempty"`
	Preset      string         `json:"preset,omitempty"`
	TrackTokens bool           `json:"track_tokens,omitempty"`
	TokenBudget int            `json:"token_budget,omitempty"`
	CostBudget  float64        `json:"cost_budget_usd,omitempty"`
	Command     []string       `json:"command"`
	StartedAt   string         `json:"started_at"`
	PID         int            `json:"pid"`
	LogPath     string         `json:"log_path"`
	SessionPath string         `json:"session_path,omitempty"`
	StatePath   string         `json:"state_path"`
	ClonePath   string         `json:"clone_path,omitempty"`
	PushRemote  string         `json:"push_remote,omitempty"`
	WorkDir     string         `json:"work_dir"`
	Batch       *BatchMetadata `json:"batch,omitempty"`
}

type BatchMetadata struct {
	ChildIssueIDs []string          `json:"child_issue_ids,omitempty"`
	ChildWorkers  []BatchWorkerLink `json:"child_workers,omitempty"`
}

type BatchWorkerLink struct {
	IssueID       string `json:"issue_id,omitempty"`
	WorkerName    string `json:"worker_name,omitempty"`
	LogPath       string `json:"log_path,omitempty"`
	ClonePath     string `json:"clone_path,omitempty"`
	StartedAt     string `json:"started_at,omitempty"`
	StatePath     string `json:"state_path,omitempty"`
	StatusCommand string `json:"status_command,omitempty"`
}

type Paths struct {
	RootDir     string
	StatePath   string
	LogPath     string
	SessionPath string
	WorkDir     string
}

type ProcessChecker func(pid int) (bool, error)

type BatchStatusCommand func(State) string

func ResolvePaths(configuredRoot, configuredWorkDir, targetKind, targetID string) (Paths, error) {
	workDir := "."
	if strings.TrimSpace(configuredWorkDir) != "" {
		workDir = configuredWorkDir
	}
	absWorkDir, err := filepath.Abs(workDir)
	if err != nil {
		return Paths{}, err
	}

	root := strings.TrimSpace(configuredRoot)
	if root == "" {
		root = filepath.Join(absWorkDir, ".orchestrator", "workers")
	} else if !filepath.IsAbs(root) {
		root = filepath.Join(absWorkDir, root)
	}

	name := WorkerName(targetKind, targetID)
	workerBase := filepath.Join(root, name)
	paths := Paths{
		RootDir:   root,
		StatePath: filepath.Join(workerBase, "worker.json"),
		LogPath:   filepath.Join(workerBase, "worker.log"),
		WorkDir:   absWorkDir,
	}
	if targetKind == "daemon" {
		paths.SessionPath = filepath.Join(workerBase, "session.json")
	}
	return paths, nil
}

func WorkerName(targetKind, targetID string) string {
	if targetID == "" {
		return targetKind
	}
	return targetKind + "-" + targetID
}

func WithBatchMetadata(states []State, requestedIssueIDs []int, statusCommand BatchStatusCommand) []State {
	if len(states) == 0 {
		return nil
	}
	childIssueIDs := make([]string, 0, len(requestedIssueIDs))
	for _, id := range requestedIssueIDs {
		childIssueIDs = append(childIssueIDs, strconv.Itoa(id))
	}
	childWorkers := make([]BatchWorkerLink, 0, len(states))
	for _, state := range states {
		link := BatchWorkerLink{
			IssueID:    state.TargetID,
			WorkerName: state.Name,
			LogPath:    state.LogPath,
			ClonePath:  state.ClonePath,
			StartedAt:  state.StartedAt,
			StatePath:  state.StatePath,
		}
		if statusCommand != nil {
			link.StatusCommand = statusCommand(state)
		}
		childWorkers = append(childWorkers, link)
	}
	updated := make([]State, len(states))
	copy(updated, states)
	batch := &BatchMetadata{
		ChildIssueIDs: childIssueIDs,
		ChildWorkers:  childWorkers,
	}
	for i := range updated {
		updated[i].Batch = batch
	}
	return updated
}

func WriteBatchStates(states []State) error {
	for _, state := range states {
		if err := WriteState(state); err != nil {
			return err
		}
	}
	return nil
}

func EnsureWritable(statePath string, processRunning ProcessChecker) error {
	state, err := ReadState(statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("failed to inspect detached worker state: %w", err)
	}
	if state.PID > 0 && processRunning != nil {
		running, _ := processRunning(state.PID)
		if running {
			return fmt.Errorf("detached worker %s is already running with pid %d (see %s)", state.Name, state.PID, state.LogPath)
		}
	}
	return nil
}

func WriteState(state State) error {
	payload, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	payload = append(payload, '\n')
	return os.WriteFile(state.StatePath, payload, 0o644)
}

func ReadState(path string) (State, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return State{}, err
	}
	var state State
	if err := json.Unmarshal(data, &state); err != nil {
		return State{}, err
	}
	return state, nil
}

func ListStates(configuredRoot, configuredWorkDir string) ([]State, error) {
	paths, err := ResolvePaths(configuredRoot, configuredWorkDir, "", "")
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(paths.RootDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return []State{}, nil
		}
		return nil, err
	}
	states := make([]State, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		statePath := filepath.Join(paths.RootDir, entry.Name(), "worker.json")
		state, err := ReadState(statePath)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return nil, err
		}
		states = append(states, state)
	}
	sort.Slice(states, func(i, j int) bool {
		return states[i].Name < states[j].Name
	})
	return states, nil
}
