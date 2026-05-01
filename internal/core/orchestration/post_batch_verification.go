package orchestration

import "strings"

var workflowCheckCommandOrder = []string{"test", "lint", "build", "e2e"}

var PostBatchVerificationDefaultCommands = []VerificationCommand{
	{Name: "python-tests", Command: "python3 -m unittest discover -s tests -q"},
	{Name: "go-test", Command: "go test ./..."},
}

func ConfiguredWorkflowCommands(projectConfig map[string]any) []VerificationCommand {
	workflow, _ := projectConfig["workflow"].(map[string]any)
	commands, _ := workflow["commands"].(map[string]any)
	if len(commands) == 0 {
		return nil
	}
	configured := make([]VerificationCommand, 0, len(workflowCheckCommandOrder))
	for _, name := range workflowCheckCommandOrder {
		rawCommand, ok := commands[name]
		if !ok || rawCommand == nil {
			continue
		}
		commandText, ok := rawCommand.(string)
		if !ok {
			continue
		}
		trimmed := strings.TrimSpace(commandText)
		if trimmed == "" {
			continue
		}
		configured = append(configured, VerificationCommand{Name: name, Command: trimmed})
	}
	return configured
}

func WorkflowOutputExcerpt(text string, maxLen int) string {
	if maxLen <= 0 {
		maxLen = 600
	}
	compact := strings.Join(strings.Fields(strings.TrimSpace(text)), " ")
	if len(compact) <= maxLen {
		return compact
	}
	if maxLen <= 3 {
		return compact[:maxLen]
	}
	return compact[:maxLen-3] + "..."
}

func FormatPostBatchVerificationIssueBody(repo string, verification VerificationResult, touchedPRs []string) string {
	lines := []string{
		"Automated post-batch verification detected a repository regression.",
		"",
		"Repository: " + strings.TrimSpace(repo),
		"Result: " + fallbackVerificationSummary(verification),
		"Next action: " + humanizeToken(fallbackVerificationNextAction(verification)),
	}
	if len(touchedPRs) > 0 {
		lines = append(lines, "", "Touched PRs:")
		for _, prURL := range touchedPRs {
			prURL = strings.TrimSpace(prURL)
			if prURL != "" {
				lines = append(lines, "- "+prURL)
			}
		}
	}
	lines = append(lines, "", "Verification commands:")
	for _, result := range verification.Commands {
		name := strings.TrimSpace(result.Name)
		if name == "" {
			name = "command"
		}
		status := strings.TrimSpace(result.Status)
		if status == "" {
			status = "unknown"
		}
		detail := "- `" + name + "`: " + status
		if result.ExitCode != nil {
			detail += " (exit code " + itoa(*result.ExitCode) + ")"
		}
		if commandText := strings.TrimSpace(result.Command); commandText != "" {
			detail += "\n  - command: `" + commandText + "`"
		}
		evidence := strings.TrimSpace(result.StderrExcerpt)
		if evidence == "" {
			evidence = strings.TrimSpace(result.StdoutExcerpt)
		}
		if evidence != "" {
			detail += "\n  - evidence: " + evidence
		}
		lines = append(lines, detail)
	}
	lines = append(lines, "", "Please fix the failing verification command(s) and rerun the post-batch verification path.")
	return strings.Join(lines, "\n")
}

func RecommendedPostBatchFollowUpIssue(repo string, verification VerificationResult, touchedPRs []string) VerificationFollowUpIssue {
	name := firstFailedVerificationName(verification.Commands)
	return VerificationFollowUpIssue{
		Status: "recommended",
		FollowUpIssueRequest: FollowUpIssueRequest{
			Title: "Post-batch verification failed: " + name,
			Body:  FormatPostBatchVerificationIssueBody(repo, verification, touchedPRs),
		},
	}
}

func firstFailedVerificationName(results []VerificationCommandResult) string {
	for _, result := range results {
		if strings.EqualFold(strings.TrimSpace(result.Status), StatusFailed) {
			if name := strings.TrimSpace(result.Name); name != "" {
				return name
			}
			break
		}
	}
	return "verification"
}

func fallbackVerificationSummary(result VerificationResult) string {
	if summary := strings.TrimSpace(result.Summary); summary != "" {
		return summary
	}
	if status := strings.TrimSpace(result.Status); status != "" {
		return status
	}
	return "post-batch verification failed"
}

func fallbackVerificationNextAction(result VerificationResult) string {
	if nextAction := strings.TrimSpace(result.NextAction); nextAction != "" {
		return nextAction
	}
	return "inspect_verification_failures"
}
