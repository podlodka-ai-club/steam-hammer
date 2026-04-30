package cli

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/workers"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type detachedWorkerState = workers.State

type detachedBatchMetadata = workers.BatchMetadata

type detachedBatchWorkerLink = workers.BatchWorkerLink

type detachedWorkerLogReport struct {
	Lines     int    `json:"lines"`
	UpdatedAt string `json:"updated_at,omitempty"`
}

type detachedWorkerLinkedReport struct {
	Target      string `json:"target,omitempty"`
	LatestState string `json:"latest_state,omitempty"`
	Current     string `json:"current,omitempty"`
	Next        string `json:"next,omitempty"`
	Blockers    string `json:"blockers,omitempty"`
	PR          string `json:"pr,omitempty"`
	PRReadiness string `json:"pr_readiness,omitempty"`
	Updated     string `json:"updated,omitempty"`
}

type detachedWorkerSessionReport struct {
	Phase         string   `json:"phase,omitempty"`
	Current       string   `json:"current,omitempty"`
	Next          []string `json:"next,omitempty"`
	Processed     int      `json:"processed,omitempty"`
	Failures      int      `json:"failures,omitempty"`
	ActiveWorkers int      `json:"active_workers,omitempty"`
	UpdatedAt     string   `json:"updated_at,omitempty"`
}

type detachedWorkerReport struct {
	Worker        detachedWorkerState          `json:"worker"`
	ProcessStatus string                       `json:"process_status"`
	Log           detachedWorkerLogReport      `json:"log"`
	Linked        *detachedWorkerLinkedReport  `json:"linked,omitempty"`
	Session       *detachedWorkerSessionReport `json:"session,omitempty"`
	Batch         *detachedBatchStatusReport   `json:"batch,omitempty"`
	Next          string                       `json:"next,omitempty"`
}

type detachedBatchStatusReport struct {
	RequestedIssueIDs []string                   `json:"requested_issue_ids,omitempty"`
	ActiveWorkers     int                        `json:"active_workers,omitempty"`
	Done              []string                   `json:"done,omitempty"`
	Current           []string                   `json:"current,omitempty"`
	Next              []string                   `json:"next,omitempty"`
	ChildWorkers      []detachedBatchChildReport `json:"child_workers,omitempty"`
	LinkedPRs         []string                   `json:"linked_prs,omitempty"`
	Conflicts         []string                   `json:"conflicts,omitempty"`
	Verification      []string                   `json:"verification,omitempty"`
	Failures          []string                   `json:"failures,omitempty"`
}

type detachedBatchChildReport struct {
	IssueID       string                      `json:"issue_id,omitempty"`
	WorkerName    string                      `json:"worker_name,omitempty"`
	ProcessStatus string                      `json:"process_status,omitempty"`
	LatestState   string                      `json:"latest_state,omitempty"`
	Current       string                      `json:"current,omitempty"`
	Next          string                      `json:"next,omitempty"`
	Blockers      string                      `json:"blockers,omitempty"`
	PR            string                      `json:"pr,omitempty"`
	PRReadiness   string                      `json:"pr_readiness,omitempty"`
	LogLines      int                         `json:"log_lines,omitempty"`
	LogPath       string                      `json:"log_path,omitempty"`
	ClonePath     string                      `json:"clone_path,omitempty"`
	StartedAt     string                      `json:"started_at,omitempty"`
	StatePath     string                      `json:"state_path,omitempty"`
	StatusCommand string                      `json:"status_command,omitempty"`
	Linked        *detachedWorkerLinkedReport `json:"linked,omitempty"`
	NextCommand   string                      `json:"next_command,omitempty"`
}

type detachedWorkerPaths = workers.Paths

func resolveDetachedWorkerPaths(configuredRoot, configuredWorkDir, targetKind, targetID string) (detachedWorkerPaths, error) {
	return workers.ResolvePaths(configuredRoot, configuredWorkDir, targetKind, targetID)
}

func workerName(targetKind, targetID string) string {
	return workers.WorkerName(targetKind, targetID)
}

func (a *App) startDetachedWorker(state detachedWorkerState) int {
	_, code := a.startDetachedWorkerState(state)
	return code
}

func (a *App) startDetachedWorkerState(state detachedWorkerState) (detachedWorkerState, int) {
	if a.start == nil {
		_, _ = fmt.Fprintln(a.err, "orchestrator: detached worker starter is not configured")
		return detachedWorkerState{}, 1
	}
	if err := ensureDetachedWorkerWritable(state.StatePath); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: %v\n", err)
		return detachedWorkerState{}, 1
	}
	if err := os.MkdirAll(filepath.Dir(state.StatePath), 0o755); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create worker directory: %v\n", err)
		return detachedWorkerState{}, 1
	}
	if state.SessionPath != "" {
		if err := os.MkdirAll(filepath.Dir(state.SessionPath), 0o755); err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to create session directory: %v\n", err)
			return detachedWorkerState{}, 1
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
		return detachedWorkerState{}, 1
	}
	state.PID = process.PID
	state.StartedAt = time.Now().UTC().Format(time.RFC3339)
	if err := writeDetachedWorkerState(state); err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to write detached worker state: %v\n", err)
		return detachedWorkerState{}, 1
	}
	_, _ = fmt.Fprintf(a.out, "started detached worker %s\n", state.Name)
	_, _ = fmt.Fprintf(a.out, "pid: %d\n", state.PID)
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	_, _ = fmt.Fprintf(a.out, "next: orchestrator status --worker %s\n", state.Name)
	return state, 0
}

func withDetachedBatchMetadata(states []detachedWorkerState, requestedIssueIDs []int) []detachedWorkerState {
	return workers.WithBatchMetadata(states, requestedIssueIDs, func(state workers.State) string {
		return detachedTargetStatusCommand(state)
	})
}

func writeDetachedBatchStates(states []detachedWorkerState) error {
	return workers.WriteBatchStates(states)
}

func ensureDetachedWorkerWritable(statePath string) error {
	return workers.EnsureWritable(statePath, processRunning)
}

func writeDetachedWorkerState(state detachedWorkerState) error {
	return workers.WriteState(state)
}

func readDetachedWorkerState(path string) (detachedWorkerState, error) {
	return workers.ReadState(path)
}

func (a *App) runDetachedStatus(configuredRoot, name string, asJSON bool) int {
	workerPaths, err := resolveDetachedWorkerPaths(configuredRoot, ".", normalizeWorkerLookupName(name), workerLookupID(name))
	if err != nil {
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to resolve detached worker paths: %v\n", err)
		return 1
	}
	report, err := detachedWorkerReportFromStateFile(workerPaths.StatePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			_, _ = fmt.Fprintf(a.err, "orchestrator: detached worker state not found: %s\n", workerPaths.StatePath)
			return 1
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect detached worker state: %v\n", err)
		return 1
	}
	if asJSON {
		payload, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode detached worker report: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", payload)
		return 0
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
	_, _ = fmt.Fprintf(a.out, "log-freshness: %s\n", detachedWorkerLogFreshness(report.Log))
	_, _ = fmt.Fprintf(a.out, "log: %s\n", state.LogPath)
	_, _ = fmt.Fprintf(a.out, "state: %s\n", state.StatePath)
	if state.SessionPath != "" {
		_, _ = fmt.Fprintf(a.out, "session: %s\n", state.SessionPath)
	}
	if report.Linked != nil {
		if report.Linked.LatestState != "" {
			_, _ = fmt.Fprintf(a.out, "linked-latest-state: %s\n", report.Linked.LatestState)
		}
		if report.Linked.Current != "" {
			_, _ = fmt.Fprintf(a.out, "linked-current: %s\n", report.Linked.Current)
		}
		if report.Linked.Next != "" {
			_, _ = fmt.Fprintf(a.out, "linked-next: %s\n", report.Linked.Next)
		}
		if report.Linked.Blockers != "" {
			_, _ = fmt.Fprintf(a.out, "linked-blockers: %s\n", report.Linked.Blockers)
		}
		if report.Linked.PR != "" {
			_, _ = fmt.Fprintf(a.out, "linked-pr: %s\n", report.Linked.PR)
		}
		if report.Linked.PRReadiness != "" {
			_, _ = fmt.Fprintf(a.out, "linked-pr-readiness: %s\n", report.Linked.PRReadiness)
		}
		if report.Linked.Updated != "" {
			_, _ = fmt.Fprintf(a.out, "linked-updated: %s\n", report.Linked.Updated)
		}
	}
	if report.Session != nil {
		_, _ = fmt.Fprintf(a.out, "session-phase: %s\n", report.Session.Phase)
		_, _ = fmt.Fprintf(a.out, "session-current: %s\n", report.Session.Current)
		_, _ = fmt.Fprintf(a.out, "session-counts: processed=%d failures=%d\n", report.Session.Processed, report.Session.Failures)
		_, _ = fmt.Fprintf(a.out, "session-active-workers: %d\n", report.Session.ActiveWorkers)
	}
	if report.Batch != nil {
		_, _ = fmt.Fprintf(a.out, "batch-child-workers: %d\n", len(report.Batch.ChildWorkers))
		_, _ = fmt.Fprintf(a.out, "batch-active-workers: %d\n", report.Batch.ActiveWorkers)
		_, _ = fmt.Fprintf(a.out, "batch-done: %s\n", joinOrNone(report.Batch.Done))
		_, _ = fmt.Fprintf(a.out, "batch-current: %s\n", joinOrNone(report.Batch.Current))
		_, _ = fmt.Fprintf(a.out, "batch-next: %s\n", joinOrNone(report.Batch.Next))
		_, _ = fmt.Fprintf(a.out, "batch-linked-prs: %s\n", joinOrNone(report.Batch.LinkedPRs))
		_, _ = fmt.Fprintf(a.out, "batch-conflicts: %s\n", joinOrNone(report.Batch.Conflicts))
		_, _ = fmt.Fprintf(a.out, "batch-verification: %s\n", joinOrNone(report.Batch.Verification))
		_, _ = fmt.Fprintf(a.out, "batch-failures: %s\n", joinOrNone(report.Batch.Failures))
	}
	_, _ = fmt.Fprintf(a.out, "next: %s\n", report.Next)
	return 0
}

func (a *App) runAutonomousSessionStatus(path string, asJSON bool) int {
	state, err := orchestration.LoadState(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			_, _ = fmt.Fprintf(a.err, "orchestrator: autonomous session file not found: %s\n", path)
			return 1
		}
		_, _ = fmt.Fprintf(a.err, "orchestrator: failed to inspect autonomous session file: %v\n", err)
		return 1
	}
	if asJSON {
		payload, err := json.MarshalIndent(state, "", "  ")
		if err != nil {
			_, _ = fmt.Fprintf(a.err, "orchestrator: failed to encode autonomous session state: %v\n", err)
			return 1
		}
		_, _ = fmt.Fprintf(a.out, "%s\n", payload)
		return 0
	}
	_, _ = fmt.Fprintln(a.out, state.Summary())
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
		_, _ = fmt.Fprintf(a.out, "%s\t%s\tlines=%d\tlog=%s\t%s\n", report.Worker.Name, report.ProcessStatus, report.Log.Lines, detachedWorkerLogFreshness(report.Log), report.Next)
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
	states, err := workers.ListStates(configuredRoot, ".")
	if err != nil {
		return nil, err
	}
	reports := make([]detachedWorkerReport, 0, len(states))
	for _, state := range states {
		report, err := detachedWorkerReportFromState(state, true)
		if err != nil {
			return nil, err
		}
		reports = append(reports, report)
	}
	return reports, nil
}

func detachedWorkerReportFromStateFile(statePath string) (detachedWorkerReport, error) {
	return detachedWorkerReportFromStateFileWithBatch(statePath, true)
}

func detachedWorkerReportFromStateFileWithBatch(statePath string, includeBatch bool) (detachedWorkerReport, error) {
	state, err := readDetachedWorkerState(statePath)
	if err != nil {
		return detachedWorkerReport{}, err
	}
	return detachedWorkerReportFromState(state, includeBatch)
}

func detachedWorkerReportFromState(state detachedWorkerState, includeBatch bool) (detachedWorkerReport, error) {
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
	if includeBatch && state.Batch != nil {
		if batch, err := detachedBatchStatus(*state.Batch); err == nil {
			report.Batch = batch
		}
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
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return detachedWorkerLogReport{}, nil
		}
		return detachedWorkerLogReport{}, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return detachedWorkerLogReport{}, err
	}
	report := detachedWorkerLogReport{UpdatedAt: info.ModTime().UTC().Format(time.RFC3339)}
	if len(data) == 0 {
		return report, nil
	}
	report.Lines = bytes.Count(data, []byte("\n"))
	return report, nil
}

func detachedWorkerLogFreshness(report detachedWorkerLogReport) string {
	if strings.TrimSpace(report.UpdatedAt) == "" {
		return "no log updates recorded"
	}
	return "updated " + report.UpdatedAt
}

func detachedWorkerLinkedStatus(state detachedWorkerState) (*detachedWorkerLinkedReport, error) {
	if state.TargetKind != "issue" && state.TargetKind != "pr" {
		return nil, nil
	}
	args := []string{defaultRuntimeProvider().RunnerScript(), "--status"}
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
		case "pr readiness":
			linked.PRReadiness = value
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
	state, err := orchestration.LoadState(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	if state.Checkpoint == nil {
		if len(state.ProcessedIssues) == 0 {
			return nil, nil
		}
		return &detachedWorkerSessionReport{Processed: len(state.ProcessedIssues)}, nil
	}
	checkpoint := state.Checkpoint
	report := &detachedWorkerSessionReport{
		Phase:         checkpoint.Phase,
		Current:       checkpoint.Current,
		Next:          checkpoint.Next,
		Processed:     checkpoint.Counts.Processed,
		Failures:      checkpoint.Counts.Failures,
		ActiveWorkers: len(checkpoint.InProgress),
		UpdatedAt:     checkpoint.UpdatedAt,
	}
	if report.Processed == 0 && len(state.ProcessedIssues) > 0 {
		report.Processed = len(state.ProcessedIssues)
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
		LogPath:     paths.LogPath,
		SessionPath: paths.SessionPath,
		StatePath:   paths.StatePath,
		ClonePath:   strings.TrimSpace(*opts.dir),
		WorkDir:     paths.WorkDir,
	}
}

func detachedBatchStatus(batch detachedBatchMetadata) (*detachedBatchStatusReport, error) {
	report := &detachedBatchStatusReport{
		RequestedIssueIDs: append([]string(nil), batch.ChildIssueIDs...),
		ActiveWorkers:     0,
		Done:              []string{},
		Current:           []string{},
		Next:              []string{},
		ChildWorkers:      []detachedBatchChildReport{},
		LinkedPRs:         []string{},
		Conflicts:         []string{},
		Verification:      []string{},
		Failures:          []string{},
	}
	for _, child := range batch.ChildWorkers {
		state, err := readDetachedWorkerState(child.StatePath)
		if err != nil {
			return nil, err
		}
		workerReport, err := detachedWorkerReportFromState(state, false)
		if err != nil {
			return nil, err
		}
		childReport := detachedBatchChildReport{
			IssueID:       child.IssueID,
			WorkerName:    child.WorkerName,
			ProcessStatus: workerReport.ProcessStatus,
			LogLines:      workerReport.Log.Lines,
			LogPath:       child.LogPath,
			ClonePath:     child.ClonePath,
			StartedAt:     child.StartedAt,
			StatePath:     child.StatePath,
			StatusCommand: child.StatusCommand,
			NextCommand:   workerReport.Next,
			Linked:        workerReport.Linked,
		}
		if workerReport.Linked != nil {
			childReport.LatestState = workerReport.Linked.LatestState
			childReport.Current = workerReport.Linked.Current
			childReport.Next = workerReport.Linked.Next
			childReport.Blockers = workerReport.Linked.Blockers
			childReport.PR = workerReport.Linked.PR
			childReport.PRReadiness = workerReport.Linked.PRReadiness
		}
		if workerReport.ProcessStatus == "running" {
			report.ActiveWorkers++
		}
		report.ChildWorkers = append(report.ChildWorkers, childReport)

		issueLabel := batchIssueLabel(child.IssueID)
		if isBatchDoneState(workerReport.Linked) {
			report.Done = append(report.Done, batchStatusEntry(issueLabel, childReport.LatestState, childReport.PR))
		} else {
			current := childReport.Current
			if strings.TrimSpace(current) == "" {
				current = childReport.LatestState
			}
			if strings.TrimSpace(current) == "" {
				current = workerReport.ProcessStatus
			}
			report.Current = append(report.Current, batchStatusEntry(issueLabel, current, childReport.PR))
		}

		next := strings.TrimSpace(childReport.Next)
		if next == "" {
			next = workerReport.Next
		}
		if next != "" {
			report.Next = append(report.Next, fmt.Sprintf("%s: %s", issueLabel, next))
		}
		if childReport.PR != "" {
			report.LinkedPRs = append(report.LinkedPRs, fmt.Sprintf("%s -> %s", issueLabel, childReport.PR))
		}
		if childReport.PRReadiness != "" {
			report.Verification = append(report.Verification, fmt.Sprintf("%s: %s", issueLabel, childReport.PRReadiness))
		}
		for _, text := range []string{childReport.Blockers, childReport.Current, childReport.Next, childReport.PRReadiness} {
			if containsBatchConflict(text) {
				entry := fmt.Sprintf("%s: %s", issueLabel, strings.TrimSpace(text))
				report.Conflicts = append(report.Conflicts, entry)
			}
			if containsBatchFailure(text) {
				entry := fmt.Sprintf("%s: %s", issueLabel, strings.TrimSpace(text))
				report.Failures = append(report.Failures, entry)
			}
		}
		if workerReport.Linked != nil && strings.EqualFold(workerReport.Linked.LatestState, "failed") {
			report.Failures = append(report.Failures, batchStatusEntry(issueLabel, workerReport.Linked.LatestState, childReport.PR))
		}
	}
	report.Done = dedupeStrings(report.Done)
	report.Current = dedupeStrings(report.Current)
	report.Next = dedupeStrings(report.Next)
	report.LinkedPRs = dedupeStrings(report.LinkedPRs)
	report.Conflicts = dedupeStrings(report.Conflicts)
	report.Verification = dedupeStrings(report.Verification)
	report.Failures = dedupeStrings(report.Failures)
	return report, nil
}

func batchIssueLabel(issueID string) string {
	trimmed := strings.TrimSpace(issueID)
	if trimmed == "" {
		return "issue"
	}
	return fmt.Sprintf("issue #%s", trimmed)
}

func batchStatusEntry(issueLabel, value, pr string) string {
	entry := fmt.Sprintf("%s: %s", issueLabel, strings.TrimSpace(value))
	if strings.TrimSpace(pr) != "" {
		entry += fmt.Sprintf(" (%s)", strings.TrimSpace(pr))
	}
	return entry
}

func isBatchDoneState(linked *detachedWorkerLinkedReport) bool {
	if linked == nil {
		return false
	}
	switch strings.TrimSpace(strings.ToLower(linked.LatestState)) {
	case "ready-for-review", "waiting-for-ci", "ready-to-merge":
		return true
	default:
		return false
	}
}

func containsBatchConflict(raw string) bool {
	text := strings.ToLower(strings.TrimSpace(raw))
	if text == "" {
		return false
	}
	return strings.Contains(text, "conflict")
}

func containsBatchFailure(raw string) bool {
	text := strings.ToLower(strings.TrimSpace(raw))
	if text == "" {
		return false
	}
	return strings.Contains(text, "failed") || strings.Contains(text, "failure")
}

func dedupeStrings(values []string) []string {
	if len(values) == 0 {
		return []string{}
	}
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed == "" {
			continue
		}
		if _, ok := seen[trimmed]; ok {
			continue
		}
		seen[trimmed] = struct{}{}
		result = append(result, trimmed)
	}
	return result
}

func joinOrNone(values []string) string {
	if len(values) == 0 {
		return "none"
	}
	return strings.Join(values, "; ")
}
