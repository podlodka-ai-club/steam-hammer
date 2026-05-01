package orchestration

import "strings"

const (
	CIFailureKindTransient = "transient"
	CIFailureKindReal      = "real"

	CIRecoveryActionRetryTransientFailure = "retry_ci_after_transient_failure"
)

type CIFailureClassification struct {
	Kind   string `json:"kind,omitempty"`
	Reason string `json:"reason,omitempty"`
}

type CIFailingCheckDiagnostic struct {
	Name           string                  `json:"name,omitempty"`
	URL            string                  `json:"url,omitempty"`
	Classification CIFailureClassification `json:"classification,omitempty"`
	LogExcerpt     string                  `json:"log_excerpt,omitempty"`
}

type CIDiagnostics struct {
	OverallClassification string                     `json:"overall_classification,omitempty"`
	FailingChecks         []CIFailingCheckDiagnostic `json:"failing_checks,omitempty"`
}

type CIRecoveryDecision struct {
	Transient  bool
	NextAction string
	Summary    string
}

func FormatCIDiagnosticsSummary(ciDiagnostics CIDiagnostics) string {
	if len(ciDiagnostics.FailingChecks) == 0 {
		return "No CI diagnostics available"
	}

	parts := make([]string, 0, len(ciDiagnostics.FailingChecks))
	for _, item := range ciDiagnostics.FailingChecks {
		name := optionalString(item.Name)
		if name == "" {
			name = "unknown-check"
		}
		kind := optionalString(item.Classification.Kind)
		if kind == "" {
			kind = CIFailureKindReal
		}
		reason := optionalString(item.Classification.Reason)
		if reason == "" {
			reason = "unspecified reason"
		}
		parts = append(parts, name+": "+kind+" ("+reason+")")
	}
	return strings.Join(parts, "; ")
}

func EvaluateCIRecovery(ciDiagnostics CIDiagnostics) CIRecoveryDecision {
	if strings.EqualFold(strings.TrimSpace(ciDiagnostics.OverallClassification), CIFailureKindTransient) && len(ciDiagnostics.FailingChecks) > 0 {
		return CIRecoveryDecision{
			Transient:  true,
			NextAction: CIRecoveryActionRetryTransientFailure,
			Summary:    FormatCIDiagnosticsSummary(ciDiagnostics),
		}
	}
	return CIRecoveryDecision{
		Transient:  false,
		NextAction: NextActionInspectFailingCIChecks,
		Summary:    FormatCIDiagnosticsSummary(ciDiagnostics),
	}

}
