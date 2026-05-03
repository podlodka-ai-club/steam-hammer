package orchestration

import (
	"encoding/json"
	"strings"
)

const (
	GroomingStatusNotRequired    = "not-required"
	GroomingStatusInProgress     = "in-progress"
	GroomingStatusQuestionsReady = "questions-ready"
	GroomingStatusPlanReady      = "plan-ready"
	GroomingStatusApproved       = "approved"
	GroomingStatusBlocked        = "blocked"
)

// GroomingSummary is the stable machine-readable task grooming payload kept in
// tracker comments. Fields intentionally mirror the human summary sections.
type GroomingSummary struct {
	Status             string   `json:"status,omitempty"`
	Goal               string   `json:"goal,omitempty"`
	NonGoals           []string `json:"non_goals,omitempty"`
	Assumptions        []string `json:"assumptions,omitempty"`
	Risks              []string `json:"risks,omitempty"`
	Dependencies       []string `json:"dependencies,omitempty"`
	AcceptanceCriteria []string `json:"acceptance_criteria,omitempty"`
	TouchedAreas       []string `json:"touched_areas,omitempty"`
	ImplementationPlan []string `json:"implementation_plan,omitempty"`
	ValidationPlan     []string `json:"validation_plan,omitempty"`
}

func BuildGroomingComment(summary GroomingSummary) (string, error) {
	summary.Status = NormalizeGroomingStatus(summary.Status)
	status := summary.Status
	if status == "" {
		status = GroomingStatusInProgress
		summary.Status = status
	}

	payload, err := json.MarshalIndent(summary, "", "  ")
	if err != nil {
		return "", err
	}

	lines := []string{
		"## Grooming Summary",
		"",
		"Status: " + status,
	}
	appendSection := func(title string, values []string) {
		lines = append(lines, "", title+":")
		if len(values) == 0 {
			lines = append(lines, "- None recorded")
			return
		}
		for _, value := range values {
			value = strings.TrimSpace(value)
			if value != "" {
				lines = append(lines, "- "+value)
			}
		}
		if strings.HasSuffix(lines[len(lines)-1], ":") {
			lines = append(lines, "- None recorded")
		}
	}

	goal := strings.TrimSpace(summary.Goal)
	if goal == "" {
		goal = "None recorded"
	}
	lines = append(lines, "", "Goal: "+goal)
	appendSection("Non-goals", summary.NonGoals)
	appendSection("Assumptions", summary.Assumptions)
	appendSection("Risks", summary.Risks)
	appendSection("Dependencies", summary.Dependencies)
	appendSection("Acceptance criteria", summary.AcceptanceCriteria)
	appendSection("Touched areas", summary.TouchedAreas)
	appendSection("Implementation plan", summary.ImplementationPlan)
	appendSection("Validation plan", summary.ValidationPlan)
	lines = append(lines, "", OrchestrationGroomingMarker, "```json", string(payload), "```")

	return strings.Join(lines, "\n"), nil
}

func ParseGroomingCommentBody(body string) (*GroomingSummary, error) {
	payload, _, err := parseGroomingCommentBody(body)
	if err != nil || payload == nil {
		return nil, err
	}
	return payload, nil
}

func SelectLatestParseableGroomingComment(comments []TrackerComment, sourceLabel string) (*ParsedTrackerComment[GroomingSummary], []string) {
	return buildLatestParseableComment(
		comments,
		sourceLabel,
		"grooming",
		func(body string) (*GroomingSummary, string, error) {
			return parseGroomingCommentBody(body)
		},
	)
}

func parseGroomingCommentBody(body string) (*GroomingSummary, string, error) {
	raw, err := parseMarkedJSONObject(body, OrchestrationGroomingMarker, "unable to parse grooming payload")
	if err != nil || raw == nil {
		return nil, "", err
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, "", err
	}
	var summary GroomingSummary
	if err := json.Unmarshal(encoded, &summary); err != nil {
		return nil, "", err
	}
	summary.Status = NormalizeGroomingStatus(summary.Status)
	return &summary, summary.Status, nil
}

func NormalizeGroomingStatus(status string) string {
	normalized := strings.TrimSpace(strings.ToLower(status))
	normalized = strings.ReplaceAll(normalized, "_", "-")
	normalized = strings.Join(strings.Fields(normalized), "-")
	switch normalized {
	case "not-required", "not-needed", "none":
		return GroomingStatusNotRequired
	case "in-progress", "started", "active":
		return GroomingStatusInProgress
	case "questions-ready", "question-ready", "needs-questions":
		return GroomingStatusQuestionsReady
	case "plan-ready", "planned":
		return GroomingStatusPlanReady
	case "approved":
		return GroomingStatusApproved
	case "blocked":
		return GroomingStatusBlocked
	default:
		return normalized
	}
}
