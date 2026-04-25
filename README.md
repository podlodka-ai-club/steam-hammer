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

**Issue run with automatic PR-review mode (when linked open PR exists):**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --issue 31 --runner opencode --agent build
```

**Force legacy issue-flow even if issue has open PR:**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --issue 31 --force-issue-flow
```

## Local config preset (per user/per machine)

You can define local defaults without changing repository defaults.

1. Copy the example config and adjust it for your setup:
   ```bash
   cp local-config.example.json local-config.json
   ```
2. Keep using CLI flags as usual. Priority is:
   - CLI flags
   - local config (`local-config.json`)
   - built-in defaults in script

`local-config.json` is ignored by git and stays local to your machine.

Supported local config keys:

- `state` (`open`, `closed`, `all`)
- `limit` (positive integer)
- `runner` (`claude` or `opencode`)
- `agent` (string)
- `model` (string or `null`)
- `agent_timeout_seconds` (positive integer)
- `agent_idle_timeout_seconds` (positive integer or `null`)
- `opencode_auto_approve` (boolean)
- `branch_prefix` (string)
- `include_empty` (boolean)
- `stop_on_error` (boolean)
- `fail_on_existing` (boolean)
- `force_issue_flow` (boolean)
- `sync_reused_branch` (boolean)
- `sync_strategy` (`rebase` or `merge`)

You can also point to a different local config file:

```bash
python scripts/run_github_issues_to_opencode.py --local-config path/to/local-config.json
```

**Use local defaults from repository config (`local-config.json`):**
```bash
cp local-config.example.json local-config.json
python scripts/run_github_issues_to_opencode.py --repo owner/repo --limit 1
```

Workflow per issue:

1. Chooses a stable base branch (repository default branch from GitHub)
2. Creates a new issue branch from that base (`--branch-prefix`, default `issue-fix`) or reuses an existing one
3. For reused branches, syncs with the latest selected base branch before agent run (default: `rebase`)
4. Runs the AI agent with issue title/body context
5. On changes, creates commit
6. Pushes issue branch to `origin`
7. Reuses an existing open PR for the issue branch when present; otherwise creates one to the stable base branch

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
- `--local-config path` load local JSON defaults (default: `local-config.json` under `--dir`)
- `--fail-on-existing` strict mode: fail if issue branch or PR already exists
- `--force-issue-flow` disable auto-switch to PR-review mode for `--issue`
- `--sync-reused-branch` / `--no-sync-reused-branch` enable or disable reused-branch sync before agent run (default: enabled)
- `--sync-strategy rebase|merge` choose how to sync a reused branch with selected base (default: `rebase`)

If `--repo` is not provided, script tries to detect repository from current `gh` context.

Note: script expects a clean git working tree before run.

PR mode notes:

- `--pr` must be used together with `--from-review-comments`.
- If PR is closed/non-open, script exits without changes.
- If there are no actionable unresolved comments, script exits successfully without running the agent.
- Review summaries are taken from the latest review per author to avoid reprocessing superseded feedback.

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

## Reruns and conflict resolution

- Re-running for an issue now auto-detects existing issue branches and reuses them instead of failing on `git checkout -b`.
- If an open PR already exists for the issue branch, the script reuses it (even if your currently checked-out local branch is different).
- PR reuse first checks `base+head`, then falls back to `head`-only lookup to avoid duplicate PR creation when reruns start from another feature branch.
- Base branch selection is deterministic: issue runs target the repository default branch from GitHub, not your current local branch.
- On rerun with a reused branch, the script syncs that branch with the selected base before running the agent (`--sync-strategy rebase` by default).
- If rebase sync for a reused branch conflicts, the script now automatically falls back to merge-based sync and resolves conflicted files in favor of the selected base branch.
- In auto-switched `pr-review` runs (`--issue <n>` with linked open PR), the same conflict flow is applied so routine sync conflicts do not block unattended reruns.
- Explicit rerun case is supported: when the issue already has an open PR that is conflicted with base (`mergeStateStatus=DIRTY`), pr-review mode auto-resolves routine sync conflicts, pushes the updated branch, and lets GitHub recalculate mergeability without manual conflict steps.
- Conflict strategy is deterministic for trusted repositories: prefer selected base branch content (`git checkout --theirs`) for conflicted paths, then finish merge with `--no-edit`.
- If merge-based auto-resolution still cannot finish, the run stops before agent execution with a clear error and hints to resolve conflicts.
- If sync updates branch history and agent produces no new file changes, the script still pushes sync-only branch updates so existing PR conflict status can be refreshed.
- For rebase-based sync that rewrites branch history, push uses `--force-with-lease` automatically.
- Use `--sync-strategy merge` if you prefer merge-based sync instead of rebase.
- Use `--no-sync-reused-branch` only when you intentionally want to skip auto-sync.
- Use `--dry-run` to preview selected base branch and whether each issue will create or reuse branch/PR resources.
- `--dry-run` also shows whether reused-branch sync will run and which strategy will be used.
- Use `--fail-on-existing` when you want strict behavior and prefer the run to fail if branch/PR already exists.

## Auto switch to PR-review mode

- When you run with `--issue <n>`, the script checks whether this issue has a linked open PR.
- If found, it automatically switches to PR-review mode and builds the agent prompt from issue + PR + review comments context.
- The script logs that auto-switch happened and why (including the PR number).
- `--dry-run` prints selected mode (`issue-flow` or `pr-review`) and the reason.
- Use `--force-issue-flow` to keep legacy issue-flow behavior.

## Verification

Run the precedence smoke test:

```bash
python3 -m unittest tests/test_local_config_precedence.py
```
