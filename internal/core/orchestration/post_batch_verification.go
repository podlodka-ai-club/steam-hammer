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

type PostBatchVerificationContext struct {
	SessionPath  string
	BatchIndex   int
	TotalBatches int
}

func FormatPostBatchVerificationIssueBody(repo string, verification VerificationResult, touchedPRs []string) string {
	return FormatFocusedPostBatchVerificationIssueBody(repo, verification, nil, PostBatchVerificationContext{}, touchedPRs)
}

func FormatFocusedPostBatchVerificationIssueBody(repo string, verification VerificationResult, failedCheck *VerificationCommandResult, context PostBatchVerificationContext, touchedPRs []string) string {
	lines := []string{
		"Automated post-batch verification detected a repository regression.",
		"",
		"Repository: " + strings.TrimSpace(repo),
		"Result: " + fallbackVerificationSummary(verification),
		"Next action: " + humanizeToken(fallbackVerificationNextAction(verification)),
	}
	if context.BatchIndex > 0 || context.TotalBatches > 0 || strings.TrimSpace(context.SessionPath) != "" {
		lines = append(lines, "", "Affected batch/session:")
		if context.BatchIndex > 0 || context.TotalBatches > 0 {
			lines = append(lines, "- batch: "+formatBatchRef(context.BatchIndex, context.TotalBatches))
		}
		if sessionPath := strings.TrimSpace(context.SessionPath); sessionPath != "" {
			lines = append(lines, "- session: `"+sessionPath+"`")
		}
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
	commands := verification.Commands
	if failedCheck != nil {
		commands = []VerificationCommandResult{*failedCheck}
	}
	lines = append(lines, "", "Failed verification check:")
	for _, result := range commands {
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
		} else if strings.EqualFold(strings.TrimSpace(result.Status), StatusFailed) {
			detail += "\n  - evidence: no log excerpt was captured; rerun the command locally or inspect the session logs"
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

func RecommendedPostBatchFollowUpIssues(repo string, verification VerificationResult, context PostBatchVerificationContext, touchedPRs []string) []VerificationFollowUpIssue {
	seen := map[string]struct{}{}
	issues := make([]VerificationFollowUpIssue, 0)
	for _, result := range verification.Commands {
		if !strings.EqualFold(strings.TrimSpace(result.Status), StatusFailed) {
			continue
		}
		key := PostBatchVerificationFailureKey(result)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		name := strings.TrimSpace(result.Name)
		if name == "" {
			name = "verification"
		}
		body := FormatFocusedPostBatchVerificationIssueBody(repo, verification, &result, context, touchedPRs)
		marker := PostBatchVerificationIssueMarker(result)
		if marker != "" && !strings.Contains(body, marker) {
			body += "\n\n" + marker
		}
		issues = append(issues, VerificationFollowUpIssue{
			Status: "recommended",
			FollowUpIssueRequest: FollowUpIssueRequest{
				Title: "Post-batch verification failed: " + name,
				Body:  body,
			},
		})
	}
	return issues
}

func PostBatchVerificationIssueMarker(result VerificationCommandResult) string {
	key := PostBatchVerificationFailureKey(result)
	if key == "" {
		return ""
	}
	return "<!-- steam-hammer:post-batch-verification:" + key + " -->"
}

func PostBatchVerificationFailureKey(result VerificationCommandResult) string {
	value := strings.TrimSpace(result.Name)
	if value == "" {
		value = strings.TrimSpace(result.Command)
	}
	if value == "" {
		value = "verification"
	}
	value = strings.ToLower(value)
	var b strings.Builder
	lastDash := false
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			lastDash = false
			continue
		}
		if !lastDash {
			b.WriteByte('-')
			lastDash = true
		}
	}
	return strings.Trim(b.String(), "-")
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

func formatBatchRef(batchIndex, totalBatches int) string {
	if batchIndex > 0 && totalBatches > 0 {
		return itoa(batchIndex) + "/" + itoa(totalBatches)
	}
	if batchIndex > 0 {
		return itoa(batchIndex)
	}
	if totalBatches > 0 {
		return "unknown/" + itoa(totalBatches)
	}
	return "unknown"
}
