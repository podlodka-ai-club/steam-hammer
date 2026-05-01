package agentexec

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

var (
	tokenLinePatterns = []tokenPattern{
		{regexp.MustCompile(`(?i)\binput(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)`), "tokens_in"},
		{regexp.MustCompile(`(?i)\bin(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)`), "tokens_in"},
		{regexp.MustCompile(`(?i)\boutput(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)`), "tokens_out"},
		{regexp.MustCompile(`(?i)\bout(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)`), "tokens_out"},
	}
	combinedTokenLinePatterns = []*regexp.Regexp{
		regexp.MustCompile(`(?i)~?\s*([0-9][0-9, _]*)\s+in\s*/\s*~?\s*([0-9][0-9, _]*)\s+out`),
	}
	costLinePattern = regexp.MustCompile(`\$([0-9]+(?:\.[0-9]{1,4})?)`)
)

type Request struct {
	Runner              string
	Prompt              string
	Agent               string
	Model               string
	ImagePaths          []string
	OpenCodeAutoApprove bool
	Cwd                 string
	Timeout             time.Duration
	IdleTimeout         time.Duration
	TrackTokens         bool
	TokenBudget         int
	CostBudgetUSD       float64
	Stdout              io.Writer
	Stderr              io.Writer
}

type Result struct {
	Command              []string
	ExitCode             int
	Output               string
	ClarificationRequest map[string]any
	Stats                Stats
}

type Stats struct {
	ElapsedSeconds int      `json:"elapsed_seconds,omitempty"`
	Elapsed        string   `json:"elapsed,omitempty"`
	TokensIn       *int     `json:"tokens_in,omitempty"`
	TokensOut      *int     `json:"tokens_out,omitempty"`
	TokensTotal    *int     `json:"tokens_total,omitempty"`
	CostUSD        *float64 `json:"cost_usd,omitempty"`
}

type TimeoutError struct {
	Timeout   time.Duration
	ItemLabel string
}

func (e *TimeoutError) Error() string {
	seconds := int(e.Timeout.Seconds())
	return fmt.Sprintf(
		"Agent timed out after %ds for %s. Possible causes: waiting for interactive approval, network stall, or a long-running task. Try increasing --agent-timeout-seconds, setting --agent-idle-timeout-seconds, or using --opencode-auto-approve for OpenCode if safe in your environment.",
		seconds,
		e.ItemLabel,
	)
}

type IdleTimeoutError struct {
	Timeout   time.Duration
	ItemLabel string
}

func (e *IdleTimeoutError) Error() string {
	seconds := int(e.Timeout.Seconds())
	return fmt.Sprintf(
		"Agent produced no output for %ds on %s; aborting to avoid indefinite hang. Possible causes: waiting for interactive approval or a stuck process. Try --opencode-auto-approve (if safe) or a larger --agent-idle-timeout-seconds.",
		seconds,
		e.ItemLabel,
	)
}

type TokenBudgetExceededError struct {
	Budget    int
	Reached   int
	ItemLabel string
}

func (e *TokenBudgetExceededError) Error() string {
	return fmt.Sprintf(
		"Agent stopped: token budget of %s exceeded for %s (reached ~%s total tokens)",
		formatBudgetMessageCount(e.Budget),
		e.ItemLabel,
		formatBudgetMessageCount(e.Reached),
	)
}

type CostBudgetExceededError struct {
	Budget    float64
	Reached   float64
	ItemLabel string
}

func (e *CostBudgetExceededError) Error() string {
	return fmt.Sprintf(
		"Agent stopped: cost budget of $%.4f exceeded for %s (reached ~$%.4f)",
		e.Budget,
		e.ItemLabel,
		e.Reached,
	)
}

type tokenPattern struct {
	pattern *regexp.Regexp
	metric  string
}

type streamEvent struct {
	stream string
	line   string
	eof    bool
	err    error
}

func BuildCommand(req Request) ([]string, error) {
	runner := strings.TrimSpace(strings.ToLower(req.Runner))
	switch runner {
	case "claude":
		command := []string{"claude", "--dangerously-skip-permissions"}
		for _, imagePath := range req.ImagePaths {
			if strings.TrimSpace(imagePath) != "" {
				command = append(command, "--image", imagePath)
			}
		}
		command = append(command, "-p", req.Prompt)
		if model := strings.TrimSpace(req.Model); model != "" {
			command = append(command, "--model", model)
		}
		return command, nil
	case "opencode":
		command := []string{"opencode", "run", "--agent", strings.TrimSpace(req.Agent)}
		if strings.TrimSpace(req.Agent) == "" {
			return nil, errors.New("opencode runner requires a non-empty agent")
		}
		if model := strings.TrimSpace(req.Model); model != "" {
			command = append(command, "--model", model)
		}
		if req.OpenCodeAutoApprove {
			command = append(command, "--dangerously-skip-permissions")
		}
		command = append(command, req.Prompt)
		return command, nil
	default:
		return nil, fmt.Errorf("unsupported runner %q", req.Runner)
	}
}

func Run(ctx context.Context, itemLabel string, req Request) (*Result, error) {
	command, err := BuildCommand(req)
	if err != nil {
		return nil, err
	}

	cmd := exec.CommandContext(ctx, command[0], command[1:]...)
	cmd.Dir = req.Cwd
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("stderr pipe: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	start := time.Now()
	lastOutput := start
	trackTokens := req.TrackTokens || req.TokenBudget > 0 || req.CostBudgetUSD > 0
	result := &Result{Command: append([]string(nil), command...)}
	collector := statsCollector{}
	var output strings.Builder

	events := make(chan streamEvent, 16)
	go readStream("stdout", stdout, events)
	go readStream("stderr", stderr, events)

	eofs := 0
	for {
		if ctx.Err() != nil {
			_ = killAndWait(cmd)
			return nil, ctx.Err()
		}
		if req.Timeout > 0 && time.Since(start) > req.Timeout {
			_ = killAndWait(cmd)
			result.Output = output.String()
			result.ClarificationRequest = orchestration.LatestClarificationRequestFromAgentOutput(result.Output)
			result.Stats = collector.buildStats(time.Since(start))
			return result, &TimeoutError{Timeout: req.Timeout, ItemLabel: itemLabel}
		}
		if req.IdleTimeout > 0 && time.Since(lastOutput) > req.IdleTimeout {
			_ = killAndWait(cmd)
			result.Output = output.String()
			result.ClarificationRequest = orchestration.LatestClarificationRequestFromAgentOutput(result.Output)
			result.Stats = collector.buildStats(time.Since(start))
			return result, &IdleTimeoutError{Timeout: req.IdleTimeout, ItemLabel: itemLabel}
		}

		select {
		case <-ctx.Done():
			_ = killAndWait(cmd)
			return nil, ctx.Err()
		case event := <-events:
			if event.err != nil {
				_ = killAndWait(cmd)
				return nil, event.err
			}
			if event.eof {
				eofs++
				if eofs < 2 {
					continue
				}
				waitErr := cmd.Wait()
				result.Output = output.String()
				result.ClarificationRequest = orchestration.LatestClarificationRequestFromAgentOutput(result.Output)
				result.Stats = collector.buildStats(time.Since(start))
				if waitErr != nil {
					var exitErr *exec.ExitError
					if errors.As(waitErr, &exitErr) {
						result.ExitCode = exitErr.ExitCode()
						return result, nil
					}
					return result, waitErr
				}
				result.ExitCode = 0
				return result, nil
			}

			lastOutput = time.Now()
			output.WriteString(event.line)
			if event.stream == "stderr" {
				if req.Stderr != nil {
					_, _ = io.WriteString(req.Stderr, event.line)
				}
			} else if req.Stdout != nil {
				_, _ = io.WriteString(req.Stdout, event.line)
			}

			if trackTokens {
				collector.update(event.line)
				if req.TokenBudget > 0 {
					if total := collector.totalTokens(); total != nil && *total > req.TokenBudget {
						_ = killAndWait(cmd)
						result.Output = output.String()
						result.ClarificationRequest = orchestration.LatestClarificationRequestFromAgentOutput(result.Output)
						result.Stats = collector.buildStats(time.Since(start))
						return result, &TokenBudgetExceededError{Budget: req.TokenBudget, Reached: *total, ItemLabel: itemLabel}
					}
				}
				if req.CostBudgetUSD > 0 && collector.costUSD != nil && *collector.costUSD > req.CostBudgetUSD {
					_ = killAndWait(cmd)
					result.Output = output.String()
					result.ClarificationRequest = orchestration.LatestClarificationRequestFromAgentOutput(result.Output)
					result.Stats = collector.buildStats(time.Since(start))
					return result, &CostBudgetExceededError{Budget: req.CostBudgetUSD, Reached: *collector.costUSD, ItemLabel: itemLabel}
				}
			}
		case <-time.After(200 * time.Millisecond):
		}
	}
}

func readStream(name string, stream io.Reader, events chan<- streamEvent) {
	reader := bufio.NewReader(stream)
	for {
		line, err := reader.ReadString('\n')
		if line != "" {
			events <- streamEvent{stream: name, line: line}
		}
		if err == nil {
			continue
		}
		if errors.Is(err, io.EOF) {
			events <- streamEvent{stream: name, eof: true}
			return
		}
		events <- streamEvent{stream: name, err: err}
		return
	}
}

func killAndWait(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return err
	}
	_ = cmd.Wait()
	return nil
}

type statsCollector struct {
	tokensIn  *int
	tokensOut *int
	costUSD   *float64
}

func (c *statsCollector) update(line string) {
	for _, pattern := range combinedTokenLinePatterns {
		matches := pattern.FindStringSubmatch(line)
		if len(matches) == 3 {
			if parsed, ok := parseIntValue(matches[1]); ok {
				c.tokensIn = intPtr(parsed)
			}
			if parsed, ok := parseIntValue(matches[2]); ok {
				c.tokensOut = intPtr(parsed)
			}
			break
		}
	}

	for _, pattern := range tokenLinePatterns {
		matches := pattern.pattern.FindStringSubmatch(line)
		if len(matches) != 2 {
			continue
		}
		parsed, ok := parseIntValue(matches[1])
		if !ok {
			continue
		}
		if pattern.metric == "tokens_in" {
			c.tokensIn = intPtr(parsed)
		} else {
			c.tokensOut = intPtr(parsed)
		}
	}

	if costMatch := costLinePattern.FindString(line); costMatch != "" {
		if parsed, ok := parseCostValue(costMatch); ok {
			c.costUSD = float64Ptr(parsed)
		}
	}
}

func (c *statsCollector) totalTokens() *int {
	if c.tokensIn == nil && c.tokensOut == nil {
		return nil
	}
	total := 0
	if c.tokensIn != nil {
		total += *c.tokensIn
	}
	if c.tokensOut != nil {
		total += *c.tokensOut
	}
	return &total
}

func (c *statsCollector) buildStats(elapsed time.Duration) Stats {
	stats := Stats{
		ElapsedSeconds: int(elapsed.Seconds()),
		Elapsed:        formatElapsedDuration(elapsed),
	}
	if c.tokensIn != nil {
		value := *c.tokensIn
		stats.TokensIn = &value
	}
	if c.tokensOut != nil {
		value := *c.tokensOut
		stats.TokensOut = &value
	}
	if total := c.totalTokens(); total != nil {
		value := *total
		stats.TokensTotal = &value
	}
	if c.costUSD != nil {
		value := *c.costUSD
		stats.CostUSD = &value
	}
	return stats
}

func formatElapsedDuration(elapsed time.Duration) string {
	totalSeconds := int(elapsed.Round(time.Second).Seconds())
	if totalSeconds < 0 {
		totalSeconds = 0
	}
	minutes := totalSeconds / 60
	seconds := totalSeconds % 60
	if minutes > 0 {
		return fmt.Sprintf("%dm %ds", minutes, seconds)
	}
	return fmt.Sprintf("%ds", seconds)
}

func parseIntValue(value string) (int, bool) {
	normalized := strings.NewReplacer(",", "", " ", "", "_", "").Replace(value)
	parsed, err := strconv.Atoi(normalized)
	if err != nil {
		return 0, false
	}
	return parsed, true
}

func parseCostValue(value string) (float64, bool) {
	normalized := strings.TrimSpace(strings.ReplaceAll(value, ",", ""))
	normalized = strings.TrimLeft(normalized, "~$ ")
	if normalized == "" {
		return 0, false
	}
	parsed, err := strconv.ParseFloat(normalized, 64)
	if err != nil {
		return 0, false
	}
	return parsed, true
}

func formatBudgetMessageCount(value int) string {
	parts := strconv.Itoa(value)
	if len(parts) <= 3 {
		return parts
	}
	var chunks []string
	for len(parts) > 3 {
		chunks = append([]string{parts[len(parts)-3:]}, chunks...)
		parts = parts[:len(parts)-3]
	}
	chunks = append([]string{parts}, chunks...)
	return strings.Join(chunks, " ")
}

func intPtr(value int) *int {
	return &value
}

func float64Ptr(value float64) *float64 {
	return &value
}
