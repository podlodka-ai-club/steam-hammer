package orchestration

import (
	"encoding/json"
	"fmt"
	"strings"
)

type VerificationCommand struct {
	Name    string `json:"name,omitempty"`
	Command string `json:"command,omitempty"`
}

type VerificationCommandResult struct {
	Name          string `json:"name,omitempty"`
	Command       string `json:"command,omitempty"`
	Status        string `json:"status,omitempty"`
	ExitCode      *int   `json:"exit_code,omitempty"`
	StdoutExcerpt string `json:"stdout_excerpt,omitempty"`
	StderrExcerpt string `json:"stderr_excerpt,omitempty"`
}

type FollowUpIssueRequest struct {
	Title string `json:"title,omitempty"`
	Body  string `json:"body,omitempty"`
}

type VerificationFollowUpIssue struct {
	Status string `json:"status,omitempty"`
	FollowUpIssueRequest
	IssueNumber *int   `json:"issue_number,omitempty"`
	IssueRef    string `json:"-"`
	IssueURL    string `json:"issue_url,omitempty"`
}

func (f *VerificationFollowUpIssue) UnmarshalJSON(data []byte) error {
	type alias struct {
		Status      string      `json:"status,omitempty"`
		Title       string      `json:"title,omitempty"`
		Body        string      `json:"body,omitempty"`
		IssueNumber interface{} `json:"issue_number,omitempty"`
		IssueURL    string      `json:"issue_url,omitempty"`
	}
	var payload alias
	if err := json.Unmarshal(data, &payload); err != nil {
		return err
	}
	f.Status = payload.Status
	f.Title = payload.Title
	f.Body = payload.Body
	f.IssueURL = payload.IssueURL
	switch number := payload.IssueNumber.(type) {
	case float64:
		if number > 0 && number == float64(int(number)) {
			value := int(number)
			f.IssueNumber = &value
			f.IssueRef = "#" + itoa(value)
		}
	case string:
		f.IssueRef = strings.TrimSpace(number)
	}
	return nil
}

func (f VerificationFollowUpIssue) Request() FollowUpIssueRequest {
	return FollowUpIssueRequest{
		Title: optionalString(f.Title),
		Body:  optionalString(f.Body),
	}
}

type VerificationResult struct {
	Status        string                      `json:"status,omitempty"`
	Summary       string                      `json:"summary,omitempty"`
	Error         string                      `json:"error,omitempty"`
	NextAction    string                      `json:"next_action,omitempty"`
	Commands      []VerificationCommandResult `json:"commands,omitempty"`
	FollowUpIssue *VerificationFollowUpIssue  `json:"follow_up_issue,omitempty"`
}

func SummarizeVerificationResults(results []VerificationCommandResult) string {
	if len(results) == 0 {
		return "passed (0/0 commands)"
	}
	failedNames := make([]string, 0)
	for _, result := range results {
		if strings.TrimSpace(result.Status) == StatusFailed {
			name := strings.TrimSpace(result.Name)
			if name == "" {
				name = "command"
			}
			failedNames = append(failedNames, name)
		}
	}
	if len(failedNames) > 0 {
		passedCount := len(results) - len(failedNames)
		return fmt.Sprintf("failed (%d/%d passed; failed: %s)", passedCount, len(results), strings.Join(failedNames, ", "))
	}
	return fmt.Sprintf("passed (%d/%d commands)", len(results), len(results))
}

func (v VerificationResult) SummaryLine() string {
	summary := optionalString(v.Summary)
	if summary == "" {
		summary = optionalString(v.Status)
		if summary == "" {
			summary = "unknown"
		}
	}
	line := "Verification: " + summary
	if v.FollowUpIssue == nil {
		return line
	}
	status := optionalString(v.FollowUpIssue.Status)
	issueRef := optionalString(v.FollowUpIssue.IssueRef)
	if issueRef == "" && v.FollowUpIssue.IssueNumber != nil {
		issueRef = "#" + itoa(*v.FollowUpIssue.IssueNumber)
	}
	if status == "created" && issueRef != "" {
		return line + "; follow-up issue " + issueRef + " created"
	}
	if status != "" {
		return line + "; follow-up=" + status
	}
	return line
}

// Compatibility aliases keep the current state/session consumers stable while
// verification execution ownership moves into the Go runtime.
type VerificationVerdict = VerificationResult
type VerificationStep = VerificationCommandResult
type FollowUpIssue = VerificationFollowUpIssue
