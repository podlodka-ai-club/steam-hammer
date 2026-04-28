package cli

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type detachedWorkerState struct {
	Name        string   `json:"name"`
	Mode        string   `json:"mode"`
	TargetKind  string   `json:"target_kind"`
	TargetID    string   `json:"target_id,omitempty"`
	Repo        string   `json:"repo,omitempty"`
	Command     []string `json:"command"`
	StartedAt   string   `json:"started_at"`
	PID         int      `json:"pid"`
	LogPath     string   `json:"log_path"`
	SessionPath string   `json:"session_path,omitempty"`
	StatePath   string   `json:"state_path"`
	WorkDir     string   `json:"work_dir"`
}

type detachedWorkerPaths struct {
	statePath   string
	logPath     string
	sessionPath string
	workDir     string
}

func resolveDetachedWorkerPaths(configuredRoot, configuredWorkDir, targetKind, targetID string) (detachedWorkerPaths, error) {
	workDir := "."
	if strings.TrimSpace(configuredWorkDir) != "" {
		workDir = configuredWorkDir
	}
	absWorkDir, err := filepath.Abs(workDir)
	if err != nil {
		return detachedWorkerPaths{}, err
	}

	root := strings.TrimSpace(configuredRoot)
	if root == "" {
		root = filepath.Join(absWorkDir, ".orchestrator", "workers")
	} else if !filepath.IsAbs(root) {
		root = filepath.Join(absWorkDir, root)
	}

	name := workerName(targetKind, targetID)
	workerBase := filepath.Join(root, name)
	paths := detachedWorkerPaths{
		statePath: filepath.Join(workerBase, "worker.json"),
		logPath:   filepath.Join(workerBase, "worker.log"),
		workDir:   absWorkDir,
	}
	if targetKind == "daemon" {
		paths.sessionPath = filepath.Join(workerBase, "session.json")
	}
	return paths, nil
}

func workerName(targetKind, targetID string) string {
	if targetID == "" {
		return targetKind
	}
	return targetKind + "-" + targetID
}

func (a *App) startDetachedWorker(state detachedWorkerState) int {
	if a.start == nil {
		_, _ = fmt.Fprintln(a.err, "orchestrator: detached worker starter is not configured")
		return 1
	}
	if err := ensureDetachedWorkerWritable(state.StatePath); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return 1
	}
	if err := os.MkdirAll(filepath.Dir(state.StatePath), 0o755); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create worker directory: %v\n", err)
		return 1
	}
	if state.SessionPath != "" {
		if err := os.MkdirAll(filepath.Dir(state.SessionPath), 0o755); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create session directory: %v\n", err)
			return 1
		}
	}
	process, err := a.start.Start(DetachedRequest{
		Name:    state.Command[0],
		Args:    state.Command[1:],
		Dir:     state.WorkDir,
		LogPath: state.LogPath,
	})
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to start detached worker: %v\n", err)
		return 1
	}
	state.PID = process.PID
	state.StartedAt = time.Now().UTC().Format(time.RFC3339)
	if err := writeDetachedWorkerState(state); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to write detached worker state: %v\n", err)
		return 1
	}
	_, _ = fmt.Fprintf(a.out, "started detached worker %s\n", state.Name)
	_, _ = fmt.Fprintf(a.out, "pid: %d\n", state.PID)
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	_, _ = fmt.Fprintf(a.out, "next: orchestrator status --worker %s\n", state.Name)
	return 0
}

func ensureDetachedWorkerWritable(statePath string) error {
	state, err := readDetachedWorkerState(statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("failed to inspect detached worker state: %w", err)
	}
	if state.PID > 0 {
		running, _ := processRunning(state.PID)
		if running {
			return fmt.Errorf("detached worker %s is already running with pid %d (see %s)", state.Name, state.PID, state.LogPath)
		}
	}
	return nil
}

func writeDetachedWorkerState(state detachedWorkerState) error {
	payload, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	payload = append(payload, '\n')
	return os.WriteFile(state.StatePath, payload, 0o644)
}

func readDetachedWorkerState(path string) (detachedWorkerState, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return detachedWorkerState{}, err
	}
	var state detachedWorkerState
	if err := json.Unmarshal(data, &state); err != nil {
		return detachedWorkerState{}, err
	}
	return state, nil
}

func (a *App) runDetachedStatus(configuredRoot, name string) int {
	workerPaths, err := resolveDetachedWorkerPaths(configuredRoot, ".", normalizeWorkerLookupName(name), workerLookupID(name))
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
		return 1
	}
	state, err := readDetachedWorkerState(workerPaths.statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			_, _ = fmt.Fprintf(a.err, "orchestrator: detached worker state not found: %s\n", workerPaths.statePath)
			return 1
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to read detached worker state: %v\n", err)
		return 1
	}
	running, runErr := processRunning(state.PID)
	processStatus := "stopped"
	if running {
		processStatus = "running"
	} else if runErr != nil {
		processStatus = "unknown"
	}
	_, _ = fmt.Fprintf(a.out, "worker: %s\n", state.Name)
	if state.TargetKind == "daemon" {
		_, _ = fmt.Fprintln(a.out, "target: daemon")
	} else {
		_, _ = fmt.Fprintf(a.out, "target: %s #%s\n", state.TargetKind, state.TargetID)
	}
	if state.Repo != "" {
		_, _ = fmt.Fprintf(a.out, "repo: %s\n", state.Repo)
	}
	_, _ = fmt.Fprintf(a.out, "process: %s\n", processStatus)
	_, _ = fmt.Fprintf(a.out, "pid: %d\n", state.PID)
	_, _ = fmt.Fprintf(a.out, "started: %s\n", state.StartedAt)
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	_, _ = fmt.Fprintf(a.out, "next: %s\n", detachedWorkerNextAction(state, processStatus))
	return 0
}

func normalizeWorkerLookupName(name string) string {
	trimmed := strings.TrimSpace(name)
	if trimmed == "" {
		return ""
	}
	parts := strings.SplitN(trimmed, "-", 2)
	return parts[0]
}

func workerLookupID(name string) string {
	trimmed := strings.TrimSpace(name)
	parts := strings.SplitN(trimmed, "-", 2)
	if len(parts) != 2 {
		return ""
	}
	return parts[1]
}

func processRunning(pid int) (bool, error) {
	if pid <= 0 {
		return false, nil
	}
	err := syscall.Kill(pid, 0)
	if err == nil {
		return true, nil
	}
	if errors.Is(err, syscall.ESRCH) {
		return false, nil
	}
	return false, err
}

func detachedWorkerNextAction(state detachedWorkerState, processStatus string) string {
	if processStatus == "running" {
		if state.TargetKind == "daemon" && state.SessionPath != "" {
			return fmt.Sprintf("tail -f %s or run orchestrator status --autonomous-session-file %s", state.LogPath, state.SessionPath)
		}
		return fmt.Sprintf("tail -f %s or run %s", state.LogPath, detachedTargetStatusCommand(state))
	}
	if state.TargetKind == "daemon" && state.SessionPath != "" {
		return fmt.Sprintf("inspect %s and, if needed, run orchestrator status --autonomous-session-file %s", state.LogPath, state.SessionPath)
	}
	return fmt.Sprintf("inspect %s and, if needed, run %s", state.LogPath, detachedTargetStatusCommand(state))
}

func detachedTargetStatusCommand(state detachedWorkerState) string {
	if state.TargetKind == "issue" {
		if state.Repo != "" {
			return fmt.Sprintf("orchestrator status --issue %s --repo %s", state.TargetID, state.Repo)
		}
		return fmt.Sprintf("orchestrator status --issue %s", state.TargetID)
	}
	if state.TargetKind == "pr" {
		if state.Repo != "" {
			return fmt.Sprintf("orchestrator status --pr %s --repo %s", state.TargetID, state.Repo)
		}
		return fmt.Sprintf("orchestrator status --pr %s", state.TargetID)
	}
	return fmt.Sprintf("orchestrator status --worker %s", state.Name)
}
