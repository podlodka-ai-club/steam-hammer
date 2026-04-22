# GitHub Issue -> OpenCode Runner

Script fetches GitHub issues via `gh`, runs OpenCode agent on each issue body, and then automates git workflow for a fix branch.

## Requirements

- `gh` (GitHub CLI) authenticated (`gh auth status`)
- `opencode` installed and configured
- Python 3.10+

## Usage

```bash
./scripts/run_github_issues_to_opencode.py --repo owner/repo --limit 1 --agent build --model openai/gpt-5.3-codex
```

Workflow per issue:

1. Creates a new branch from current branch (`--branch-prefix`, default `issue-fix`)
2. Runs `opencode run` with issue title/body context
3. On changes, creates commit
4. Pushes issue branch to `origin`
5. Creates PR back to the original base branch

Useful options:

- `--state open|closed|all`
- `--include-empty` to process issues with empty body
- `--stop-on-error` to stop on first failed run
- `--dry-run` to preview without executing `opencode`
- `--model provider/model` to override model
- `--branch-prefix prefix` to customize fix branch names

If `--repo` is not provided, script tries to detect repository from current `gh` context.

Note: script expects a clean git working tree before run.
