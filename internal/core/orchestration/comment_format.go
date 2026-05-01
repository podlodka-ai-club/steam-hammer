package orchestration

import (
	"encoding/json"
	"fmt"
	"strings"
)

func BuildOrchestrationStateComment(state TrackedState) (string, error) {
	status := strings.TrimSpace(state.Status)
	if status == "" {
		status = "unknown"
	}
	taskType := strings.TrimSpace(state.TaskType)
	if taskType == "" {
		taskType = "unknown"
	}
	stage := strings.TrimSpace(state.Stage)
	if stage == "" {
		stage = "unknown"
	}
	nextAction := strings.TrimSpace(state.NextAction)
	if nextAction == "" {
		nextAction = "unknown"
	}
	payload, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return "", err
	}
	return fmt.Sprintf(
		"Orchestration state update: %s (%s, stage=%s, next=%s).\n\n%s\n```json\n%s\n```",
		status,
		taskType,
		stage,
		nextAction,
		OrchestrationStateMarker,
		payload,
	), nil
}

func BuildClarificationRequestComment(question, reason string) string {
	question = strings.TrimSpace(question)
	reason = strings.TrimSpace(reason)
	lines := []string{
		"Automation needs clarification before it can continue safely.",
		"",
		"Question: " + question,
	}
	if reason != "" && reason != question {
		lines = append(lines, "", "Why this is blocked: "+reason)
	}
	lines = append(lines, "", "Next action: reply here and rerun the orchestrator.")
	return strings.Join(lines, "\n")
}
