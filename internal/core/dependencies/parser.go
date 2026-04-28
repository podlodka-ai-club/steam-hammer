package dependencies

import (
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
)

const (
	TrackerGitHub                   = "github"
	TrackerJira                     = "jira"
	OrchestrationDependenciesMarker = "<!-- orchestration-dependencies:v1 -->"
)

var (
	autonomousDependencyLineRE = regexp.MustCompile(`(?im)^\s*(?:[-*]\s*)?(?:depends on|blocked by)\s*:?\s*(.+)$`)
	githubIssueReferenceRE     = regexp.MustCompile(`(?:^|[^A-Za-z0-9])#(\d+)\b`)
	jiraIssueKeyRE             = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]*-[0-9]+$`)
	fencedJSONObjectRE         = regexp.MustCompile("(?is)```(?:json)?\\s*(\\{.*?\\})\\s*```")
)

// ParseInput defines the Python-compatible dependency parsing boundary that the
// future Go orchestration core can reuse without pulling in tracker adapters.
type ParseInput struct {
	Tracker  string
	SelfRef  string
	Body     string
	Comments []string
}

// ParseIssueReferences mirrors the current Python dependency parser and keeps
// refs normalized as strings so the future Go core can remain tracker-agnostic.
func ParseIssueReferences(input ParseInput) []string {
	tracker := normalizeTracker(input.Tracker)
	selfRef := strings.TrimSpace(input.SelfRef)
	refs := make([]string, 0)
	seen := make(map[string]struct{})

	textSources := make([]string, 0, 1+len(input.Comments))
	textSources = append(textSources, input.Body)
	textSources = append(textSources, input.Comments...)

	for _, text := range textSources {
		for _, ref := range dependencyRefsFromMarkerPayload(text, tracker) {
			if ref == "" || ref == selfRef {
				continue
			}
			if _, ok := seen[ref]; ok {
				continue
			}
			seen[ref] = struct{}{}
			refs = append(refs, ref)
		}

		matches := autonomousDependencyLineRE.FindAllStringSubmatch(text, -1)
		for _, match := range matches {
			if len(match) < 2 {
				continue
			}
			for _, ref := range extractIssueReferencesFromText(match[1], tracker) {
				if ref == "" || ref == selfRef {
					continue
				}
				if _, ok := seen[ref]; ok {
					continue
				}
				seen[ref] = struct{}{}
				refs = append(refs, ref)
			}
		}
	}

	return refs
}

func normalizeTracker(tracker string) string {
	if strings.EqualFold(strings.TrimSpace(tracker), TrackerJira) {
		return TrackerJira
	}
	return TrackerGitHub
}

func extractIssueReferencesFromText(raw string, tracker string) []string {
	if tracker == TrackerJira {
		candidate := strings.ToUpper(raw)
		if jiraIssueKeyRE.MatchString(candidate) {
			return []string{candidate}
		}
		return nil
	}

	matches := githubIssueReferenceRE.FindAllStringSubmatch(raw, -1)
	refs := make([]string, 0, len(matches))
	seen := make(map[string]struct{}, len(matches))
	for _, match := range matches {
		if len(match) < 2 {
			continue
		}
		ref := normalizeGitHubIssueNumber(match[1])
		if ref == "" {
			continue
		}
		if _, ok := seen[ref]; ok {
			continue
		}
		seen[ref] = struct{}{}
		refs = append(refs, ref)
	}
	return refs
}

func dependencyRefsFromMarkerPayload(body string, tracker string) []string {
	idx := strings.Index(body, OrchestrationDependenciesMarker)
	if idx < 0 {
		return nil
	}

	afterMarker := strings.TrimSpace(body[idx+len(OrchestrationDependenciesMarker):])
	if afterMarker == "" {
		return nil
	}

	candidates := fencedJSONObjectRE.FindAllStringSubmatch(afterMarker, -1)
	if len(candidates) == 0 {
		payload, ok := firstJSONObject(afterMarker)
		if !ok {
			return nil
		}
		return mergeDependencyPayloadRefs(payload, tracker)
	}

	for _, candidate := range candidates {
		if len(candidate) < 2 {
			continue
		}
		payload, ok := firstJSONObject(candidate[1])
		if !ok {
			continue
		}
		return mergeDependencyPayloadRefs(payload, tracker)
	}

	return nil
}

func firstJSONObject(raw string) (map[string]any, bool) {
	start := strings.Index(raw, "{")
	if start < 0 {
		return nil, false
	}

	var payload map[string]any
	decoder := json.NewDecoder(strings.NewReader(raw[start:]))
	if err := decoder.Decode(&payload); err != nil {
		return nil, false
	}
	if payload == nil {
		return nil, false
	}
	return payload, true
}

func mergeDependencyPayloadRefs(payload map[string]any, tracker string) []string {
	dependsOn := normalizeDependencyRefs(payload["depends_on"], tracker)
	blockedBy := normalizeDependencyRefs(payload["blocked_by"], tracker)
	if len(blockedBy) == 0 {
		return dependsOn
	}

	seen := make(map[string]struct{}, len(dependsOn))
	merged := append([]string(nil), dependsOn...)
	for _, ref := range dependsOn {
		seen[ref] = struct{}{}
	}
	for _, ref := range blockedBy {
		if _, ok := seen[ref]; ok {
			continue
		}
		seen[ref] = struct{}{}
		merged = append(merged, ref)
	}
	return merged
}

func normalizeDependencyRefs(rawValues any, tracker string) []string {
	values, ok := rawValues.([]any)
	if !ok {
		return nil
	}

	refs := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, raw := range values {
		ref, ok := normalizeIssueRef(raw, tracker)
		if !ok {
			continue
		}
		if _, exists := seen[ref]; exists {
			continue
		}
		seen[ref] = struct{}{}
		refs = append(refs, ref)
	}
	return refs
}

func normalizeIssueRef(value any, tracker string) (string, bool) {
	if tracker == TrackerJira {
		text, ok := value.(string)
		if !ok {
			return "", false
		}
		text = strings.TrimSpace(text)
		if !jiraIssueKeyRE.MatchString(text) {
			return "", false
		}
		return text, true
	}

	return normalizeGitHubIssueValue(value)
}

func normalizeGitHubIssueValue(value any) (string, bool) {
	switch v := value.(type) {
	case float64:
		if v <= 0 || v != float64(int64(v)) {
			return "", false
		}
		return strconv.FormatInt(int64(v), 10), true
	case string:
		return normalizeGitHubIssueNumber(v), normalizeGitHubIssueNumber(v) != ""
	default:
		return "", false
	}
}

func normalizeGitHubIssueNumber(raw string) string {
	text := strings.TrimSpace(raw)
	if text == "" {
		return ""
	}
	number, err := strconv.Atoi(text)
	if err != nil || number <= 0 {
		return ""
	}
	return strconv.Itoa(number)
}
