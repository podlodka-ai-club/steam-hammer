package agentexec

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/podlodka-ai-club/steam-hammer/internal/core/orchestration"
)

func TestBuildCommand(t *testing.T) {
	t.Run("opencode", func(t *testing.T) {
		command, err := BuildCommand(Request{
			Runner:              "opencode",
			Agent:               "build",
			Model:               "openai/gpt-5.4",
			Prompt:              "fix it",
			OpenCodeAutoApprove: true,
		})
		if err != nil {
			t.Fatalf("BuildCommand() error = %v", err)
		}
		want := []string{"opencode", "run", "--agent", "build", "--model", "openai/gpt-5.4", "--dangerously-skip-permissions", "fix it"}
		if !reflect.DeepEqual(command, want) {
			t.Fatalf("BuildCommand() = %#v, want %#v", command, want)
		}
	})

	t.Run("claude", func(t *testing.T) {
		command, err := BuildCommand(Request{
			Runner:     "claude",
			Model:      "claude-sonnet-4-5",
			Prompt:     "review it",
			ImagePaths: []string{"/tmp/a.png", "/tmp/b.png"},
		})
		if err != nil {
			t.Fatalf("BuildCommand() error = %v", err)
		}
		want := []string{"claude", "--dangerously-skip-permissions", "--image", "/tmp/a.png", "--image", "/tmp/b.png", "-p", "review it", "--model", "claude-sonnet-4-5"}
		if !reflect.DeepEqual(command, want) {
			t.Fatalf("BuildCommand() = %#v, want %#v", command, want)
		}
	})
}

func TestRunCapturesStatsAndClarificationRequest(t *testing.T) {
	binDir := t.TempDir()
	writeExecutable(t, filepath.Join(binDir, "opencode"), strings.Join([]string{
		"#!/bin/sh",
		"printf 'Input tokens: 2,000\n'",
		"printf 'Output tokens: 3_000\n'",
		"printf 'Estimated cost: $0.0421\n' >&2",
		"printf 'note\n'",
		"printf '" + orchestration.ClarificationRequestMarker + "\n'",
		"printf '{\"question\":\"Should this pause on unsafe writes?\",\"reason\":\"Needs product decision\"}\n'",
	}, "\n"))
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result, err := Run(context.Background(), "issue #248", Request{
		Runner:      "opencode",
		Agent:       "build",
		Prompt:      "fix it",
		TrackTokens: true,
	})
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if result.ExitCode != 0 {
		t.Fatalf("exit code = %d, want 0", result.ExitCode)
	}
	if result.ClarificationRequest == nil {
		t.Fatalf("clarification request = nil, want parsed payload")
	}
	if result.ClarificationRequest["question"] != "Should this pause on unsafe writes?" {
		t.Fatalf("question = %#v, want parsed payload", result.ClarificationRequest["question"])
	}
	if result.Stats.TokensIn == nil || *result.Stats.TokensIn != 2000 {
		t.Fatalf("tokens_in = %#v, want 2000", result.Stats.TokensIn)
	}
	if result.Stats.TokensOut == nil || *result.Stats.TokensOut != 3000 {
		t.Fatalf("tokens_out = %#v, want 3000", result.Stats.TokensOut)
	}
	if result.Stats.TokensTotal == nil || *result.Stats.TokensTotal != 5000 {
		t.Fatalf("tokens_total = %#v, want 5000", result.Stats.TokensTotal)
	}
	if result.Stats.CostUSD == nil || *result.Stats.CostUSD != 0.0421 {
		t.Fatalf("cost_usd = %#v, want 0.0421", result.Stats.CostUSD)
	}
}

func TestRunStopsOnIdleTimeout(t *testing.T) {
	binDir := t.TempDir()
	writeExecutable(t, filepath.Join(binDir, "opencode"), strings.Join([]string{
		"#!/bin/sh",
		"sleep 2",
	}, "\n"))
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result, err := Run(context.Background(), "issue #248", Request{
		Runner:      "opencode",
		Agent:       "build",
		Prompt:      "fix it",
		IdleTimeout: 200 * time.Millisecond,
	})
	var idleErr *IdleTimeoutError
	if !errors.As(err, &idleErr) {
		t.Fatalf("Run() error = %v, want IdleTimeoutError", err)
	}
	if result == nil {
		t.Fatalf("result = nil, want partial stats")
	}
}

func TestRunStopsWhenTokenBudgetExceeded(t *testing.T) {
	binDir := t.TempDir()
	writeExecutable(t, filepath.Join(binDir, "opencode"), strings.Join([]string{
		"#!/bin/sh",
		"printf 'Input tokens: 20,000\n'",
		"printf 'Output tokens: 1,400\n'",
		"sleep 1",
	}, "\n"))
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result, err := Run(context.Background(), "issue #248", Request{
		Runner:      "opencode",
		Agent:       "build",
		Prompt:      "fix it",
		TokenBudget: 20000,
	})
	var budgetErr *TokenBudgetExceededError
	if !errors.As(err, &budgetErr) {
		t.Fatalf("Run() error = %v, want TokenBudgetExceededError", err)
	}
	if result == nil || result.Stats.TokensTotal == nil || *result.Stats.TokensTotal != 21400 {
		t.Fatalf("tokens_total = %#v, want 21400", result)
	}
	if !strings.Contains(err.Error(), "token budget of 20 000 exceeded") {
		t.Fatalf("error = %q, want formatted budget message", err.Error())
	}
}

func writeExecutable(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatalf("WriteFile(%s) error = %v", path, err)
	}
}
