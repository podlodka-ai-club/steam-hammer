#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
import sys


def run_capture(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{stderr}")
    return result.stdout


def run_command(command: list[str]) -> None:
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(command)}")


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


def current_branch() -> str:
    return run_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()


def ensure_clean_worktree() -> None:
    status = run_capture(["git", "status", "--porcelain"]).strip()
    if status:
        raise RuntimeError("Git working tree must be clean before running this script.")


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:40] or "issue"


def branch_name_for_issue(issue: dict, prefix: str) -> str:
    return f"{prefix}/#{issue['number']}-{slugify(issue['title'])}".replace("#", "")


def has_changes() -> bool:
    return bool(run_capture(["git", "status", "--porcelain"]).strip())


def build_prompt(issue: dict) -> str:
    return (
        "You are working on a GitHub issue in the current git branch.\n"
        "Implement the fix for the issue in the repository files.\n"
        "Do not run git commands; git actions are handled by orchestration script.\n\n"
        f"Issue: #{issue['number']} - {issue['title']}\n"
        f"URL: {issue['url']}\n\n"
        "Issue body:\n"
        f"{issue.get('body', '').strip()}\n"
    )


def run_agent(
    issue: dict,
    runner: str,
    agent: str,
    model: str | None,
    dry_run: bool,
) -> int:
    prompt = build_prompt(issue)

    if runner == "claude":
        command = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        if model:
            command.extend(["--model", model])
    else:
        command = ["opencode", "run", "--agent", agent]
        if model:
            command.extend(["--model", model])
        command.append(prompt)

    if dry_run:
        print(
            f"[dry-run] Would run: {' '.join(command[:4])} ... for issue #{issue['number']}"
        )
        return 0

    print(f"Running agent for issue #{issue['number']}: {issue['title']}")
    result = subprocess.run(command)
    return result.returncode


def create_branch(base_branch: str, branch_name: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] Would create branch '{branch_name}' from '{base_branch}'")
        return
    run_command(["git", "checkout", base_branch])
    run_command(["git", "checkout", "-b", branch_name])


def commit_changes(issue: dict, dry_run: bool) -> str:
    message = f"Fix issue #{issue['number']}: {issue['title']}"
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
    run_command(["git", "add", "-A"])
    run_command(["git", "commit", "-m", message])
    return message


def push_branch(branch_name: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] Would push branch '{branch_name}' to origin")
        return
    run_command(["git", "push", "-u", "origin", branch_name])


def open_pr(
    repo: str,
    base_branch: str,
    branch_name: str,
    issue: dict,
    dry_run: bool,
) -> str:
    title = f"Fix issue #{issue['number']}: {issue['title']}"
    body = (
        "## Summary\n"
        f"- Implements fix for issue #{issue['number']}\n"
        f"- Source issue: {issue['url']}\n\n"
        f"Closes #{issue['number']}\n"
    )
    if dry_run:
        print(
            f"[dry-run] Would create PR '{title}' from '{branch_name}' to '{base_branch}'"
        )
        return ""
    output = run_capture(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base_branch,
            "--head",
            branch_name,
            "--title",
            title,
            "--body",
            body,
        ]
    )
    return output.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub issues with gh and run an AI agent for each issue body."
    )
    parser.add_argument(
        "--repo", help="GitHub repo in owner/name format. Defaults to current gh repo."
    )
    parser.add_argument("--state", default="open", choices=["open", "closed", "all"])
    parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of issues to process."
    )
    parser.add_argument(
        "--runner",
        default="claude",
        choices=["claude", "opencode"],
        help="AI agent runner to use (default: claude).",
    )
    parser.add_argument("--agent", default="build", help="Opencode agent name (only used with --runner opencode).")
    parser.add_argument("--model", help="Optional model override. For Claude: e.g. claude-sonnet-4-6. For OpenCode: e.g. openai/gpt-4o.")
    parser.add_argument(
        "--branch-prefix",
        default="issue-fix",
        help="Prefix for per-issue git branches.",
    )
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
        "--dry-run", action="store_true", help="Print actions without running the agent."
    )
    args = parser.parse_args()

    try:
        ensure_clean_worktree()
        base_branch = current_branch()
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
    created_prs: list[str] = []

    for issue in issues:
        body = (issue.get("body") or "").strip()
        if not body and not args.include_empty:
            print(f"Skipping issue #{issue['number']} (empty body)")
            continue

        processed += 1
        issue_branch = branch_name_for_issue(issue=issue, prefix=args.branch_prefix)

        try:
            create_branch(
                base_branch=base_branch, branch_name=issue_branch, dry_run=args.dry_run
            )

            exit_code = run_agent(
                issue=issue,
                runner=args.runner,
                agent=args.agent,
                model=args.model,
                dry_run=args.dry_run,
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"Agent failed for issue #{issue['number']} with exit code {exit_code}"
                )

            if not args.dry_run and not has_changes():
                print(
                    f"No changes detected for issue #{issue['number']}; skipping commit and PR"
                )
                run_command(["git", "checkout", base_branch])
                continue

            commit_changes(issue=issue, dry_run=args.dry_run)
            push_branch(branch_name=issue_branch, dry_run=args.dry_run)
            pr_url = open_pr(
                repo=repo,
                base_branch=base_branch,
                branch_name=issue_branch,
                issue=issue,
                dry_run=args.dry_run,
            )
            if pr_url:
                created_prs.append(pr_url)
                print(f"Created PR: {pr_url}")

            if not args.dry_run:
                run_command(["git", "checkout", base_branch])
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"Issue #{issue['number']} failed: {exc}", file=sys.stderr)
            if args.stop_on_error:
                break

    print(f"Done. Processed: {processed}, failures: {failures}")
    if created_prs:
        print("PRs:")
        for pr_url in created_prs:
            print(f"- {pr_url}")
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
