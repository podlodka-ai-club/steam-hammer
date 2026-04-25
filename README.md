# GitHub Issue -> AI Agent Runner

Script fetches GitHub issues via `gh`, runs an AI agent on each issue body, and then automates git workflow for a fix branch.

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

1. Creates a new branch from current branch (`--branch-prefix`, default `issue-fix`)
2. Runs the AI agent with issue title/body context
3. On changes, creates commit
4. Pushes issue branch to `origin`
5. Creates PR back to the original base branch

Useful options:

- `--runner claude|opencode` to select the AI agent runner (default: `claude`)
- `--state open|closed|all`
- `--include-empty` to process issues with empty body
- `--stop-on-error` to stop on first failed run
- `--dry-run` to preview without executing the agent
- `--model model-name` to override model (e.g. `claude-sonnet-4-6` for Claude, `openai/gpt-4o` for OpenCode)
- `--agent name` agent name for OpenCode (ignored when using Claude)
- `--branch-prefix prefix` to customize fix branch names
- `--agent-timeout-seconds N` hard timeout for agent run (default: `900`)
- `--agent-idle-timeout-seconds N` fail if agent prints no output for `N` seconds
- `--opencode-auto-approve` pass `--dangerously-skip-permissions` to OpenCode (use with caution)
- `--local-config path` load local JSON defaults (default: `local-config.json` under `--dir`)

If `--repo` is not provided, script tries to detect repository from current `gh` context.

Note: script expects a clean git working tree before run.

## Troubleshooting hangs

- If a run appears stuck, set an explicit timeout and idle-timeout:
  - `--agent-timeout-seconds 900`
  - `--agent-idle-timeout-seconds 180`
- If OpenCode may be waiting for interactive permission approvals, try `--opencode-auto-approve` only in trusted environments.
- On timeout/idle-timeout the script now exits with a clear error so normal failure handling (`--stop-on-error`) can proceed.

## Verification

Run the precedence smoke test:

```bash
python3 -m unittest tests/test_local_config_precedence.py
```
