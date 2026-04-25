#!/usr/bin/env python3

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time


MAX_REVIEW_ITEMS = 40


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


def fetch_issue(repo: str, number: int) -> dict:
    output = run_capture(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url",
        ]
    )
    issue = json.loads(output)
    if not isinstance(issue, dict):
        raise RuntimeError(f"Unexpected response fetching issue #{number}")
    return issue


def parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise RuntimeError(f"Invalid --repo format: {repo}. Expected owner/name.")
    return parts[0], parts[1]


def fetch_pull_request(repo: str, number: int) -> dict:
    output = run_capture(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url,state,baseRefName,headRefName,author,reviews,closingIssuesReferences",
        ]
    )
    pull_request = json.loads(output)
    if not isinstance(pull_request, dict):
        raise RuntimeError(f"Unexpected response fetching PR #{number}")
    return pull_request


def fetch_pr_review_threads(repo: str, number: int) -> list[dict]:
    owner, name = parse_repo(repo)
    query = (
        "query($owner:String!, $name:String!, $number:Int!, $cursor:String) {"
        " repository(owner:$owner, name:$name) {"
        "   pullRequest(number:$number) {"
        "     reviewThreads(first:100, after:$cursor) {"
        "       nodes {"
        "         id"
        "         isResolved"
        "         isOutdated"
        "         comments(first:100) {"
        "           nodes {"
        "             id"
        "             body"
        "             path"
        "             line"
        "             originalLine"
        "             url"
        "             outdated"
        "             author { login }"
        "           }"
        "         }"
        "       }"
        "       pageInfo { hasNextPage endCursor }"
        "     }"
        "   }"
        " }"
        "}"
    )

    threads: list[dict] = []
    cursor: str | None = None

    while True:
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor:
            command.extend(["-F", f"cursor={cursor}"])

        output = run_capture(command)
        if not output.strip():
            raise RuntimeError(f"Empty response fetching review threads for PR #{number}")
        response = json.loads(output)
        if "errors" in response:
            raise RuntimeError(
                f"GraphQL error fetching review threads for PR #{number}: {response['errors']}"
            )

        repository = response.get("data", {}).get("repository")
        if not isinstance(repository, dict):
            raise RuntimeError(f"Unexpected GraphQL repository payload for PR #{number}")

        pull_request = repository.get("pullRequest")
        if pull_request is None:
            raise RuntimeError(f"Pull request #{number} not found in GraphQL response")
        if not isinstance(pull_request, dict):
            raise RuntimeError(f"Unexpected GraphQL pullRequest payload for PR #{number}")

        pr_data = pull_request.get("reviewThreads") or {}
        if not isinstance(pr_data, dict):
            raise RuntimeError(f"Unexpected review thread structure for PR #{number}")
        page_nodes = pr_data.get("nodes") or []
        if not isinstance(page_nodes, list):
            raise RuntimeError(f"Unexpected review thread structure for PR #{number}")
        threads.extend(page_nodes)

        page_info = pr_data.get("pageInfo") or {}
        has_next = bool(page_info.get("hasNextPage"))
        cursor = page_info.get("endCursor")
        if not has_next:
            break

    return threads


def normalize_review_items(
    threads: list[dict],
    reviews: list[dict],
    pr_author_login: str | None = None,
) -> tuple[list[dict], dict]:
    actionable: list[dict] = []
    pr_author = (pr_author_login or "").strip().lower()
    stats = {
        "threads_total": len(threads),
        "threads_resolved": 0,
        "threads_outdated": 0,
        "comments_total": 0,
        "comments_empty": 0,
        "comments_outdated": 0,
        "comments_pr_author": 0,
        "reviews_total": len(reviews),
        "reviews_used": 0,
    }

    for thread in threads:
        if thread.get("isResolved"):
            stats["threads_resolved"] += 1
            continue
        if thread.get("isOutdated"):
            stats["threads_outdated"] += 1
            continue

        comments = (thread.get("comments") or {}).get("nodes") or []
        if not isinstance(comments, list):
            continue

        for comment in comments:
            stats["comments_total"] += 1
            if comment.get("outdated"):
                stats["comments_outdated"] += 1
                continue

            author_login = ((comment.get("author") or {}).get("login") or "").strip()
            if pr_author and author_login.lower() == pr_author:
                stats["comments_pr_author"] += 1
                continue

            body = (comment.get("body") or "").strip()
            if not body:
                stats["comments_empty"] += 1
                continue

            actionable.append(
                {
                    "type": "review_comment",
                    "author": author_login or "unknown",
                    "body": body,
                    "path": comment.get("path") or "",
                    "line": comment.get("line") or comment.get("originalLine"),
                    "url": comment.get("url") or "",
                }
            )

    for review in reviews:
        state = (review.get("state") or "").upper()
        body = (review.get("body") or "").strip()
        if state not in {"CHANGES_REQUESTED", "COMMENTED"}:
            continue
        if not body:
            continue

        actionable.append(
            {
                "type": "review_summary",
                "author": (review.get("author") or {}).get("login") or "unknown",
                "body": body,
                "path": "",
                "line": None,
                "url": review.get("url") or "",
                "state": state,
            }
        )
        stats["reviews_used"] += 1

    return actionable[:MAX_REVIEW_ITEMS], stats


def _linked_issue_number(candidate: dict) -> int | None:
    number = candidate.get("number")
    return number if isinstance(number, int) and number > 0 else None


def load_linked_issue_context(repo: str, pull_request: dict) -> list[dict]:
    linked = pull_request.get("closingIssuesReferences") or []
    if not isinstance(linked, list) or not linked:
        return []

    context_items: list[dict] = []
    for linked_issue in linked[:5]:
        number = _linked_issue_number(linked_issue)
        if number is None:
            continue

        title = (linked_issue.get("title") or "").strip()
        body = (linked_issue.get("body") or "").strip()
        url = (linked_issue.get("url") or "").strip()

        if title and (body or url):
            context_items.append(
                {
                    "number": number,
                    "title": title,
                    "body": body,
                    "url": url,
                }
            )
            continue

        fetched = fetch_issue(repo=repo, number=number)
        context_items.append(
            {
                "number": fetched.get("number", number),
                "title": (fetched.get("title") or "").strip(),
                "body": (fetched.get("body") or "").strip(),
                "url": (fetched.get("url") or "").strip(),
            }
        )

    return context_items


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


def build_pr_review_prompt(
    pull_request: dict,
    review_items: list[dict],
    linked_issues: list[dict] | None = None,
) -> str:
    pr_number = pull_request["number"]
    lines = [
        "You are working on review feedback for an existing GitHub pull request in the current git branch.",
        "Implement fixes for the review comments in repository files.",
        "Do not run git commands; git actions are handled by orchestration script.",
        "",
        f"Pull Request: #{pr_number} - {pull_request['title']}",
        f"URL: {pull_request['url']}",
        "",
        "PR description:",
        (pull_request.get("body") or "").strip() or "(empty)",
    ]

    if linked_issues:
        lines.extend(["", "Linked issues context:"])
        for issue in linked_issues[:5]:
            issue_body = (issue.get("body") or "").strip()
            lines.append(
                f"- #{issue.get('number')} {issue.get('title')} ({issue.get('url')})"
            )
            if issue_body:
                lines.append(issue_body)

    lines.extend(["", "Review feedback to address:"])
    for idx, item in enumerate(review_items, start=1):
        location = item["path"]
        if item.get("line"):
            location = f"{location}:{item['line']}" if location else f"line {item['line']}"
        if not location:
            location = "general"
        url = item.get("url") or "n/a"
        item_type = "summary" if item["type"] == "review_summary" else "comment"
        lines.extend(
            [
                f"{idx}. [{item_type}] by @{item['author']}",
                f"   Location: {location}",
                f"   Link: {url}",
                f"   Text: {item['body']}",
            ]
        )

    return "\n".join(lines) + "\n"


def print_review_dry_run(pull_request: dict, review_items: list[dict], stats: dict) -> None:
    print(
        f"[dry-run] PR #{pull_request['number']} review mode: "
        f"{len(review_items)} actionable items"
    )
    print(
        "[dry-run] Review scan stats: "
        f"threads={stats.get('threads_total', 0)}, resolved={stats.get('threads_resolved', 0)}, "
        f"outdated_threads={stats.get('threads_outdated', 0)}, "
        f"comments_total={stats.get('comments_total', 0)}, "
        f"comments_outdated={stats.get('comments_outdated', 0)}, "
        f"comments_empty={stats.get('comments_empty', 0)}, reviews_used={stats.get('reviews_used', 0)}"
        f", comments_pr_author={stats.get('comments_pr_author', 0)}"
    )
    preview = review_items[:10]
    for item in preview:
        location = item["path"] or "general"
        if item.get("line"):
            location = f"{location}:{item['line']}"
        body = item["body"].replace("\n", " ").strip()
        short_body = body if len(body) <= 160 else f"{body[:157]}..."
        print(
            f"[dry-run] - @{item['author']} {location} -> {short_body}"
        )
    if len(review_items) > len(preview):
        print(f"[dry-run] ... and {len(review_items) - len(preview)} more item(s)")


def run_agent(
    issue: dict,
    runner: str,
    agent: str,
    model: str | None,
    dry_run: bool,
    timeout_seconds: int,
    idle_timeout_seconds: int | None,
    opencode_auto_approve: bool,
) -> int:
    return run_agent_with_prompt(
        prompt=build_prompt(issue),
        item_label=f"issue #{issue['number']}: {issue['title']}",
        runner=runner,
        agent=agent,
        model=model,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        opencode_auto_approve=opencode_auto_approve,
    )


def run_agent_with_prompt(
    prompt: str,
    item_label: str,
    runner: str,
    agent: str,
    model: str | None,
    dry_run: bool,
    timeout_seconds: int,
    idle_timeout_seconds: int | None,
    opencode_auto_approve: bool,
) -> int:
    if runner == "claude":
        command = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        if model:
            command.extend(["--model", model])
    else:
        command = ["opencode", "run", "--agent", agent]
        if model:
            command.extend(["--model", model])
        if opencode_auto_approve:
            command.append("--dangerously-skip-permissions")
        command.append(prompt)

    if dry_run:
        print(f"[dry-run] Would run: {' '.join(command[:4])} ... for {item_label}")
        return 0

    print(f"Running agent for {item_label}")
    start = time.monotonic()
    last_output = start

    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ)
    if process.stderr is not None:
        selector.register(process.stderr, selectors.EVENT_READ)

    try:
        while True:
            now = time.monotonic()
            elapsed = now - start
            idle_elapsed = now - last_output

            if timeout_seconds > 0 and elapsed > timeout_seconds:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(
                    f"Agent timed out after {timeout_seconds}s for {item_label}. "
                    "Possible causes: waiting for interactive approval, network stall, "
                    "or a long-running task. Try increasing --agent-timeout-seconds, "
                    "setting --agent-idle-timeout-seconds, or using --opencode-auto-approve "
                    "for OpenCode if safe in your environment."
                )

            if idle_timeout_seconds and idle_elapsed > idle_timeout_seconds:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(
                    f"Agent produced no output for {idle_timeout_seconds}s on {item_label}; "
                    "aborting to avoid indefinite hang. Possible causes: waiting for "
                    "interactive approval or a stuck process. Try --opencode-auto-approve "
                    "(if safe) or a larger --agent-idle-timeout-seconds."
                )

            events = selector.select(timeout=1.0)
            if events:
                for key, _ in events:
                    line = key.fileobj.readline()
                    if line:
                        last_output = time.monotonic()
                        if key.fileobj is process.stderr:
                            print(line, end="", file=sys.stderr)
                        else:
                            print(line, end="")

            if process.poll() is not None:
                if process.stdout is not None:
                    remainder = process.stdout.read() or ""
                    if remainder:
                        print(remainder, end="")
                if process.stderr is not None:
                    remainder = process.stderr.read() or ""
                    if remainder:
                        print(remainder, end="", file=sys.stderr)
                return process.returncode
    finally:
        selector.close()


def create_branch(base_branch: str, branch_name: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] Would create branch '{branch_name}' from '{base_branch}'")
        return
    run_command(["git", "checkout", base_branch])
    run_command(["git", "checkout", "-b", branch_name])


def create_followup_branch(current_branch_name: str, branch_name: str, dry_run: bool) -> None:
    if dry_run:
        print(
            f"[dry-run] Would create follow-up branch '{branch_name}' from '{current_branch_name}'"
        )
        return
    run_command(["git", "checkout", current_branch_name])
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


def push_current_branch(dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] Would push current branch to origin")
        return
    run_command(["git", "push"])


def commit_pr_review_changes(pull_request: dict, dry_run: bool) -> str:
    message = f"Address review comments for PR #{pull_request['number']}"
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
    run_command(["git", "add", "-A"])
    run_command(["git", "commit", "-m", message])
    return message


def leave_pr_summary_comment(
    repo: str,
    pr_number: int,
    review_items_count: int,
    dry_run: bool,
) -> None:
    body = (
        "Automated follow-up completed.\n\n"
        f"- Addressed review feedback items: {review_items_count}\n"
        "- Please run another review pass for confirmation."
    )
    if dry_run:
        print(f"[dry-run] Would leave summary comment in PR #{pr_number}")
        return
    run_command(
        [
            "gh",
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            repo,
            "--body",
            body,
        ]
    )


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
    parser.add_argument(
        "--issue", type=int, help="Process a single issue by number, ignoring --limit and --state."
    )
    parser.add_argument(
        "--pr", type=int, help="Process a single pull request by number."
    )
    parser.add_argument(
        "--from-review-comments",
        action="store_true",
        help="Enable PR review-comments mode (requires --pr).",
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
        "--agent-timeout-seconds",
        type=int,
        default=900,
        help="Hard timeout for agent execution in seconds (default: 900).",
    )
    parser.add_argument(
        "--agent-idle-timeout-seconds",
        type=int,
        help="Abort if agent produces no output for this many seconds.",
    )
    parser.add_argument(
        "--opencode-auto-approve",
        action="store_true",
        help=(
            "For --runner opencode, pass --dangerously-skip-permissions to reduce "
            "interactive approval waits. Use with caution."
        ),
    )
    parser.add_argument(
        "--branch-prefix",
        default="issue-fix",
        help="Prefix for per-issue git branches.",
    )
    parser.add_argument(
        "--pr-followup-branch-prefix",
        help=(
            "Optional prefix for follow-up branch in PR review mode. If omitted, "
            "changes are committed to the current PR branch."
        ),
    )
    parser.add_argument(
        "--post-pr-summary",
        action="store_true",
        help="Post a short summary comment to PR after successful PR review run.",
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
        "--dir",
        default=".",
        help="Path to the local git repository to operate on. Defaults to the current directory.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without running the agent."
    )
    args = parser.parse_args()

    try:
        target_dir = os.path.abspath(args.dir)
        if not os.path.isdir(target_dir):
            raise RuntimeError(f"--dir path does not exist or is not a directory: {target_dir}")
        if not os.path.isdir(os.path.join(target_dir, ".git")):
            raise RuntimeError(f"--dir path is not a git repository: {target_dir}")
        os.chdir(target_dir)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.issue is not None and args.pr is not None:
            raise RuntimeError("Use either --issue or --pr, not both.")
        pr_mode_requested = args.pr is not None or args.from_review_comments
        if args.from_review_comments and args.pr is None:
            raise RuntimeError("--from-review-comments requires --pr <number>.")
        if args.pr is not None and not args.from_review_comments:
            raise RuntimeError("--pr requires --from-review-comments.")

        ensure_clean_worktree()
        base_branch = current_branch()
        repo = args.repo or detect_repo()
        if pr_mode_requested:
            pull_request = fetch_pull_request(repo=repo, number=args.pr)
            if pull_request.get("state") != "OPEN":
                print(f"PR #{pull_request['number']} is not open; nothing to do.")
                return 0
            threads = fetch_pr_review_threads(repo=repo, number=args.pr)
            reviews = pull_request.get("reviews") or []
            pr_author_login = ((pull_request.get("author") or {}).get("login") or "").strip()
            review_items, review_stats = normalize_review_items(
                threads=threads,
                reviews=reviews,
                pr_author_login=pr_author_login,
            )
            linked_issue_context = load_linked_issue_context(repo=repo, pull_request=pull_request)
            if args.dry_run:
                print_review_dry_run(
                    pull_request=pull_request,
                    review_items=review_items,
                    stats=review_stats,
                )
            if not review_items:
                print(f"No actionable unresolved review feedback found for PR #{args.pr}.")
                return 0
            prompt = build_pr_review_prompt(
                pull_request=pull_request,
                review_items=review_items,
                linked_issues=linked_issue_context,
            )
            followup_branch = ""
            if args.pr_followup_branch_prefix:
                followup_branch = (
                    f"{args.pr_followup_branch_prefix}/pr-{pull_request['number']}-"
                    f"{slugify(pull_request['title'])}"
                )
                create_followup_branch(
                    current_branch_name=base_branch,
                    branch_name=followup_branch,
                    dry_run=args.dry_run,
                )

            exit_code = run_agent_with_prompt(
                prompt=prompt,
                item_label=f"PR #{pull_request['number']} review comments",
                runner=args.runner,
                agent=args.agent,
                model=args.model,
                dry_run=args.dry_run,
                timeout_seconds=args.agent_timeout_seconds,
                idle_timeout_seconds=args.agent_idle_timeout_seconds,
                opencode_auto_approve=args.opencode_auto_approve,
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"Agent failed for PR #{pull_request['number']} with exit code {exit_code}"
                )

            if not args.dry_run and not has_changes():
                print(f"No changes detected after processing PR #{pull_request['number']} comments")
                return 0

            commit_pr_review_changes(pull_request=pull_request, dry_run=args.dry_run)
            if followup_branch:
                push_branch(branch_name=followup_branch, dry_run=args.dry_run)
            else:
                push_current_branch(dry_run=args.dry_run)
            if args.post_pr_summary:
                leave_pr_summary_comment(
                    repo=repo,
                    pr_number=pull_request["number"],
                    review_items_count=len(review_items),
                    dry_run=args.dry_run,
                )

            print(
                f"Done. Processed PR #{pull_request['number']} review mode; "
                "failures: 0"
            )
            return 0

        if args.issue is not None:
            issues = [fetch_issue(repo=repo, number=args.issue)]
        else:
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
                timeout_seconds=args.agent_timeout_seconds,
                idle_timeout_seconds=args.agent_idle_timeout_seconds,
                opencode_auto_approve=args.opencode_auto_approve,
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
