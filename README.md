# GitHub Issue -> OpenCode Runner

Simple script that fetches GitHub issues via `gh`, reads each issue body, and runs an OpenCode agent for every issue.

## Requirements

- `gh` (GitHub CLI) authenticated (`gh auth status`)
- `opencode` installed and configured
- Python 3.10+

## Usage

```bash
./scripts/run_github_issues_to_opencode.py --repo owner/repo --limit 5 --agent general
```

Useful options:

- `--state open|closed|all`
- `--include-empty` to process issues with empty body
- `--stop-on-error` to stop on first failed run
- `--dry-run` to preview without executing `opencode`
- `--model provider/model` to override model

If `--repo` is not provided, script tries to detect repository from current `gh` context.
