package orchestration

import (
	"encoding/json"
	"regexp"
	"strings"
)

const PRReviewOutcomeMarker = "<!-- orchestration-pr-review-outcomes:v1 -->"

var reviewFeedbackOnlyPunctuationRE = regexp.MustCompile(`^[\W_]+$`)

var reviewFeedbackActionablePatterns = []*regexp.Regexp{
	regexp.MustCompile(`\bplease\b`),
	regexp.MustCompile(`\bcan you\b`),
	regexp.MustCompile(`\bshould\b`),
	regexp.MustCompile(`\bneed(?:s|ed)?\b`),
	regexp.MustCompile(`\bmust\b`),
	regexp.MustCompile(`\bfix\b`),
	regexp.MustCompile(`\bchange\b`),
	regexp.MustCompile(`\bupdate\b`),
	regexp.MustCompile(`\brename\b`),
	regexp.MustCompile(`\badd\b`),
	regexp.MustCompile(`\bremove\b`),
	regexp.MustCompile(`\bconsider\b`),
	regexp.MustCompile(`\bavoid\b`),
	regexp.MustCompile(`\buse\b`),
	regexp.MustCompile(`\bnit\b`),
	regexp.MustCompile(`\btodo\b`),
	regexp.MustCompile(`\bfollow up\b`),
}

var nonActionableReviewFeedback = map[string]struct{}{
	"lgtm":             {},
	"looks good":       {},
	"looks good to me": {},
	"approved":         {},
	"ship it":          {},
	"thanks":           {},
	"thank you":        {},
	"great work":       {},
	"+1":               {},
	"done":             {},
}

type ReviewThreadComment struct {
	Body     string
	Path     string
	Line     int
	Outdated bool
	URL      string
	Author   string
}

type ReviewThread struct {
	Resolved bool
	Outdated bool
	Comments []ReviewThreadComment
}

type ConversationComment struct {
	Author string
	Body   string
	URL    string
}

type ReviewFeedbackItem struct {
	Type   string
	Author string
	Body   string
	State  string
	Path   string
	Line   int
	URL    string
}

type PRReviewItemOutcome struct {
	Item       int    `json:"item"`
	Status     string `json:"status,omitempty"`
	Summary    string `json:"summary,omitempty"`
	NextAction string `json:"next_action,omitempty"`
}

type PRReviewOutcomeSummary struct {
	Items []PRReviewItemOutcome `json:"items,omitempty"`
}

func ParsePRReviewOutcomeSummary(output string) *PRReviewOutcomeSummary {
	raw := strings.TrimSpace(output)
	if raw == "" || !strings.Contains(raw, PRReviewOutcomeMarker) {
		return nil
	}
	afterMarker := strings.TrimSpace(strings.SplitN(raw, PRReviewOutcomeMarker, 2)[1])
	if afterMarker == "" {
		return nil
	}
	start := strings.Index(afterMarker, "{")
	if start < 0 {
		return nil
	}
	var payload PRReviewOutcomeSummary
	if err := json.NewDecoder(strings.NewReader(afterMarker[start:])).Decode(&payload); err != nil {
		return nil
	}
	if len(payload.Items) == 0 {
		return nil
	}
	for i := range payload.Items {
		payload.Items[i].Status = strings.TrimSpace(strings.ToLower(payload.Items[i].Status))
		payload.Items[i].Summary = strings.TrimSpace(payload.Items[i].Summary)
		payload.Items[i].NextAction = strings.TrimSpace(payload.Items[i].NextAction)
	}
	return &payload
}

type ReviewFeedbackStats struct {
	ThreadsTotal              int
	ThreadsResolved           int
	ThreadsOutdated           int
	CommentsTotal             int
	CommentsOutdated          int
	CommentsEmpty             int
	CommentsPRAuthor          int
	CommentsDuplicates        int
	CommentsUsed              int
	ReviewsTotal              int
	ReviewsUsed               int
	ReviewsSuperseded         int
	ReviewsPRAuthor           int
	ReviewsEmpty              int
	ReviewsNonActionable      int
	ReviewsDuplicates         int
	ConversationTotal         int
	ConversationUsed          int
	ConversationPRAuthor      int
	ConversationEmpty         int
	ConversationNonActionable int
	ConversationDuplicates    int
}

func CanonicalReviewFeedbackText(body string) string {
	return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(body))), " ")
}

func IsActionableReviewFeedback(body string) bool {
	text := CanonicalReviewFeedbackText(body)
	if text == "" {
		return false
	}
	if reviewFeedbackOnlyPunctuationRE.MatchString(text) {
		return false
	}
	if _, ok := nonActionableReviewFeedback[text]; ok {
		return false
	}
	for _, pattern := range reviewFeedbackActionablePatterns {
		if pattern.MatchString(text) {
			return true
		}
	}
	return strings.Contains(body, "`") || strings.Contains(body, "\n")
}

func NormalizeReviewFeedback(threads []ReviewThread, reviews []PullRequestReview, conversationComments []ConversationComment, prAuthorLogin string) ([]ReviewFeedbackItem, ReviewFeedbackStats) {
	stats := ReviewFeedbackStats{}
	items := make([]ReviewFeedbackItem, 0)
	prAuthorLogin = strings.ToLower(strings.TrimSpace(prAuthorLogin))

	for _, thread := range threads {
		stats.ThreadsTotal++
		if thread.Resolved {
			stats.ThreadsResolved++
			continue
		}
		if thread.Outdated {
			stats.ThreadsOutdated++
			continue
		}
		for _, comment := range thread.Comments {
			stats.CommentsTotal++
			if comment.Outdated {
				stats.CommentsOutdated++
				continue
			}
			body := strings.TrimSpace(comment.Body)
			if body == "" {
				stats.CommentsEmpty++
				continue
			}
			author := strings.TrimSpace(comment.Author)
			if author == "" {
				author = "unknown"
			}
			if prAuthorLogin != "" && strings.EqualFold(author, prAuthorLogin) {
				stats.CommentsPRAuthor++
			}
			items = append(items, ReviewFeedbackItem{
				Type:   "review_comment",
				Author: author,
				Body:   body,
				Path:   strings.TrimSpace(comment.Path),
				Line:   comment.Line,
				URL:    strings.TrimSpace(comment.URL),
			})
			stats.CommentsUsed++
		}
	}

	latestByAuthor := latestReviewFeedbackByAuthor(reviews)
	stats.ReviewsTotal = len(reviews)
	if len(latestByAuthor) < stats.ReviewsTotal {
		stats.ReviewsSuperseded = stats.ReviewsTotal - len(latestByAuthor)
	}
	for authorKey, review := range latestByAuthor {
		author := strings.TrimSpace(review.AuthorLogin)
		if author == "" {
			author = "unknown"
		}
		if prAuthorLogin != "" && authorKey == prAuthorLogin {
			stats.ReviewsPRAuthor++
		}
		body := strings.TrimSpace(review.Body)
		if body == "" {
			stats.ReviewsEmpty++
			continue
		}
		state := strings.ToUpper(strings.TrimSpace(review.State))
		if state != "CHANGES_REQUESTED" && state != "COMMENTED" && state != "APPROVED" {
			continue
		}
		if (state == "COMMENTED" || state == "APPROVED") && !IsActionableReviewFeedback(body) {
			stats.ReviewsNonActionable++
			continue
		}
		items = append(items, ReviewFeedbackItem{
			Type:   "review_summary",
			Author: author,
			Body:   body,
			State:  state,
			URL:    strings.TrimSpace(review.URL),
		})
		stats.ReviewsUsed++
	}

	for _, comment := range conversationComments {
		stats.ConversationTotal++
		body := strings.TrimSpace(comment.Body)
		if body == "" {
			stats.ConversationEmpty++
			continue
		}
		author := strings.TrimSpace(comment.Author)
		if author == "" {
			author = "unknown"
		}
		if prAuthorLogin != "" && strings.EqualFold(author, prAuthorLogin) {
			stats.ConversationPRAuthor++
		}
		if !IsActionableReviewFeedback(body) {
			stats.ConversationNonActionable++
			continue
		}
		items = append(items, ReviewFeedbackItem{
			Type:   "conversation_comment",
			Author: author,
			Body:   body,
			URL:    strings.TrimSpace(comment.URL),
		})
		stats.ConversationUsed++
	}

	return dedupeReviewFeedback(items, stats)
}

func FormatReviewFeedbackStats(stats ReviewFeedbackStats) string {
	return "threads=" +
		"total:" + itoa(stats.ThreadsTotal) +
		" excluded(resolved:" + itoa(stats.ThreadsResolved) +
		", outdated:" + itoa(stats.ThreadsOutdated) + ")" +
		"; inline=total:" + itoa(stats.CommentsTotal) +
		" included:" + itoa(stats.CommentsUsed) +
		"(from_pr_author:" + itoa(stats.CommentsPRAuthor) + ")" +
		" excluded(outdated:" + itoa(stats.CommentsOutdated) +
		", empty:" + itoa(stats.CommentsEmpty) +
		", duplicates:" + itoa(stats.CommentsDuplicates) + ")" +
		"; review_summaries=total:" + itoa(stats.ReviewsTotal) +
		" included:" + itoa(stats.ReviewsUsed) +
		"(from_pr_author:" + itoa(stats.ReviewsPRAuthor) + ")" +
		" excluded(superseded:" + itoa(stats.ReviewsSuperseded) +
		", empty:" + itoa(stats.ReviewsEmpty) +
		", non_actionable:" + itoa(stats.ReviewsNonActionable) +
		", duplicates:" + itoa(stats.ReviewsDuplicates) + ")" +
		"; conversation=total:" + itoa(stats.ConversationTotal) +
		" included:" + itoa(stats.ConversationUsed) +
		"(from_pr_author:" + itoa(stats.ConversationPRAuthor) + ")" +
		" excluded(empty:" + itoa(stats.ConversationEmpty) +
		", non_actionable:" + itoa(stats.ConversationNonActionable) +
		", duplicates:" + itoa(stats.ConversationDuplicates) + ")"
}

func latestReviewFeedbackByAuthor(reviews []PullRequestReview) map[string]PullRequestReview {
	latest := make(map[string]PullRequestReview, len(reviews))
	for _, review := range reviews {
		author := strings.ToLower(strings.TrimSpace(review.AuthorLogin))
		if author == "" {
			author = "unknown"
		}
		existing, ok := latest[author]
		if !ok || strings.TrimSpace(review.SubmittedAt) >= strings.TrimSpace(existing.SubmittedAt) {
			latest[author] = review
		}
	}
	return latest
}

func dedupeReviewFeedback(items []ReviewFeedbackItem, stats ReviewFeedbackStats) ([]ReviewFeedbackItem, ReviewFeedbackStats) {
	seen := make(map[string]struct{}, len(items))
	deduped := make([]ReviewFeedbackItem, 0, len(items))
	for _, item := range items {
		key := strings.ToLower(strings.TrimSpace(item.Author)) + "\x00" + CanonicalReviewFeedbackText(item.Body)
		if _, ok := seen[key]; ok {
			switch item.Type {
			case "review_comment":
				stats.CommentsDuplicates++
			case "review_summary":
				stats.ReviewsDuplicates++
			case "conversation_comment":
				stats.ConversationDuplicates++
			}
			continue
		}
		seen[key] = struct{}{}
		deduped = append(deduped, item)
	}
	return deduped, stats
}
