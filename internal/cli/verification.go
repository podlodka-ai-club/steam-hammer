package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/lifecycle"
	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

type shellExecutionResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
}

type shellExecutor interface {
	Run(ctx context.Context, cwd, command string) (shellExecutionResult, error)
}

type execShellExecutor struct{}

func (execShellExecutor) Run(ctx context.Context, cwd, command string) (shellExecutionResult, error) {
	cmd := exec.CommandContext(ctx, "bash", "-lc", command)
	cmd.Dir = cwd
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	result := shellExecutionResult{
		Stdout:   stdout.String(),
		Stderr:   stderr.String(),
		ExitCode: 0,
	}
	if err == nil {
		return result, nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		result.ExitCode = exitErr.ExitCode()
		return result, nil
	}
	if ctx.Err() != nil {
		return result, ctx.Err()
	}
	return result, err
}

func (a *App) SetShellExecutor(executor shellExecutor) {
	if executor == nil {
		a.shell = execShellExecutor{}
		return
	}
	a.shell = executor
}

func (a *App) runPostBatchVerification(ctx context.Context, opts commonOptions, createFollowupIssue bool, sessionPath string) (orchestration.VerificationResult, error) {
	cwd := defaultSourceDir(*opts.dir)
	commands, err := postBatchVerificationCommands(cwd, *opts.project)
	if err != nil {
		return orchestration.VerificationResult{}, err
	}
	if len(commands) == 0 {
		result := orchestration.VerificationResult{
			Status:     "not-applicable",
			Summary:    "not-applicable (no verification commands detected)",
			NextAction: "configure_post_batch_verification",
			Commands:   []orchestration.VerificationCommandResult{},
			FollowUpIssue: &orchestration.VerificationFollowUpIssue{
				Status: "not-needed",
			},
		}
		if err := persistVerificationToSession(sessionPath, result); err != nil {
			return orchestration.VerificationResult{}, err
		}
		_, _ = fmt.Fprintln(a.out, result.SummaryLine())
		return result, nil
	}

	results := make([]orchestration.VerificationCommandResult, 0, len(commands))
	for _, command := range commands {
		if *opts.dryRun {
			_, _ = fmt.Fprintf(a.out, "[dry-run] Would run post-batch verification '%s': %s\n", command.Name, command.Command)
			results = append(results, orchestration.VerificationCommandResult{
				Name:    command.Name,
				Command: command.Command,
				Status:  "dry-run",
			})
			continue
		}

		_, _ = fmt.Fprintf(a.out, "Running post-batch verification '%s': %s\n", command.Name, command.Command)
		execution, err := a.shell.Run(ctx, cwd, command.Command)
		if err != nil {
			return orchestration.VerificationResult{}, err
		}
		result := orchestration.VerificationCommandResult{
			Name:    command.Name,
			Command: command.Command,
			Status:  "passed",
		}
		if execution.ExitCode != 0 {
			result.Status = orchestration.StatusFailed
			result.ExitCode = intPtr(execution.ExitCode)
		} else {
			result.ExitCode = intPtr(0)
		}
		if excerpt := orchestration.WorkflowOutputExcerpt(execution.Stdout, 600); excerpt != "" {
			result.StdoutExcerpt = excerpt
		}
		if excerpt := orchestration.WorkflowOutputExcerpt(execution.Stderr, 600); excerpt != "" {
			result.StderrExcerpt = excerpt
		}
		results = append(results, result)
		if result.Status == orchestration.StatusFailed {
			_, _ = fmt.Fprintf(a.err, "Post-batch verification '%s' failed with exit code %d\n", command.Name, execution.ExitCode)
			continue
		}
		_, _ = fmt.Fprintf(a.out, "Post-batch verification '%s' passed\n", command.Name)
	}

	verification := orchestration.VerificationResult{Commands: results}
	if *opts.dryRun {
		verification.Status = "dry-run"
		verification.Summary = fmt.Sprintf("dry-run (%d commands)", len(results))
		verification.NextAction = "run_post_batch_verification"
		verification.FollowUpIssue = &orchestration.VerificationFollowUpIssue{Status: "not-requested"}
		if err := persistVerificationToSession(sessionPath, verification); err != nil {
			return orchestration.VerificationResult{}, err
		}
		_, _ = fmt.Fprintln(a.out, verification.SummaryLine())
		return verification, nil
	}

	failed := false
	for _, result := range results {
		if strings.EqualFold(strings.TrimSpace(result.Status), orchestration.StatusFailed) {
			failed = true
			break
		}
	}
	verification.Status = "passed"
	verification.Summary = orchestration.SummarizeVerificationResults(results)
	verification.NextAction = "none"
	if failed {
		verification.Status = orchestration.StatusFailed
		verification.NextAction = "inspect_verification_failures"
	}

	if !failed {
		verification.FollowUpIssue = &orchestration.VerificationFollowUpIssue{Status: "not-needed"}
		if err := persistVerificationToSession(sessionPath, verification); err != nil {
			return orchestration.VerificationResult{}, err
		}
		_, _ = fmt.Fprintln(a.out, verification.SummaryLine())
		return verification, nil
	}

	followUp := orchestration.RecommendedPostBatchFollowUpIssue(strings.TrimSpace(*opts.repo), verification, nil)
	tracker := strings.TrimSpace(*opts.tracker)
	if tracker == "" {
		tracker = lifecycle.TrackerGitHub
	}
	if createFollowupIssue && strings.EqualFold(tracker, lifecycle.TrackerGitHub) && a.daemon != nil && strings.TrimSpace(*opts.repo) != "" {
		created, err := a.daemon.CreateIssue(ctx, lifecycle.CreateIssueRequest{
			Repo:  strings.TrimSpace(*opts.repo),
			Title: followUp.Title,
			Body:  followUp.Body,
		})
		if err != nil {
			return orchestration.VerificationResult{}, err
		}
		followUp.Status = "created"
		followUp.IssueNumber = intPtr(created.Number)
		followUp.IssueRef = "#" + fmt.Sprintf("%d", created.Number)
		followUp.IssueURL = strings.TrimSpace(created.URL)
		verification.NextAction = "fix_regression_from_follow_up_issue"
		verification.FollowUpIssue = &followUp
		if err := persistVerificationToSession(sessionPath, verification); err != nil {
			return orchestration.VerificationResult{}, err
		}
		_, _ = fmt.Fprintln(a.out, verification.SummaryLine())
		return verification, nil
	}

	verification.NextAction = "create_follow_up_issue_and_fix_regression"
	verification.FollowUpIssue = &followUp
	if err := persistVerificationToSession(sessionPath, verification); err != nil {
		return orchestration.VerificationResult{}, err
	}
	_, _ = fmt.Fprintln(a.out, verification.SummaryLine())
	return verification, nil
}

func postBatchVerificationCommands(cwd, explicitProjectConfigPath string) ([]orchestration.VerificationCommand, error) {
	projectConfig, err := loadVerificationProjectConfig(cwd, explicitProjectConfigPath)
	if err != nil {
		return nil, err
	}
	if len(projectConfig) > 0 {
		if commands := orchestration.ConfiguredWorkflowCommands(projectConfig); len(commands) > 0 {
			return commands, nil
		}
	}
	targetDir := filepath.Clean(cwd)
	commands := make([]orchestration.VerificationCommand, 0, len(orchestration.PostBatchVerificationDefaultCommands))
	if info, err := os.Stat(filepath.Join(targetDir, "tests")); err == nil && info.IsDir() {
		commands = append(commands, orchestration.PostBatchVerificationDefaultCommands[0])
	}
	if info, err := os.Stat(filepath.Join(targetDir, "go.mod")); err == nil && !info.IsDir() {
		commands = append(commands, orchestration.PostBatchVerificationDefaultCommands[1])
	}
	return commands, nil
}

func loadVerificationProjectConfig(cwd, explicitPath string) (map[string]any, error) {
	path := strings.TrimSpace(explicitPath)
	explicit := path != ""
	if path == "" {
		path = filepath.Join(defaultSourceDir(cwd), defaultProjectConfigName)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) && !explicit {
			return nil, nil
		}
		return nil, fmt.Errorf("failed to read project config %s: %w", path, err)
	}
	var projectConfig map[string]any
	if err := json.Unmarshal(raw, &projectConfig); err != nil {
		return nil, fmt.Errorf("failed to parse project config %s: %w", path, err)
	}
	return projectConfig, nil
}

func persistVerificationToSession(sessionPath string, verification orchestration.VerificationResult) error {
	if strings.TrimSpace(sessionPath) == "" {
		return nil
	}
	raw, err := os.ReadFile(sessionPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("failed to read autonomous session file %s: %w", sessionPath, err)
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil
	}
	var state map[string]any
	if err := json.Unmarshal(raw, &state); err != nil {
		return fmt.Errorf("failed to parse autonomous session file %s: %w", sessionPath, err)
	}
	checkpoint, _ := state["checkpoint"].(map[string]any)
	if checkpoint == nil {
		return nil
	}
	encodedVerification, err := json.Marshal(verification)
	if err != nil {
		return fmt.Errorf("failed to encode verification result: %w", err)
	}
	var verificationValue map[string]any
	if err := json.Unmarshal(encodedVerification, &verificationValue); err != nil {
		return fmt.Errorf("failed to normalize verification result: %w", err)
	}
	checkpoint["verification"] = verificationValue
	state["checkpoint"] = checkpoint
	encodedState, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode autonomous session file %s: %w", sessionPath, err)
	}
	encodedState = append(encodedState, '\n')
	if err := os.WriteFile(sessionPath, encodedState, 0o644); err != nil {
		return fmt.Errorf("failed to write autonomous session file %s: %w", sessionPath, err)
	}
	return nil
}

func intPtr(value int) *int {
	return &value
}
