package cli

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
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
	Tracker     string   `json:"tracker,omitempty"`
	CodeHost    string   `json:"codehost,omitempty"`
	Runner      string   `json:"runner,omitempty"`
	Agent       string   `json:"agent,omitempty"`
	Model       string   `json:"model,omitempty"`
	Command     []string `json:"command"`
	StartedAt   string   `json:"started_at"`
	PID         int      `json:"pid"`
	LogPath     string   `json:"log_path"`
	SessionPath string   `json:"session_path,omitempty"`
	StatePath   string   `json:"state_path"`
	ClonePath   string   `json:"clone_path,omitempty"`
	WorkDir     string   `json:"work_dir"`
}

type detachedWorkerLogReport struct {
	Lines int `json:"lines"`
}

type detachedWorkerLinkedReport struct {
	Target      string `json:"target,omitempty"`
	LatestState string `json:"latest_state,omitempty"`
	Current     string `json:"current,omitempty"`
	Next        string `json:"next,omitempty"`
	Blockers    string `json:"blockers,omitempty"`
	PR          string `json:"pr,omitempty"`
	Updated     string `json:"updated,omitempty"`
}

type detachedWorkerSessionReport struct {
	Phase     string   `json:"phase,omitempty"`
	Current   string   `json:"current,omitempty"`
	Next      []string `json:"next,omitempty"`
	Processed int      `json:"processed,omitempty"`
	Failures  int      `json:"failures,omitempty"`
	UpdatedAt string   `json:"updated_at,omitempty"`
}

type detachedWorkerReport struct {
	Worker        detachedWorkerState          `json:"worker"`
	ProcessStatus string                       `json:"process_status"`
	Log           detachedWorkerLogReport      `json:"log"`
	Linked        *detachedWorkerLinkedReport  `json:"linked,omitempty"`
	Session       *detachedWorkerSessionReport `json:"session,omitempty"`
	Next          string                       `json:"next,omitempty"`
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
	report, err := detachedWorkerReportFromStateFile(workerPaths.statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			_, _ = fmt.Fprintf(a.err, "orchestrator: detached worker state not found: %s\n", workerPaths.statePath)
			return 1
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect detached worker state: %v\n", err)
		return 1
	}
	state := report.Worker
	_, _ = fmt.Fprintf(a.out, "worker: %s\n", state.Name)
	if state.TargetKind == "daemon" {
		_, _ = fmt.Fprintln(a.out, "target: daemon")
	} else {
		_, _ = fmt.Fprintf(a.out, "target: %s #%s\n", state.TargetKind, state.TargetID)
	}
	if state.Repo != "" {
		_, _ = fmt.Fprintf(a.out, "repo: %s\n", state.Repo)
	}
	_, _ = fmt.Fprintf(a.out, "process: %s\n", report.ProcessStatus)
	_, _ = fmt.Fprintf(a.out, "pid: %d\n", state.PID)
	_, _ = fmt.Fprintf(a.out, "started: %s\n", state.StartedAt)
	if state.ClonePath != "" {
		_, _ = fmt.Fprintf(a.out, "clone: %s\n", state.ClonePath)
	}
	if state.Runner != "" || state.Agent != "" || state.Model != "" {
		_, _ = fmt.Fprintf(a.out, "agent: runner=%s agent=%s model=%s\n", state.Runner, state.Agent, state.Model)
	}
	_, _ = fmt.Fprintf(a.out, "log-progress: lines=%d\n", report.Log.Lines)
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	if report.Linked != nil {
		if report.Linked.LatestState != "" {
			_, _ = fmt.Fprintf(a.out, "linked-latest-state: %s\n", report.Linked.LatestState)
		}
		if report.Linked.PR != "" {
			_, _ = fmt.Fprintf(a.out, "linked-pr: %s\n", report.Linked.PR)
		}
	}
	if report.Session != nil {
		_, _ = fmt.Fprintf(a.out, "session-phase: %s\n", report.Session.Phase)
		_, _ = fmt.Fprintf(a.out, "session-current: %s\n", report.Session.Current)
		_, _ = fmt.Fprintf(a.out, "session-counts: processed=%d failures=%d\n", report.Session.Processed, report.Session.Failures)
	}
	_, _ = fmt.Fprintf(a.out, "next: %s\n", report.Next)
	return 0
}

func (a *App) runDetachedStatusList(configuredRoot string, asJSON bool) int {
	reports, err := detachedWorkerReports(configuredRoot)
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect detached worker registry: %v\n", err)
		return 1
	}
	if asJSON {
		payload, err := json.MarshalIndent(struct {
			Workers []detachedWorkerReport `json:"workers"`
		}{Workers: reports}, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode detached worker registry: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", payload)
		return 0
	}
	for _, report := range reports {
		_, _ = fmt.Fprintf(a.out, "%s\t%s\tlines=%d\t%s\n", report.Worker.Name, report.ProcessStatus, report.Log.Lines, report.Next)
	}
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

func detachedWorkerReports(configuredRoot string) ([]detachedWorkerReport, error) {
	workerPaths, err := resolveDetachedWorkerPaths(configuredRoot, ".", "", "")
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(filepath.Dir(workerPaths.statePath))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return []detachedWorkerReport{}, nil
		}
		return nil, err
	}
	reports := make([]detachedWorkerReport, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		statePath := filepath.Join(filepath.Dir(workerPaths.statePath), entry.Name(), "worker.json")
		report, err := detachedWorkerReportFromStateFile(statePath)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return nil, err
		}
		reports = append(reports, report)
	}
	sort.Slice(reports, func(i, j int) bool {
		return reports[i].Worker.Name < reports[j].Worker.Name
	})
	return reports, nil
}

func detachedWorkerReportFromStateFile(statePath string) (detachedWorkerReport, error) {
	state, err := readDetachedWorkerState(statePath)
	if err != nil {
		return detachedWorkerReport{}, err
	}
	processStatus := detachedProcessStatus(state.PID)
	logReport, err := detachedWorkerLogSummary(state.LogPath)
	if err != nil {
		return detachedWorkerReport{}, err
	}
	report := detachedWorkerReport{
		Worker:        state,
		ProcessStatus: processStatus,
		Log:           logReport,
		Next:          detachedWorkerNextAction(state, processStatus),
	}
	if linked, err := detachedWorkerLinkedStatus(state); err == nil {
		report.Linked = linked
	}
	if session, err := detachedWorkerSessionStatus(state.SessionPath); err == nil {
		report.Session = session
	}
	return report, nil
}

func detachedProcessStatus(pid int) string {
	running, err := processRunning(pid)
	if running {
		return "running"
	}
	if err != nil {
		return "unknown"
	}
	return "exited"
}

func detachedWorkerLogSummary(path string) (detachedWorkerLogReport, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return detachedWorkerLogReport{}, nil
		}
		return detachedWorkerLogReport{}, err
	}
	if len(data) == 0 {
		return detachedWorkerLogReport{}, nil
	}
	return detachedWorkerLogReport{Lines: bytes.Count(data, []byte("\n"))}, nil
}

func detachedWorkerLinkedStatus(state detachedWorkerState) (*detachedWorkerLinkedReport, error) {
	if state.TargetKind != "issue" && state.TargetKind != "pr" {
		return nil, nil
	}
	args := []string{runnerScript, "--status"}
	if state.TargetKind == "issue" {
		args = append(args, "--issue", state.TargetID)
	} else {
		args = append(args, "--pr", state.TargetID)
	}
	if state.Repo != "" {
		args = append(args, "--repo", state.Repo)
	}
	if state.Tracker != "" {
		args = append(args, "--tracker", state.Tracker)
	}
	if state.CodeHost != "" {
		args = append(args, "--codehost", state.CodeHost)
	}
	if state.ClonePath != "" {
		args = append(args, "--dir", state.ClonePath)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "python3", args...)
	if state.ClonePath != "" {
		cmd.Dir = state.ClonePath
	}
	output, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	linked := &detachedWorkerLinkedReport{}
	scanner := bufio.NewScanner(bytes.NewReader(output))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		value = strings.TrimSpace(value)
		switch strings.ToLower(strings.TrimSpace(key)) {
		case "target":
			linked.Target = value
		case "latest state":
			linked.LatestState = value
		case "current":
			linked.Current = value
		case "next":
			linked.Next = value
		case "blockers":
			linked.Blockers = value
		case "pr":
			linked.PR = value
		case "updated":
			linked.Updated = value
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if *linked == (detachedWorkerLinkedReport{}) {
		return nil, nil
	}
	return linked, nil
}

func detachedWorkerSessionStatus(path string) (*detachedWorkerSessionReport, error) {
	if strings.TrimSpace(path) == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var payload struct {
		Checkpoint struct {
			Phase     string   `json:"phase"`
			Current   string   `json:"current"`
			Next      []string `json:"next"`
			UpdatedAt string   `json:"updated_at"`
			Counts    struct {
				Processed int `json:"processed"`
				Failures  int `json:"failures"`
			} `json:"counts"`
		} `json:"checkpoint"`
		ProcessedIssues map[string]json.RawMessage `json:"processed_issues"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, err
	}
	report := &detachedWorkerSessionReport{
		Phase:     payload.Checkpoint.Phase,
		Current:   payload.Checkpoint.Current,
		Next:      payload.Checkpoint.Next,
		Processed: payload.Checkpoint.Counts.Processed,
		Failures:  payload.Checkpoint.Counts.Failures,
		UpdatedAt: payload.Checkpoint.UpdatedAt,
	}
	if report.Processed == 0 && len(payload.ProcessedIssues) > 0 {
		report.Processed = len(payload.ProcessedIssues)
	}
	if report.Phase == "" && report.Current == "" && len(report.Next) == 0 && report.Processed == 0 && report.Failures == 0 && report.UpdatedAt == "" {
		return nil, nil
	}
	return report, nil
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

func detachedWorkerStateFromOptions(name, mode, targetKind, targetID string, opts commonOptions, command []string, paths detachedWorkerPaths) detachedWorkerState {
	return detachedWorkerState{
		Name:        name,
		Mode:        mode,
		TargetKind:  targetKind,
		TargetID:    targetID,
		Repo:        strings.TrimSpace(*opts.repo),
		Tracker:     strings.TrimSpace(*opts.tracker),
		CodeHost:    strings.TrimSpace(*opts.codehost),
		Runner:      strings.TrimSpace(*opts.runner),
		Agent:       strings.TrimSpace(*opts.agent),
		Model:       strings.TrimSpace(*opts.model),
		Command:     append([]string(nil), command...),
		LogPath:     paths.logPath,
		SessionPath: paths.sessionPath,
		StatePath:   paths.statePath,
		ClonePath:   strings.TrimSpace(*opts.dir),
		WorkDir:     paths.workDir,
	}
}
