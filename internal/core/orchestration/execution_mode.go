package orchestration

import "fmt"

const (
	ExecutionModeIssueFlow = "issue-flow"
	ExecutionModePRReview  = "pr-review"
	ExecutionModeSkip      = "skip"
	TaskTypePR             = "pr"
)

type LinkedPullRequest struct {
	Number int
}

type ExecutionModeDecision struct {
	Mode   string
	Reason string
}

func ChooseExecutionMode(issueNumber int, linkedOpenPR *LinkedPullRequest, forceIssueFlow bool, recoveredState *TrackedState, clarificationAnswer map[string]any) ExecutionModeDecision {
	recoveredStatus := ""
	recoveredTaskType := ""
	if recoveredState != nil {
		recoveredStatus = optionalString(recoveredState.Status)
		recoveredTaskType = optionalString(recoveredState.TaskType)
	}

	if recoveredStatus == StatusWaitingForAuthor && clarificationAnswer != nil {
		if linkedOpenPR != nil && recoveredTaskType == TaskTypePR {
			return ExecutionModeDecision{
				Mode:   ExecutionModePRReview,
				Reason: fmt.Sprintf("recovered waiting-for-author state has a newer author answer for linked PR #%d", linkedOpenPR.Number),
			}
		}
		return ExecutionModeDecision{Mode: ExecutionModeIssueFlow, Reason: "recovered waiting-for-author state has a newer author answer"}
	}

	if recoveredStatus == StatusWaitingForAuthor || recoveredStatus == StatusBlocked {
		return ExecutionModeDecision{
			Mode:   ExecutionModeSkip,
			Reason: fmt.Sprintf("recovered orchestration state is %s; skipping until explicitly resumed", recoveredStatus),
		}
	}

	if forceIssueFlow {
		return ExecutionModeDecision{Mode: ExecutionModeIssueFlow, Reason: "--force-issue-flow is set"}
	}

	if linkedOpenPR != nil {
		switch recoveredStatus {
		case StatusWaitingForCI, StatusReadyToMerge:
			return ExecutionModeDecision{
				Mode:   ExecutionModePRReview,
				Reason: fmt.Sprintf("recovered orchestration state is %s and linked open PR #%d exists", recoveredStatus, linkedOpenPR.Number),
			}
		case StatusReadyForReview:
			return ExecutionModeDecision{
				Mode:   ExecutionModePRReview,
				Reason: fmt.Sprintf("recovered orchestration state is ready-for-review and linked open PR #%d exists", linkedOpenPR.Number),
			}
		}
	}

	if linkedOpenPR == nil {
		return ExecutionModeDecision{Mode: ExecutionModeIssueFlow, Reason: fmt.Sprintf("no open PR linked to issue #%d", issueNumber)}
	}

	return ExecutionModeDecision{Mode: ExecutionModePRReview, Reason: fmt.Sprintf("found linked open PR #%d", linkedOpenPR.Number)}
}
