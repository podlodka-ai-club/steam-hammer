package workers

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestResolvePathsUsesWorkDirRelativeDefaultRoot(t *testing.T) {
	workDir := t.TempDir()

	paths, err := ResolvePaths("", workDir, "issue", "71")
	if err != nil {
		t.Fatalf("ResolvePaths() error = %v", err)
	}

	wantRoot := filepath.Join(workDir, ".orchestrator", "workers")
	if paths.RootDir != wantRoot {
		t.Fatalf("RootDir = %q, want %q", paths.RootDir, wantRoot)
	}
	if paths.StatePath != filepath.Join(wantRoot, "issue-71", "worker.json") {
		t.Fatalf("StatePath = %q", paths.StatePath)
	}
	if paths.LogPath != filepath.Join(wantRoot, "issue-71", "worker.log") {
		t.Fatalf("LogPath = %q", paths.LogPath)
	}
	if paths.WorkDir != workDir {
		t.Fatalf("WorkDir = %q, want %q", paths.WorkDir, workDir)
	}
}

func TestResolvePathsIncludesDaemonSessionPath(t *testing.T) {
	workDir := t.TempDir()

	paths, err := ResolvePaths("workers", workDir, "daemon", "")
	if err != nil {
		t.Fatalf("ResolvePaths() error = %v", err)
	}

	wantRoot := filepath.Join(workDir, "workers")
	if paths.SessionPath != filepath.Join(wantRoot, "daemon", "session.json") {
		t.Fatalf("SessionPath = %q", paths.SessionPath)
	}
}

func TestWriteReadAndListStates(t *testing.T) {
	workDir := t.TempDir()
	root := filepath.Join(workDir, ".orchestrator", "workers")
	state := State{
		Name:       "issue-71",
		Mode:       "run issue",
		TargetKind: "issue",
		TargetID:   "71",
		Command:    []string{"python3", "script.py"},
		StartedAt:  "2026-04-28T12:00:00Z",
		PID:        42,
		LogPath:    filepath.Join(root, "issue-71", "worker.log"),
		StatePath:  filepath.Join(root, "issue-71", "worker.json"),
		WorkDir:    workDir,
	}
	if err := os.MkdirAll(filepath.Dir(state.StatePath), 0o755); err != nil {
		t.Fatalf("MkdirAll() error = %v", err)
	}
	if err := WriteState(state); err != nil {
		t.Fatalf("WriteState() error = %v", err)
	}

	loaded, err := ReadState(state.StatePath)
	if err != nil {
		t.Fatalf("ReadState() error = %v", err)
	}
	if !reflect.DeepEqual(loaded, state) {
		t.Fatalf("ReadState() = %#v, want %#v", loaded, state)
	}

	states, err := ListStates(root, workDir)
	if err != nil {
		t.Fatalf("ListStates() error = %v", err)
	}
	if len(states) != 1 {
		t.Fatalf("ListStates() len = %d, want 1", len(states))
	}
	if !reflect.DeepEqual(states[0], state) {
		t.Fatalf("ListStates()[0] = %#v, want %#v", states[0], state)
	}
}

func TestEnsureWritableAllowsMissingState(t *testing.T) {
	if err := EnsureWritable(filepath.Join(t.TempDir(), "missing.json"), nil); err != nil {
		t.Fatalf("EnsureWritable() error = %v", err)
	}
}

func TestEnsureWritableRejectsRunningWorker(t *testing.T) {
	root := t.TempDir()
	statePath := filepath.Join(root, "worker.json")
	state := State{Name: "issue-71", PID: 31337, LogPath: "/tmp/worker.log", StatePath: statePath}
	if err := WriteState(state); err != nil {
		t.Fatalf("WriteState() error = %v", err)
	}

	err := EnsureWritable(statePath, func(pid int) (bool, error) {
		if pid != 31337 {
			t.Fatalf("pid = %d, want 31337", pid)
		}
		return true, nil
	})
	if err == nil {
		t.Fatal("EnsureWritable() error = nil, want running worker error")
	}
	if !strings.Contains(err.Error(), "already running") {
		t.Fatalf("EnsureWritable() error = %v", err)
	}
}

func TestWithBatchMetadataCopiesStatesAndAttachesLinks(t *testing.T) {
	states := []State{{
		Name:      "issue-71",
		TargetID:  "71",
		LogPath:   "/tmp/issue-71.log",
		ClonePath: "/repo/71",
		StartedAt: "2026-04-28T12:00:00Z",
		StatePath: "/tmp/issue-71.json",
	}}

	updated := WithBatchMetadata(states, []int{71, 72}, func(state State) string {
		return "status " + state.Name
	})

	if states[0].Batch != nil {
		t.Fatal("WithBatchMetadata() mutated input slice")
	}
	if updated[0].Batch == nil {
		t.Fatal("WithBatchMetadata() batch = nil")
	}
	if !reflect.DeepEqual(updated[0].Batch.ChildIssueIDs, []string{"71", "72"}) {
		t.Fatalf("ChildIssueIDs = %#v", updated[0].Batch.ChildIssueIDs)
	}
	if len(updated[0].Batch.ChildWorkers) != 1 {
		t.Fatalf("ChildWorkers len = %d, want 1", len(updated[0].Batch.ChildWorkers))
	}
	if updated[0].Batch.ChildWorkers[0].StatusCommand != "status issue-71" {
		t.Fatalf("StatusCommand = %q", updated[0].Batch.ChildWorkers[0].StatusCommand)
	}
}

func TestListStatesReturnsEmptyWhenRegistryMissing(t *testing.T) {
	states, err := ListStates(filepath.Join(t.TempDir(), "workers"), ".")
	if err != nil {
		t.Fatalf("ListStates() error = %v", err)
	}
	if len(states) != 0 {
		t.Fatalf("ListStates() len = %d, want 0", len(states))
	}
}
