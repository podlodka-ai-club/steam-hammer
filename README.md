# GitHub Issue/PR -> AI Agent Runner

Script can run in two modes:

- Issue mode: fetches GitHub issues via `gh`, runs an AI agent on each issue body, and automates git workflow for a fix branch.
- PR review mode: fetches unresolved PR review feedback, builds a focused prompt for the agent, and prepares a follow-up commit.

Memo link: https://www.notion.so/Hacker-Sprint-1-33f2db4c860e8064a657e199b4578f66

- `gh` (GitHub CLI) authenticated (`gh auth status`)
- Python 3.10+
- `claude` (Claude Code CLI) — default runner
- `opencode` — only if using `--runner opencode`

```text
.
├── .gitignore
├── README.md
├── readme.md
└── scripts
    └── run_github_issues_to_opencode.py
```

## Run Example

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

**Issue run stacked on current branch (opt-in):**
```bash
python scripts/run_github_issues_to_opencode.py --repo owner/repo --issue 45 --base current --runner opencode --agent build
```

## Doctor diagnostics

Run environment diagnostics without starting an agent run:

```bash
python scripts/run_github_issues_to_opencode.py --doctor --repo owner/repo
```

Optional: include a lightweight runner CLI smoke check:

```bash
python scripts/run_github_issues_to_opencode.py --doctor --doctor-smoke-check --runner opencode --model openai/gpt-5.3-codex
```

Doctor output uses `[PASS]`, `[WARN]`, `[FAIL]` per check and prints a final summary.

- `PASS`: check is healthy.
- `WARN`: non-blocking issue or optional check skipped.
- `FAIL`: blocking readiness issue.

Exit codes:

- `0` when there are no failed checks.
- non-zero when one or more checks fail.

## Project config scaffold (repository-level)

You can define repository defaults and placeholders for future orchestration policies.

1. Copy the scaffold and adapt it for your project:
   ```bash
   cp project-config.example.json project-config.json
   ```
2. Keep using CLI flags and local config as usual. Precedence is:
   - CLI flags
   - local config (`local-config.json`)
   - project config (`project-config.json`)
   - built-in defaults in script

Project config currently supports these sections:

- `workflow.commands.test|lint|build` (non-empty string shell command or `null`)
- `defaults.runner|agent|model` (used as parser defaults)
- `scope.defaults.labels.allow|deny` (arrays of label names)
- `scope.defaults.authors.allow|deny` (arrays of GitHub logins; optional placeholder)
- `retry.max_attempts` (positive integer placeholder)
- `communication.verbosity` (`low`, `normal`, `high`)
- `presets` (object placeholder)

Validation is strict: unsupported keys or invalid value types fail fast with a config error.

Scope rules are evaluated before any issue-mode agent execution:

- deny labels always win;
- if allow labels are configured, issue must match at least one allow label;
- optional author allow/deny rules use the same semantics;
- out-of-scope issues get a `blocked` orchestration state and a dedicated scope decision comment;
- out-of-scope issues do not run the agent unless explicitly forced with `--force-reprocess`.

Workflow checks are evaluated after agent changes are committed and before final PR-ready states are posted:

- commands run in this order when configured: `test`, `lint`, `build`;
- each command is executed via `bash -lc "<command>"` from repository `--dir`;
- in `--dry-run`, checks are not executed and the script prints which checks would run;
- on failure, orchestration posts a state update with `stage=workflow_checks` and a `workflow_checks` payload containing command, exit code, and output excerpts;
- workflow-check failures block readiness transitions (`ready-for-review` / `waiting-for-ci`) and follow existing stop policy (`--stop-on-error`).

Example `project-config.json` workflow block:

```json
{
  "workflow": {
    "commands": {
      "test": "python -m unittest",
      "lint": "ruff check .",
      "build": null
    }
  }
}
```

Example `project-config.json` scope block:

```json
{
  "scope": {
    "defaults": {
      "labels": {
        "allow": ["autonomous", "bug"],
        "deny": ["manual-only", "needs-product-decision"]
      },
      "authors": {
        "allow": [],
        "deny": ["dependabot[bot]"]
      }
    }
  }
}
```

You can also point to a different project config file:

```bash
python scripts/run_github_issues_to_opencode.py --project-config path/to/project-config.json --repo owner/repo --limit 1
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
   - project config (`project-config.json`)
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
- `skip_if_pr_exists` (boolean)
- `skip_if_branch_exists` (boolean)
- `force_reprocess` (boolean)
- `sync_reused_branch` (boolean)
- `sync_strategy` (`rebase` or `merge`)
- `base_branch` (`default` or `current`)

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

1. Evaluates scope eligibility from project rules (`scope.defaults`) and prints decision (also in `--dry-run`)
2. Out-of-scope issues are blocked (`status=blocked`, stage=`scope_check`) and get a dedicated scope comment; agent run is skipped unless `--force-reprocess` is set
3. Pre-checks whether issue should be skipped (linked open PR and/or existing deterministic remote branch)
4. Chooses a base branch (`default`: repository default branch from GitHub; `current`: currently checked-out branch)
5. Creates a new issue branch from that base (`--branch-prefix`, default `issue-fix`) or reuses an existing one
6. For reused branches, syncs with the latest selected base branch before agent run (default: `rebase`)
7. Runs the AI agent with issue title/body context
8. On changes, creates commit
9. Pushes issue branch to `origin`
10. Reuses an existing open PR for the issue branch when present; otherwise creates one to the selected base branch
11. Posts append-only orchestration state comments to GitHub issue/PR on key transitions
12. On per-issue failure (agent, commit/push, PR create, etc.), posts a structured failure report comment to the issue and adds label `auto:agent-failed`
13. On successful issue completion (or no-op completion), removes label `auto:agent-failed` from that issue if present

Workflow in PR review mode:

1. Loads PR metadata and all feedback sources:
   - unresolved inline review thread comments
   - latest review summary per reviewer (`CHANGES_REQUESTED`, `COMMENTED`, `APPROVED`)
   - PR conversation comments (issue comments on the PR)
2. Filters and de-duplicates feedback, then builds an actionable prompt with file/line links
3. Adds PR description and linked issue context (including issue body when available)
4. Selects PR target branch (`headRefName`) as execution branch
   - by default switches current worktree to target PR branch (with safeguard)
   - or runs in isolated temporary worktree with `--isolate-worktree`
5. Runs AI agent on target branch (or optional follow-up branch from target)
6. On changes, creates commit and pushes updates to the selected branch
7. Optionally posts a summary comment to the PR
8. Posts append-only orchestration state comments to the PR (`in-progress`, `waiting-for-ci`, `waiting-for-author`, `failed`)

State comment format:

- Marker: `<!-- orchestration-state:v1 -->`
- Contains a human-readable header and a parseable JSON payload with fields like `status`, `task_type`, `issue`, `pr`, `branch`, `base_branch`, `runner`, `agent`, `model`, `attempt`, `stage`, `next_action`, `error`, `timestamp`
- Dry-run never posts comments; it prints which state comment would be posted and where

Automation failure reporting:

- Failure label: `auto:agent-failed` (auto-created if missing with color `B60205` and description `Automation run failed for this issue`)
- Failure issue comment includes: `status`, `stage`, `error`, `branch`, `base_branch`, `runner/agent/model`, `run id`, `timestamp`, and rerun hints
- Failure comments include marker `<!-- orchestration-agent-failure:v1 -->` and JSON payload for machine-readable context
- Dry-run prints which failure comment/label operations would run; it does not post or edit labels

Scope decision comments:

- Marker: `<!-- orchestration-scope:v1 -->`
- Out-of-scope comment includes decision, reason, forced flag, and timestamp in machine-readable JSON
- Dry-run prints where scope decision comment would be posted

Useful options:

- `--runner claude|opencode` to select the AI agent runner (default: `claude`)
- `--state open|closed|all`
- `--include-empty` to process issues with empty body
- `--stop-on-error` to stop on first failed run
- `--dry-run` to preview without executing the agent (includes scope decision per issue)
- `--pr N --from-review-comments` to run PR review-comments mode
- `--pr-followup-branch-prefix prefix` to create a follow-up branch in PR mode instead of committing to target PR branch
- `--allow-pr-branch-switch` allow switching current worktree to target PR branch when they differ
- `--isolate-worktree` run PR mode in a temporary git worktree without touching current branch
- `--post-pr-summary` to leave a short summary comment in the PR after successful PR mode run
- `--model model-name` to override model (e.g. `claude-sonnet-4-6` for Claude, `openai/gpt-4o` for OpenCode)
- `--agent name` agent name for OpenCode (ignored when using Claude)
- `--branch-prefix prefix` to customize fix branch names
- `--agent-timeout-seconds N` hard timeout for agent run (default: `900`)
- `--agent-idle-timeout-seconds N` fail if agent prints no output for `N` seconds
- `--opencode-auto-approve` pass `--dangerously-skip-permissions` to OpenCode (use with caution)
- `--local-config path` load local JSON defaults (default: `local-config.json` under `--dir`)
- `--project-config path` load repository JSON defaults scaffold (default: `project-config.json` under `--dir`)
- `--fail-on-existing` strict mode: fail if issue branch or PR already exists
- `--force-issue-flow` disable auto-switch to PR-review mode for `--issue`
- `--skip-if-pr-exists` / `--no-skip-if-pr-exists` skip or process batch issues when a linked open PR exists (default: skip; single `--issue` uses state-aware PR-review progression instead of hard-skip)
- `--skip-if-branch-exists` / `--no-skip-if-branch-exists` skip or process issues when deterministic issue branch exists on `origin` (default: skip)
- `--force-reprocess` override skip guards and out-of-scope gating (scope decision is still logged/commented)
- `--sync-reused-branch` / `--no-sync-reused-branch` enable or disable reused-branch sync before agent run (default: enabled)
- `--sync-strategy rebase|merge` choose how to sync a reused branch with selected base (default: `rebase`)
- `--base default|current` (`--base-branch` alias) choose issue-flow base mode; `current` enables stacked execution from your current branch (opt-in)
- `--doctor` run preflight diagnostics only (no agent run)
- `--doctor-smoke-check` in doctor mode, run a lightweight runner CLI smoke check

If `--repo` is not provided, script tries to detect repository from current `gh` context.

Note: script expects a clean git working tree before run.

PR mode notes:

- `--pr` must be used together with `--from-review-comments`.
- By default PR mode works on the target PR branch (`headRefName`) rather than the branch you started from.
- Safeguard: if current branch differs from target PR branch, run fails unless you pass `--allow-pr-branch-switch` (or `--isolate-worktree`).
- `--dry-run` prints selected target branch and whether execution will switch branches or use isolated worktree.
- If PR is closed/non-open, script exits without changes.
- If there are no actionable unresolved comments, script exits successfully without running the agent.
- If recovered state is `waiting-for-ci`, script reads GitHub check-runs and commit statuses for PR `headRefOid`:
  - pending checks -> emits `waiting-for-ci` (stage `ci_checks`);
  - successful checks or no checks -> emits `ready-to-merge`;
  - failing checks -> emits `blocked` and includes failing check names with URLs in state error/details.
- Prompt input priority is deterministic: unresolved inline comments first, then review summaries, then conversation comments.
- Review summaries are taken from the latest review per author to avoid reprocessing superseded feedback.
- Filtering rules are deterministic and backward-compatible:
  - Exclude resolved/outdated inline threads, outdated inline comments, empty comments, and PR-author self-comments.
  - Keep `CHANGES_REQUESTED` review summaries when non-empty.
  - Keep `COMMENTED`/`APPROVED` review summaries only when actionable (for example: contains concrete change requests).
  - Keep conversation comments only when actionable.
  - Exclude obvious non-actionable noise (`lgtm`, `looks good`, `thanks`, `+1`, punctuation-only text, etc.).
  - De-duplicate across all included sources while preserving priority order.
- Prompt-generation logs now include per-source counts and included/excluded breakdown.

Examples:

```bash
# Default PR mode with explicit branch-switch confirmation when needed
python scripts/run_github_issues_to_opencode.py --repo owner/repo --pr 22 --from-review-comments --allow-pr-branch-switch

# Isolated PR mode: do not switch current branch
python scripts/run_github_issues_to_opencode.py --repo owner/repo --pr 22 --from-review-comments --isolate-worktree
```

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

- Re-running is idempotent by default: issues are skipped when a linked open PR already exists.
- Re-running is also skipped by default when deterministic issue branch already exists on `origin`.
- Use `--force-reprocess` (or `--no-skip-if-pr-exists` / `--no-skip-if-branch-exists`) for intentional reruns.
- When rerun skip guards are disabled, the script auto-detects existing issue branches and reuses them instead of failing on `git checkout -b`.
- If an open PR already exists for the issue branch, the script reuses it (even if your currently checked-out local branch is different) when rerun skip guards are disabled.
- PR reuse first checks `base+head`, then falls back to `head`-only lookup to avoid duplicate PR creation when reruns start from another feature branch.
- Base branch selection is deterministic: by default issue runs target the repository default branch from GitHub; use `--base current` to stack on your current local branch.
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
- Use `--dry-run` to preview selected base branch mode (`default` vs `current`), selected base branch name, and whether each issue will create or reuse branch/PR resources.
- `--dry-run` also shows whether reused-branch sync will run and which strategy will be used.
- In `--base current` mode, the runner warns when the current branch is dirty, has no upstream tracking branch, or is ahead of upstream.
- Use `--fail-on-existing` when you want strict behavior and prefer the run to fail if branch/PR already exists.

## Auto switch to PR-review mode

- When you run with `--issue <n>`, the script checks whether this issue has a linked open PR.
- For single-issue runs, a linked open PR does not hard-skip the task; it enters state-aware PR-review/check progression so actionable review feedback can still be addressed.
- In batch issue runs, `--skip-if-pr-exists` remains enabled by default and skips issues that already have linked open PRs.
- If PR-review is selected, it builds the agent prompt from issue + PR + review comments context.
- The script logs that auto-switch happened and why (including the PR number).
- `--dry-run` prints selected mode (`issue-flow` or `pr-review`) and the reason.
- Use `--force-issue-flow` to keep legacy issue-flow behavior.

## Orchestration state recovery (first slice)

- For single-item runs (`--issue <n>` or `--pr <n> --from-review-comments`), the script inspects recent issue/PR comments for marker `<!-- orchestration-state:v1 -->`.
- It parses JSON payloads from those comments, ignores malformed payloads safely, and logs a warning for each malformed comment.
- It recovers the latest parseable state by comment `created_at` and prints recovered context (including in `--dry-run`).
- Recovered `waiting-for-author` causes issue processing to skip by default with a clear reason; use `--force-issue-flow` to override.
- Recovered `ready-for-review` keeps behavior conservative and, when an open linked PR exists, prefers PR-review path (no silent override of existing branch/PR checks).
- Recovered `waiting-for-ci` now performs a first-slice CI read from GitHub checks for the PR head SHA and updates orchestration state to `waiting-for-ci` / `ready-to-merge` / `blocked` based on pending/success/failure.
- Recovered `failed` state does not block rerun; previous failure details are logged and appended to the agent prompt as additional context.

## Verification

Run the precedence smoke test:

```bash
python3 -m unittest tests/test_local_config_precedence.py
```
