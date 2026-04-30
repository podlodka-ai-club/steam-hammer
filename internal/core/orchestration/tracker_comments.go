package orchestration

import (
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

const (
	OrchestrationStateMarker         = "<!-- orchestration-state:v1 -->"
	OrchestrationClaimMarker         = "<!-- orchestration-claim:v1 -->"
	OrchestrationDecompositionMarker = "<!-- orchestration-decomposition:v1 -->"
)

var fencedJSONObjectRE = regexp.MustCompile("(?s)```(?:json)?\\s*(\\{.*?\\})\\s*```")

type TrackerComment struct {
	ID        int64
	CreatedAt string
	HTMLURL   string
	Body      string
}

type ParsedTrackerComment[T any] struct {
	Source    string
	CreatedAt string
	URL       string
	CommentID int64
	Payload   T
	Status    string
}

func ParseOrchestrationStateCommentBody(body string) (*TrackedState, error) {
	payload, _, err := parseTrackedStateCommentBody(body)
	if err != nil || payload == nil {
		return nil, err
	}
	return payload, nil
}

func SelectLatestParseableOrchestrationState(comments []TrackerComment, sourceLabel string) (*ParsedTrackerComment[TrackedState], []string) {
	return buildLatestParseableComment(
		comments,
		sourceLabel,
		"orchestration state",
		func(body string) (*TrackedState, string, error) {
			return parseTrackedStateCommentBody(body)
		},
	)
}

func ParseOrchestrationClaimCommentBody(body string) (map[string]any, error) {
	return parseMarkedJSONObject(body, OrchestrationClaimMarker, "unable to parse claim payload")
}

func SelectLatestParseableOrchestrationClaim(comments []TrackerComment, sourceLabel string) (*ParsedTrackerComment[map[string]any], []string) {
	return buildLatestParseableComment(
		comments,
		sourceLabel,
		"orchestration claim",
		func(body string) (*map[string]any, string, error) {
			payload, err := ParseOrchestrationClaimCommentBody(body)
			if payload == nil || err != nil {
				return nil, "", err
			}
			return &payload, normalizePayloadStatus(payload, "status"), nil
		},
	)
}

func ParseDecompositionPlanCommentBody(body string) (map[string]any, error) {
	return parseMarkedJSONObject(body, OrchestrationDecompositionMarker, "unable to parse decomposition payload")
}

func SelectLatestParseableDecompositionPlan(comments []TrackerComment, sourceLabel string) (*ParsedTrackerComment[map[string]any], []string) {
	return buildLatestParseableComment(
		comments,
		sourceLabel,
		"decomposition",
		func(body string) (*map[string]any, string, error) {
			payload, err := ParseDecompositionPlanCommentBody(body)
			if payload == nil || err != nil {
				return nil, "", err
			}
			return &payload, normalizePayloadStatus(payload, "status"), nil
		},
	)
}

func parseTrackedStateCommentBody(body string) (*TrackedState, string, error) {
	raw, err := parseMarkedJSONObject(body, OrchestrationStateMarker, "unable to parse state payload")
	if err != nil || raw == nil {
		return nil, "", err
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, "", err
	}
	var decoded struct {
		TrackedState
		LegacyState string `json:"state,omitempty"`
	}
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		return nil, "", err
	}
	status := strings.ToLower(strings.TrimSpace(decoded.Status))
	if status == "" {
		status = strings.ToLower(strings.TrimSpace(decoded.LegacyState))
	}
	return &decoded.TrackedState, status, nil
}

func parseMarkedJSONObject(body, marker, missingError string) (map[string]any, error) {
	if !strings.Contains(body, marker) {
		return nil, nil
	}
	afterMarker := strings.TrimSpace(strings.SplitN(body, marker, 2)[1])
	if afterMarker == "" {
		return nil, errors.New("marker found but payload is empty")
	}

	matches := fencedJSONObjectRE.FindAllStringSubmatch(afterMarker, -1)
	candidates := make([]string, 0, len(matches))
	for _, match := range matches {
		if len(match) > 1 {
			candidates = append(candidates, match[1])
		}
	}
	if len(candidates) == 0 {
		candidates = append(candidates, afterMarker)
	}

	var parseErr error
	for _, candidate := range candidates {
		payload, err := firstJSONObject(candidate)
		if err == nil {
			return payload, nil
		}
		parseErr = err
	}
	if parseErr != nil {
		return nil, parseErr
	}
	return nil, errors.New(missingError)
}

func firstJSONObject(raw string) (map[string]any, error) {
	start := strings.Index(raw, "{")
	if start < 0 {
		return nil, errors.New("payload is missing JSON object")
	}
	var payload map[string]any
	if err := json.NewDecoder(strings.NewReader(raw[start:])).Decode(&payload); err != nil {
		return nil, err
	}
	if payload == nil {
		return nil, errors.New("payload JSON must be an object")
	}
	return payload, nil
}

func normalizePayloadStatus(payload map[string]any, key string) string {
	if payload == nil {
		return ""
	}
	value, _ := payload[key].(string)
	return strings.ToLower(strings.TrimSpace(value))
}

func buildLatestParseableComment[T any](
	comments []TrackerComment,
	sourceLabel, commentKind string,
	parse func(body string) (*T, string, error),
) (*ParsedTrackerComment[T], []string) {
	var latest *ParsedTrackerComment[T]
	warnings := make([]string, 0)
	for _, comment := range comments {
		payload, status, err := parse(comment.Body)
		if payload == nil {
			if err != nil {
				createdAt := strings.TrimSpace(comment.CreatedAt)
				if createdAt == "" {
					createdAt = "unknown-time"
				}
				context := ""
				if url := strings.TrimSpace(comment.HTMLURL); url != "" {
					context = " at " + url
				}
				warnings = append(warnings, fmt.Sprintf("ignoring malformed %s comment in %s (%s)%s: %v", commentKind, sourceLabel, createdAt, context, err))
			}
			continue
		}

		candidate := &ParsedTrackerComment[T]{
			Source:    sourceLabel,
			CreatedAt: comment.CreatedAt,
			URL:       comment.HTMLURL,
			CommentID: comment.ID,
			Payload:   *payload,
			Status:    status,
		}
		if latest == nil || comment.CreatedAt >= latest.CreatedAt {
			latest = candidate
		}
	}
	return latest, warnings
}
