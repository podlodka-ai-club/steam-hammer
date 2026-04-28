package dependencies

import (
	"reflect"
	"testing"
)

func TestParseIssueReferencesGitHubSupportsBodyCommentsAndMarker(t *testing.T) {
	refs := ParseIssueReferences(ParseInput{
		Tracker: TrackerGitHub,
		SelfRef: "158",
		Body:    "Depends on #156\n\n<!-- orchestration-dependencies:v1 -->\n```json\n{\"blocked_by\":[157, 156]}\n```",
		Comments: []string{
			"Blocked by #159",
			"No dependency here",
		},
	})

	want := []string{"157", "156", "159"}
	if !reflect.DeepEqual(refs, want) {
		t.Fatalf("ParseIssueReferences() = %v, want %v", refs, want)
	}
}

func TestParseIssueReferencesJiraSupportsMarkerAndComments(t *testing.T) {
	refs := ParseIssueReferences(ParseInput{
		Tracker: TrackerJira,
		SelfRef: "PROJ-44",
		Body:    "Blocked by PROJ-42\n\n<!-- orchestration-dependencies:v1 -->\n```json\n{\"depends_on\":[\"PROJ-41\"],\"blocked_by\":[\"PROJ-42\",\"PROJ-43\"]}\n```",
		Comments: []string{
			"Depends on PROJ-45",
		},
	})

	want := []string{"PROJ-41", "PROJ-42", "PROJ-43", "PROJ-45"}
	if !reflect.DeepEqual(refs, want) {
		t.Fatalf("ParseIssueReferences() = %v, want %v", refs, want)
	}
}

func TestParseIssueReferencesSkipsMalformedMarkerUntilValidPayload(t *testing.T) {
	refs := ParseIssueReferences(ParseInput{
		Tracker: TrackerGitHub,
		SelfRef: "200",
		Body: "<!-- orchestration-dependencies:v1 -->\n```json\n{not-json}\n```\n" +
			"```json\n{\"depends_on\":[201],\"blocked_by\":[202,201]}\n```",
	})

	want := []string{"201", "202"}
	if !reflect.DeepEqual(refs, want) {
		t.Fatalf("ParseIssueReferences() = %v, want %v", refs, want)
	}
}
