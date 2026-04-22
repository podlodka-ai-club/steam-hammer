#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys


def run_capture(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{stderr}")
    return result.stdout


def detect_repo() -> str:
    output = run_capture(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    repo = output.strip()
    if not repo:
        raise RuntimeError("Unable to detect GitHub repository. Use --repo owner/name.")
    return repo


def fetch_issues(repo: str, state: str, limit: int) -> list[dict]:
    output = run_capture(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,body,url",
        ]
    )
    issues = json.loads(output)
    if not isinstance(issues, list):
        raise RuntimeError("Unexpected response from gh issue list")
    return issues


def build_prompt(issue: dict) -> str:
    return (
        "You are working on a GitHub issue.\n\n"
        f"Issue: #{issue['number']} - {issue['title']}\n"
        f"URL: {issue['url']}\n\n"
        "Issue body:\n"
        f"{issue.get('body', '').strip()}\n"
    )


def run_agent(issue: dict, agent: str, model: str | None, dry_run: bool) -> int:
    prompt = build_prompt(issue)
    command = ["opencode", "run", "--agent", agent]
    if model:
        command.extend(["--model", model])
    command.append(prompt)

    if dry_run:
        print(
            f"[dry-run] Would run: {' '.join(command[:5])} ... for issue #{issue['number']}"
        )
        return 0

    print(f"Running agent for issue #{issue['number']}: {issue['title']}")
    result = subprocess.run(command)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub issues with gh and run opencode agent for each issue body."
    )
    parser.add_argument(
        "--repo", help="GitHub repo in owner/name format. Defaults to current gh repo."
    )
    parser.add_argument("--state", default="open", choices=["open", "closed", "all"])
    parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of issues to process."
    )
    parser.add_argument("--agent", default="general", help="Opencode agent name.")
    parser.add_argument("--model", help="Optional model override for opencode.")
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Process issues even if body is empty.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop after first failed agent run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without running opencode."
    )
    args = parser.parse_args()

    try:
        repo = args.repo or detect_repo()
        issues = fetch_issues(repo=repo, state=args.state, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not issues:
        print("No issues found.")
        return 0

    failures = 0
    processed = 0

    for issue in issues:
        body = (issue.get("body") or "").strip()
        if not body and not args.include_empty:
            print(f"Skipping issue #{issue['number']} (empty body)")
            continue

        processed += 1
        exit_code = run_agent(
            issue=issue, agent=args.agent, model=args.model, dry_run=args.dry_run
        )
        if exit_code != 0:
            failures += 1
            print(
                f"Agent failed for issue #{issue['number']} with exit code {exit_code}",
                file=sys.stderr,
            )
            if args.stop_on_error:
                break

    print(f"Done. Processed: {processed}, failures: {failures}")
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
