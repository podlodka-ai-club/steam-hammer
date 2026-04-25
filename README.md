# GitHub Issue/PR -> AI Agent Runner

Script can run in two modes:

- Issue mode: fetches GitHub issues via `gh`, runs an AI agent on each issue body, and automates git workflow for a fix branch.
- PR review mode: fetches unresolved PR review feedback, builds a focused prompt for the agent, and prepares a follow-up commit.

## Requirements

- `gh` (GitHub CLI) authenticated (`gh auth status`)
- Python 3.10+
- `claude` (Claude Code CLI) — default runner
- `opencode` — only if using `--runner opencode`

## Usage

**With Claude (default):**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --limit 1
```

**With OpenCode:**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --limit 1 --runner opencode --agent build --model openai/gpt-4o
```

**With OpenCode auto-approval + explicit timeout (non-interactive friendly):**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --issue 20 --runner opencode --model openai/gpt-5.3-codex --agent build --opencode-auto-approve --agent-timeout-seconds 900 --agent-idle-timeout-seconds 180
```

**PR review-comments mode (apply unresolved review feedback):**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --pr 23 --from-review-comments --runner opencode --agent build
```

**PR review-comments mode dry-run (preview comments and planned actions):**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --pr 23 --from-review-comments --dry-run
```

Workflow per issue:

1. Creates a new branch from current branch (`--branch-prefix`, default `issue-fix`)
2. Runs the AI agent with issue title/body context
3. On changes, creates commit
4. Pushes issue branch to `origin`
5. Creates PR back to the original base branch

Workflow in PR review mode:

1. Loads PR metadata and review threads/comments
2. Filters out resolved/outdated/empty feedback and builds an actionable prompt with file/line links
3. Adds PR description and linked issue context (including issue body when available)
4. Runs AI agent in current branch (or optional follow-up branch)
5. On changes, creates commit and pushes updates
6. Optionally posts a summary comment to the PR

Useful options:

- `--runner claude|opencode` to select the AI agent runner (default: `claude`)
- `--state open|closed|all`
- `--include-empty` to process issues with empty body
- `--stop-on-error` to stop on first failed run
- `--dry-run` to preview without executing the agent
- `--pr N --from-review-comments` to run PR review-comments mode
- `--pr-followup-branch-prefix prefix` to create a follow-up branch in PR mode instead of committing to current branch
- `--post-pr-summary` to leave a short summary comment in the PR after successful PR mode run
- `--model model-name` to override model (e.g. `claude-sonnet-4-6` for Claude, `openai/gpt-4o` for OpenCode)
- `--agent name` agent name for OpenCode (ignored when using Claude)
- `--branch-prefix prefix` to customize fix branch names
- `--agent-timeout-seconds N` hard timeout for agent run (default: `900`)
- `--agent-idle-timeout-seconds N` fail if agent prints no output for `N` seconds
- `--opencode-auto-approve` pass `--dangerously-skip-permissions` to OpenCode (use with caution)

If `--repo` is not provided, script tries to detect repository from current `gh` context.

Note: script expects a clean git working tree before run.

PR mode notes:

- `--pr` must be used together with `--from-review-comments`.
- If PR is closed/non-open, script exits without changes.
- If there are no actionable unresolved comments, script exits successfully without running the agent.

## Smoke test

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Troubleshooting hangs

- If a run appears stuck, set an explicit timeout and idle-timeout:
  - `--agent-timeout-seconds 900`
  - `--agent-idle-timeout-seconds 180`
- If OpenCode may be waiting for interactive permission approvals, try `--opencode-auto-approve` only in trusted environments.
- On timeout/idle-timeout the script now exits with a clear error so normal failure handling (`--stop-on-error`) can proceed.
