#!/usr/bin/env python3

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time


LOCAL_CONFIG_RELATIVE_PATH = "local-config.json"
BUILTIN_DEFAULTS = {
    "state": "open",
    "limit": 10,
    "runner": "claude",
    "agent": "build",
    "model": None,
    "agent_timeout_seconds": 900,
    "agent_idle_timeout_seconds": None,
    "opencode_auto_approve": False,
    "branch_prefix": "issue-fix",
    "include_empty": False,
    "stop_on_error": False,
    "fail_on_existing": False,
    "force_issue_flow": False,
    "dir": ".",
}


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


def command_succeeds(command: list[str]) -> bool:
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0


def detect_repo() -> str:
    output = run_capture(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    repo = output.strip()
    if not repo:
        raise RuntimeError("Unable to detect GitHub repository. Use --repo owner/name.")
    return repo


def detect_default_branch(repo: str) -> str:
    output = run_capture(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ]
    )
    branch = output.strip()
    if not branch:
        raise RuntimeError(
            "Unable to detect repository default branch. Use a valid --repo or check gh auth context."
        )
    return branch


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


def split_repo_name(repo: str) -> tuple[str, str]:
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        raise RuntimeError(f"Invalid repo format '{repo}'. Expected owner/name.")
    return owner, name


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
            "number,title,body,url,state,headRefName,baseRefName,author,closingIssuesReferences,reviews",
        ]
    )
    pull_request = json.loads(output)
    if not isinstance(pull_request, dict):
        raise RuntimeError(f"Unexpected response fetching PR #{number}")
    return pull_request


def fetch_pr_review_threads(repo: str, number: int) -> list[dict]:
    owner, name = split_repo_name(repo)
    query = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          isOutdated
          comments(first: 100) {
            nodes {
              body
              path
              line
              outdated
              url
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
""".strip()
    output = run_capture(
        [
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
    )
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected response from gh api while fetching PR review threads")

    repository_data = payload.get("data", {}).get("repository")
    if not isinstance(repository_data, dict):
        raise RuntimeError("Unexpected GraphQL payload while fetching PR review threads")
    pull_request = repository_data.get("pullRequest")
    if pull_request is None:
        raise RuntimeError(f"Pull request #{number} not found in repository {repo}")
    if not isinstance(pull_request, dict):
        raise RuntimeError("Unexpected pullRequest payload while fetching review threads")

    threads = pull_request.get("reviewThreads", {}).get("nodes", [])
    if not isinstance(threads, list):
        raise RuntimeError("Unexpected reviewThreads payload while fetching PR review threads")
    return threads


def _submitted_at_key(review: dict) -> str:
    value = review.get("submittedAt")
    if not isinstance(value, str):
        return ""
    return value


def normalize_review_items(
    threads: list[dict],
    reviews: list[dict],
    pr_author_login: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    stats = {
        "threads_total": 0,
        "threads_resolved": 0,
        "threads_outdated": 0,
        "comments_total": 0,
        "comments_outdated": 0,
        "comments_empty": 0,
        "comments_pr_author": 0,
        "reviews_total": 0,
        "reviews_used": 0,
        "reviews_superseded": 0,
        "reviews_pr_author": 0,
    }
    normalized: list[dict] = []
    author_login = (pr_author_login or "").strip().lower()

    for thread in threads:
        if not isinstance(thread, dict):
            continue
        stats["threads_total"] += 1
        if bool(thread.get("isResolved")):
            stats["threads_resolved"] += 1
            continue
        if bool(thread.get("isOutdated")):
            stats["threads_outdated"] += 1
            continue

        comments = thread.get("comments", {}).get("nodes", [])
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            stats["comments_total"] += 1

            if bool(comment.get("outdated")):
                stats["comments_outdated"] += 1
                continue

            body = str(comment.get("body") or "").strip()
            if not body:
                stats["comments_empty"] += 1
                continue

            comment_author = "unknown"
            author_payload = comment.get("author")
            if isinstance(author_payload, dict):
                comment_author = str(author_payload.get("login") or "unknown")
            if author_login and comment_author.lower() == author_login:
                stats["comments_pr_author"] += 1
                continue

            normalized.append(
                {
                    "type": "review_comment",
                    "author": comment_author,
                    "body": body,
                    "path": str(comment.get("path") or ""),
                    "line": comment.get("line"),
                    "url": str(comment.get("url") or ""),
                }
            )

    latest_review_by_author: dict[str, dict] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        stats["reviews_total"] += 1
        review_author = "unknown"
        author_payload = review.get("author")
        if isinstance(author_payload, dict):
            review_author = str(author_payload.get("login") or "unknown")
        key = review_author.lower()

        existing = latest_review_by_author.get(key)
        if existing is None:
            latest_review_by_author[key] = review
            continue

        if _submitted_at_key(review) >= _submitted_at_key(existing):
            latest_review_by_author[key] = review
        stats["reviews_superseded"] += 1

    for key, review in latest_review_by_author.items():
        review_author = "unknown"
        author_payload = review.get("author")
        if isinstance(author_payload, dict):
            review_author = str(author_payload.get("login") or "unknown")

        if author_login and key == author_login:
            stats["reviews_pr_author"] += 1
            continue

        state = str(review.get("state") or "").strip().upper()
        body = str(review.get("body") or "").strip()
        if not body:
            continue
        if state not in {"CHANGES_REQUESTED", "COMMENTED"}:
            continue

        normalized.append(
            {
                "type": "review_summary",
                "author": review_author,
                "body": body,
                "state": state,
                "url": str(review.get("url") or ""),
            }
        )
        stats["reviews_used"] += 1

    return normalized, stats


def load_linked_issue_context(repo: str, pull_request: dict) -> list[dict]:
    references = pull_request.get("closingIssuesReferences")
    if not isinstance(references, list):
        return []

    linked_issues: list[dict] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        number = reference.get("number")
        if type(number) is not int:
            continue

        title = str(reference.get("title") or "").strip()
        body = str(reference.get("body") or "").strip()
        url = str(reference.get("url") or "").strip()
        if not title or not body or not url:
            issue = fetch_issue(repo=repo, number=number)
            if isinstance(issue, dict):
                title = str(issue.get("title") or title)
                body = str(issue.get("body") or body)
                url = str(issue.get("url") or url)

        linked_issues.append(
            {
                "number": number,
                "title": title,
                "body": body,
                "url": url,
            }
        )
    return linked_issues


def pr_links_issue(pr: dict, issue_number: int) -> bool:
    references = pr.get("closingIssuesReferences")
    if isinstance(references, list):
        for reference in references:
            if isinstance(reference, dict) and reference.get("number") == issue_number:
                return True

    token = f"#{issue_number}"
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    if token in title or token in body:
        return True

    head_ref = str(pr.get("headRefName") or "")
    if re.search(rf"(^|[^0-9]){issue_number}([^0-9]|$)", head_ref):
        return True

    return False


def find_open_pr_for_issue(repo: str, issue_number: int) -> dict | None:
    output = run_capture(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,url,body,headRefName,baseRefName,closingIssuesReferences",
        ]
    )
    prs = json.loads(output)
    if not isinstance(prs, list):
        raise RuntimeError("Unexpected response from gh pr list while searching linked PR")

    for pr in prs:
        if isinstance(pr, dict) and pr_links_issue(pr, issue_number=issue_number):
            return pr
    return None


def fetch_pr_review_comments(repo: str, pr_number: int) -> list[dict]:
    output = run_capture(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr_number}/comments",
            "--method",
            "GET",
            "-f",
            "per_page=100",
        ]
    )
    comments = json.loads(output)
    if not isinstance(comments, list):
        raise RuntimeError("Unexpected response from gh api while fetching PR review comments")

    normalized_comments: list[dict] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        normalized_comments.append(
            {
                "author": str(user.get("login") or "unknown"),
                "path": str(comment.get("path") or ""),
                "line": comment.get("line"),
                "body": str(comment.get("body") or "").strip(),
                "url": str(comment.get("html_url") or ""),
            }
        )
    return normalized_comments


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
    pr_number = pull_request.get("number")
    pr_title = str(pull_request.get("title") or "")
    pr_url = str(pull_request.get("url") or "")
    pr_body = str(pull_request.get("body") or "").strip()
    issue_context_lines: list[str] = []
    for issue in linked_issues or []:
        if not isinstance(issue, dict):
            continue
        issue_number = issue.get("number")
        issue_title = str(issue.get("title") or "")
        issue_url = str(issue.get("url") or "")
        issue_body = str(issue.get("body") or "").strip()
        issue_context_lines.append(
            f"- Issue #{issue_number}: {issue_title}\n  URL: {issue_url}\n  Body: {issue_body}".strip()
        )

    if not issue_context_lines:
        issue_context_lines.append("- No linked issue context found.")

    comment_lines: list[str] = []
    for index, item in enumerate(review_items, start=1):
        item_type = str(item.get("type") or "review_comment")
        author = str(item.get("author") or "unknown")
        body = str(item.get("body") or "").strip()
        url = str(item.get("url") or "")
        path = str(item.get("path") or "")
        line = item.get("line")
        location = path
        if isinstance(line, int):
            location = f"{path}:{line}" if path else str(line)

        if item_type == "review_summary":
            state = str(item.get("state") or "").strip()
            comment_lines.append(
                (
                    f"{index}. Type: review_summary\n"
                    f"   Author: {author}\n"
                    f"   State: {state}\n"
                    f"   Feedback: {body}\n"
                    f"   Link: {url}"
                ).strip()
            )
            continue

        comment_lines.append(
            (
                f"{index}. Type: review_comment\n"
                f"   Author: {author}\n"
                f"   Location: {location or 'unknown-location'}\n"
                f"   Feedback: {body}\n"
                f"   Link: {url}"
            ).strip()
        )

    comments_text = "\n".join(comment_lines)
    issue_context = "\n".join(issue_context_lines)

    return (
        "You are working on an existing GitHub pull request review cycle in the current git branch.\n"
        "Implement the fix requested in PR review comments in repository files.\n"
        "Do not run git commands; git actions are handled by orchestration script.\n\n"
        f"Pull Request: #{pr_number} - {pr_title}\n"
        f"PR URL: {pr_url}\n\n"
        "PR description:\n"
        f"{pr_body}\n\n"
        "Linked issue context:\n"
        f"{issue_context}\n\n"
        "Review comments to address:\n"
        f"{comments_text}\n"
    )


def choose_execution_mode(
    issue_number: int,
    linked_open_pr: dict | None,
    force_issue_flow: bool,
) -> tuple[str, str]:
    if force_issue_flow:
        return "issue-flow", "--force-issue-flow is set"

    if linked_open_pr is None:
        return "issue-flow", f"no open PR linked to issue #{issue_number}"

    pr_number = linked_open_pr.get("number")
    return "pr-review", f"found linked open PR #{pr_number}"


def run_agent(
    issue: dict,
    runner: str,
    agent: str,
    model: str | None,
    dry_run: bool,
    timeout_seconds: int,
    idle_timeout_seconds: int | None,
    opencode_auto_approve: bool,
    prompt_override: str | None = None,
) -> int:
    prompt = prompt_override if prompt_override is not None else build_prompt(issue)
    return run_agent_with_prompt(
        prompt=prompt,
        item_label=f"issue #{issue['number']}",
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
    prepare_issue_branch(
        base_branch=base_branch,
        branch_name=branch_name,
        dry_run=dry_run,
        fail_on_existing=False,
    )


def local_branch_exists(branch_name: str) -> bool:
    return command_succeeds(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"])


def remote_branch_exists(branch_name: str) -> bool:
    return command_succeeds(
        ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch_name}"]
    )


def prepare_issue_branch(
    base_branch: str,
    branch_name: str,
    dry_run: bool,
    fail_on_existing: bool,
) -> str:
    local_exists = local_branch_exists(branch_name)
    remote_exists = remote_branch_exists(branch_name)
    branch_exists = local_exists or remote_exists

    if branch_exists and fail_on_existing:
        raise RuntimeError(
            f"Branch '{branch_name}' already exists and --fail-on-existing is enabled"
        )

    branch_status = "reused" if branch_exists else "created"

    if dry_run:
        if branch_exists:
            print(f"[dry-run] Would reuse existing branch '{branch_name}'")
        else:
            print(f"[dry-run] Would create branch '{branch_name}' from '{base_branch}'")
        return branch_status

    run_command(["git", "checkout", base_branch])

    if local_exists:
        run_command(["git", "checkout", branch_name])
        print(f"Reusing existing branch: {branch_name}")
        return branch_status

    if remote_exists:
        run_command(["git", "checkout", "-b", branch_name, "--track", f"origin/{branch_name}"])
        print(f"Reusing existing remote branch: {branch_name}")
        return branch_status

    run_command(["git", "checkout", "-b", branch_name])
    print(f"Created branch: {branch_name}")
    return branch_status


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


def find_existing_pr(repo: str, base_branch: str, branch_name: str) -> dict | None:
    output = run_capture(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--base",
            base_branch,
            "--head",
            branch_name,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number,url,baseRefName",
        ]
    )
    prs = json.loads(output)
    if not isinstance(prs, list):
        raise RuntimeError("Unexpected response from gh pr list")
    if prs:
        pr = prs[0]
        if not isinstance(pr, dict):
            raise RuntimeError("Unexpected PR entry format from gh pr list")
        return pr

    output = run_capture(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch_name,
            "--state",
            "open",
            "--limit",
            "2",
            "--json",
            "number,url,baseRefName",
        ]
    )
    prs = json.loads(output)
    if not isinstance(prs, list):
        raise RuntimeError("Unexpected response from gh pr list")
    if not prs:
        return None
    if len(prs) > 1:
        raise RuntimeError(
            f"Multiple open PRs found for head '{branch_name}'. Resolve ambiguity manually."
        )

    pr = prs[0]
    if not isinstance(pr, dict):
        raise RuntimeError("Unexpected PR entry format from gh pr list")
    return pr


def ensure_pr(
    repo: str,
    base_branch: str,
    branch_name: str,
    issue: dict,
    dry_run: bool,
    fail_on_existing: bool,
) -> tuple[str, str]:
    existing_pr = find_existing_pr(repo=repo, base_branch=base_branch, branch_name=branch_name)
    if existing_pr is not None:
        pr_url = str(existing_pr.get("url", "")).strip()
        pr_number = existing_pr.get("number")
        existing_base = str(existing_pr.get("baseRefName", "")).strip()
        if fail_on_existing:
            if existing_base and existing_base != base_branch:
                raise RuntimeError(
                    f"PR already exists for branch '{branch_name}' to '{existing_base}' "
                    f"(#{pr_number}; selected base '{base_branch}') and --fail-on-existing is enabled"
                )
            raise RuntimeError(
                f"PR already exists for branch '{branch_name}' to '{base_branch}' "
                f"(#{pr_number}) and --fail-on-existing is enabled"
            )

        if dry_run:
            if existing_base and existing_base != base_branch:
                print(
                    f"[dry-run] Would reuse existing PR #{pr_number} from '{branch_name}' to "
                    f"'{existing_base}' (selected base branch: '{base_branch}')"
                )
            else:
                print(
                    f"[dry-run] Would reuse existing PR #{pr_number} from '{branch_name}' to '{base_branch}'"
                )
        else:
            if existing_base and existing_base != base_branch:
                print(
                    f"Reusing existing PR #{pr_number}: {pr_url} "
                    f"(base '{existing_base}', selected base '{base_branch}')"
                )
            else:
                print(f"Reusing existing PR #{pr_number}: {pr_url}")

        return "reused", pr_url

    pr_url = open_pr(
        repo=repo,
        base_branch=base_branch,
        branch_name=branch_name,
        issue=issue,
        dry_run=dry_run,
    )
    return "created", pr_url


def resolve_local_config_path(raw_path: str | None, target_dir: str) -> str:
    config_path = raw_path or LOCAL_CONFIG_RELATIVE_PATH
    if not os.path.isabs(config_path):
        config_path = os.path.join(target_dir, config_path)
    return os.path.abspath(config_path)


def validate_local_config(config: dict, config_path: str) -> dict:
    supported_keys = {
        "state",
        "limit",
        "runner",
        "agent",
        "model",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "opencode_auto_approve",
        "branch_prefix",
        "include_empty",
        "stop_on_error",
        "fail_on_existing",
        "force_issue_flow",
    }

    unsupported = sorted(set(config) - supported_keys)
    if unsupported:
        unsupported_text = ", ".join(unsupported)
        raise RuntimeError(
            f"Unsupported key(s) in local config {config_path}: {unsupported_text}"
        )

    validated: dict = {}

    if "state" in config:
        if config["state"] not in {"open", "closed", "all"}:
            raise RuntimeError("Local config key 'state' must be one of: open, closed, all")
        validated["state"] = config["state"]

    if "limit" in config:
        if type(config["limit"]) is not int or config["limit"] <= 0:
            raise RuntimeError("Local config key 'limit' must be a positive integer")
        validated["limit"] = config["limit"]

    if "runner" in config:
        if config["runner"] not in {"claude", "opencode"}:
            raise RuntimeError(
                "Local config key 'runner' must be one of: claude, opencode"
            )
        validated["runner"] = config["runner"]

    if "agent" in config:
        if not isinstance(config["agent"], str) or not config["agent"].strip():
            raise RuntimeError("Local config key 'agent' must be a non-empty string")
        validated["agent"] = config["agent"]

    if "model" in config:
        if config["model"] is not None and not isinstance(config["model"], str):
            raise RuntimeError("Local config key 'model' must be a string or null")
        validated["model"] = config["model"]

    if "agent_timeout_seconds" in config:
        value = config["agent_timeout_seconds"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(
                "Local config key 'agent_timeout_seconds' must be a positive integer"
            )
        validated["agent_timeout_seconds"] = value

    if "agent_idle_timeout_seconds" in config:
        value = config["agent_idle_timeout_seconds"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                "Local config key 'agent_idle_timeout_seconds' must be a positive integer or null"
            )
        validated["agent_idle_timeout_seconds"] = value

    for key in [
        "opencode_auto_approve",
        "include_empty",
        "stop_on_error",
        "fail_on_existing",
        "force_issue_flow",
    ]:
        if key in config:
            if not isinstance(config[key], bool):
                raise RuntimeError(f"Local config key '{key}' must be a boolean")
            validated[key] = config[key]

    if "branch_prefix" in config:
        if not isinstance(config["branch_prefix"], str) or not config["branch_prefix"].strip():
            raise RuntimeError(
                "Local config key 'branch_prefix' must be a non-empty string"
            )
        validated["branch_prefix"] = config["branch_prefix"]

    return validated


def load_local_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, encoding="utf-8") as config_file:
            data = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in local config {config_path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read local config {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Local config {config_path} must contain a JSON object")

    return validate_local_config(config=data, config_path=config_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub issues with gh and run an AI agent for each issue body."
    )
    parser.add_argument(
        "--repo", help="GitHub repo in owner/name format. Defaults to current gh repo."
    )
    parser.add_argument(
        "--issue",
        type=int,
        help="Process a single issue by number, ignoring --limit and --state.",
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="Process a single pull request by number (requires --from-review-comments).",
    )
    parser.add_argument(
        "--from-review-comments",
        action="store_true",
        help="Enable PR review-comments mode (must be used with --pr).",
    )
    parser.add_argument(
        "--state",
        default=BUILTIN_DEFAULTS["state"],
        choices=["open", "closed", "all"],
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=BUILTIN_DEFAULTS["limit"],
        help="Maximum number of issues to process.",
    )
    parser.add_argument(
        "--runner",
        default=BUILTIN_DEFAULTS["runner"],
        choices=["claude", "opencode"],
        help="AI agent runner to use (default: claude).",
    )
    parser.add_argument(
        "--agent",
        default=BUILTIN_DEFAULTS["agent"],
        help="Opencode agent name (only used with --runner opencode).",
    )
    parser.add_argument(
        "--model",
        help=(
            "Optional model override. For Claude: e.g. claude-sonnet-4-6. "
            "For OpenCode: e.g. openai/gpt-4o."
        ),
    )
    parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        default=BUILTIN_DEFAULTS["agent_timeout_seconds"],
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
        default=BUILTIN_DEFAULTS["branch_prefix"],
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
        "--fail-on-existing",
        action="store_true",
        help=(
            "Fail instead of reusing existing issue branch/PR. By default existing "
            "branch/PR are reused when possible."
        ),
    )
    parser.add_argument(
        "--force-issue-flow",
        action="store_true",
        help=(
            "Disable auto-switch to PR-review mode when --issue has a linked open PR. "
            "Keeps legacy issue-flow behavior."
        ),
    )
    parser.add_argument(
        "--dir",
        default=BUILTIN_DEFAULTS["dir"],
        help="Path to the local git repository to operate on. Defaults to the current directory.",
    )
    parser.add_argument(
        "--local-config",
        help=(
            "Path to local JSON config with user-specific defaults. "
            "Defaults to local-config.json under --dir."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without running the agent."
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument("--dir", default=BUILTIN_DEFAULTS["dir"])
    bootstrap_parser.add_argument("--local-config")
    bootstrap_args, _ = bootstrap_parser.parse_known_args(argv)

    target_dir = os.path.abspath(bootstrap_args.dir)
    local_config_path = resolve_local_config_path(bootstrap_args.local_config, target_dir)
    local_defaults = load_local_config(local_config_path)

    parser = build_parser()
    parser.set_defaults(**local_defaults)
    parser.set_defaults(local_config=local_config_path)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    issue_number_arg = getattr(args, "issue", None)
    pr_number_arg = getattr(args, "pr", None)
    from_review_comments = bool(getattr(args, "from_review_comments", False))
    force_issue_flow = bool(getattr(args, "force_issue_flow", False))
    has_force_issue_flow_flag = hasattr(args, "force_issue_flow")
    pr_followup_branch_prefix = getattr(args, "pr_followup_branch_prefix", None)
    post_pr_summary = bool(getattr(args, "post_pr_summary", False))

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
        if issue_number_arg is not None and pr_number_arg is not None:
            raise RuntimeError("Use either --issue or --pr, not both.")
        pr_mode_requested = pr_number_arg is not None or from_review_comments
        if from_review_comments and pr_number_arg is None:
            raise RuntimeError("--from-review-comments requires --pr <number>.")
        if pr_number_arg is not None and not from_review_comments:
            raise RuntimeError("--pr requires --from-review-comments.")

        ensure_clean_worktree()
        repo = args.repo or detect_repo()
        if pr_mode_requested:
            base_branch = ""
            issues = []
        else:
            base_branch = detect_default_branch(repo)
            mode_label = "[dry-run]" if args.dry_run else ""
            if mode_label:
                print(f"{mode_label} Selected stable base branch: {base_branch}")
            else:
                print(f"Selected stable base branch: {base_branch}")

            if issue_number_arg is not None:
                issues = [fetch_issue(repo=repo, number=issue_number_arg)]
            else:
                issues = fetch_issues(repo=repo, state=args.state, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if pr_mode_requested:
        try:
            if type(pr_number_arg) is not int:
                raise RuntimeError("--pr must be an integer pull request number")

            pull_request = fetch_pull_request(repo=repo, number=pr_number_arg)
            pr_state = str(pull_request.get("state") or "").strip().upper()
            if pr_state != "OPEN":
                print(f"PR #{pr_number_arg} is not open (state: {pr_state}); skipping.")
                return 0

            threads = fetch_pr_review_threads(repo=repo, number=pr_number_arg)
            reviews = pull_request.get("reviews")
            if not isinstance(reviews, list):
                reviews = []

            pr_author_payload = pull_request.get("author")
            pr_author_login = ""
            if isinstance(pr_author_payload, dict):
                pr_author_login = str(pr_author_payload.get("login") or "")

            review_items, review_stats = normalize_review_items(
                threads=threads,
                reviews=reviews,
                pr_author_login=pr_author_login,
            )
            if not review_items:
                print(
                    "No actionable review comments found "
                    f"for PR #{pr_number_arg}; nothing to do."
                )
                if args.dry_run:
                    print(f"[dry-run] Review filtering stats: {review_stats}")
                return 0

            linked_issues = load_linked_issue_context(repo=repo, pull_request=pull_request)
            prompt = build_pr_review_prompt(
                pull_request=pull_request,
                review_items=review_items,
                linked_issues=linked_issues,
            )

            base_branch_for_run = current_branch()
            active_branch = base_branch_for_run
            if pr_followup_branch_prefix:
                followup_branch = (
                    f"{pr_followup_branch_prefix}/pr-{pr_number_arg}-review-comments"
                )
                create_followup_branch(
                    current_branch_name=base_branch_for_run,
                    branch_name=followup_branch,
                    dry_run=args.dry_run,
                )
                active_branch = followup_branch

            exit_code = run_agent_with_prompt(
                prompt=prompt,
                item_label=f"PR #{pr_number_arg}",
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
                    f"Agent failed for PR #{pr_number_arg} with exit code {exit_code}"
                )

            if not args.dry_run and not has_changes():
                print(f"No changes detected for PR #{pr_number_arg}; skipping commit and push")
                if pr_followup_branch_prefix:
                    run_command(["git", "checkout", base_branch_for_run])
                return 0

            commit_pr_review_changes(pull_request=pull_request, dry_run=args.dry_run)
            if pr_followup_branch_prefix:
                push_branch(branch_name=active_branch, dry_run=args.dry_run)
                print(f"Pushed follow-up branch for PR #{pr_number_arg}: {active_branch}")
            else:
                push_current_branch(dry_run=args.dry_run)

            if post_pr_summary:
                leave_pr_summary_comment(
                    repo=repo,
                    pr_number=pr_number_arg,
                    review_items_count=len(review_items),
                    dry_run=args.dry_run,
                )

            if not args.dry_run and pr_followup_branch_prefix:
                run_command(["git", "checkout", base_branch_for_run])

            print(
                f"Done. Processed PR #{pr_number_arg} with {len(review_items)} actionable review items."
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if not issues:
        print("No issues found.")
        return 0

    failures = 0
    processed = 0
    touched_prs: list[str] = []

    for issue in issues:
        try:
            linked_open_pr: dict | None = None
            mode = "issue-flow"
            mode_reason = "batch issue processing"
            if issue_number_arg is not None and has_force_issue_flow_flag:
                linked_open_pr = find_open_pr_for_issue(repo=repo, issue_number=issue["number"])
                mode, mode_reason = choose_execution_mode(
                    issue_number=issue["number"],
                    linked_open_pr=linked_open_pr,
                    force_issue_flow=force_issue_flow,
                )

            body = (issue.get("body") or "").strip()
            if mode == "issue-flow" and not body and not args.include_empty:
                print(f"Skipping issue #{issue['number']} (empty body)")
                continue

            processed += 1

            prompt_override: str | None = None
            if mode == "pr-review":
                if linked_open_pr is None:
                    raise RuntimeError(
                        f"Internal error: PR-review mode selected without linked PR for issue #{issue['number']}"
                    )

                pr_number_raw = linked_open_pr.get("number")
                if type(pr_number_raw) is not int:
                    raise RuntimeError(
                        f"Linked PR for issue #{issue['number']} has invalid number: {pr_number_raw}"
                    )
                pr_number = pr_number_raw

                print(
                    f"Auto-switch to PR-review mode for issue #{issue['number']}: {mode_reason}."
                )
                pull_request = fetch_pull_request(repo=repo, number=pr_number)
                thread_items = fetch_pr_review_threads(repo=repo, number=pr_number)
                pr_reviews = pull_request.get("reviews")
                if not isinstance(pr_reviews, list):
                    pr_reviews = []
                pr_author_payload = pull_request.get("author")
                pr_author_login = ""
                if isinstance(pr_author_payload, dict):
                    pr_author_login = str(pr_author_payload.get("login") or "")

                review_items, _review_stats = normalize_review_items(
                    threads=thread_items,
                    reviews=pr_reviews,
                    pr_author_login=pr_author_login,
                )
                if not review_items:
                    print(
                        f"No actionable review comments for linked PR #{pr_number}; "
                        "skipping issue run."
                    )
                    continue

                linked_issues = load_linked_issue_context(repo=repo, pull_request=pull_request)
                prompt_override = build_pr_review_prompt(
                    pull_request=pull_request,
                    review_items=review_items,
                    linked_issues=linked_issues,
                )
                issue_branch = str(linked_open_pr.get("headRefName") or "").strip()
                if not issue_branch:
                    raise RuntimeError(
                        f"Linked PR #{pr_number} has empty headRefName; cannot select working branch"
                    )
                target_base_branch = str(linked_open_pr.get("baseRefName") or base_branch).strip()
                if not target_base_branch:
                    target_base_branch = base_branch

                review_comment_count = len(review_items)
                if args.dry_run:
                    print(
                        "[dry-run] Selected mode: pr-review "
                        f"(reason: {mode_reason}; PR #{pr_number}; review comments: {review_comment_count})"
                    )
                else:
                    print(
                        f"Selected mode: pr-review (reason: {mode_reason}; "
                        f"PR #{pr_number}; review comments: {review_comment_count})"
                    )
            else:
                if issue_number_arg is not None:
                    if args.dry_run:
                        print(
                            f"[dry-run] Selected mode: issue-flow (reason: {mode_reason})"
                        )
                    else:
                        print(f"Selected mode: issue-flow (reason: {mode_reason})")
                issue_branch = branch_name_for_issue(issue=issue, prefix=args.branch_prefix)
                target_base_branch = base_branch

            branch_status = prepare_issue_branch(
                base_branch=target_base_branch,
                branch_name=issue_branch,
                dry_run=args.dry_run,
                fail_on_existing=args.fail_on_existing,
            )
            print(f"Branch status for issue #{issue['number']}: {branch_status}")

            exit_code = run_agent(
                issue=issue,
                runner=args.runner,
                agent=args.agent,
                model=args.model,
                dry_run=args.dry_run,
                timeout_seconds=args.agent_timeout_seconds,
                idle_timeout_seconds=args.agent_idle_timeout_seconds,
                opencode_auto_approve=args.opencode_auto_approve,
                prompt_override=prompt_override,
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
            pr_status, pr_url = ensure_pr(
                repo=repo,
                base_branch=target_base_branch,
                branch_name=issue_branch,
                issue=issue,
                dry_run=args.dry_run,
                fail_on_existing=args.fail_on_existing,
            )
            if pr_url:
                touched_prs.append(pr_url)
                print(f"PR status for issue #{issue['number']}: {pr_status} ({pr_url})")

            if not args.dry_run:
                run_command(["git", "checkout", base_branch])
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"Issue #{issue['number']} failed: {exc}", file=sys.stderr)
            if args.stop_on_error:
                break

    print(f"Done. Processed: {processed}, failures: {failures}")
    if touched_prs:
        print("PRs:")
        for pr_url in touched_prs:
            print(f"- {pr_url}")
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
