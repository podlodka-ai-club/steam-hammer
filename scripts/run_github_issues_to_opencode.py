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
    "sync_reused_branch": True,
    "sync_strategy": "rebase",
    "dir": ".",
}

ORCHESTRATION_STATE_MARKER = "<!-- orchestration-state:v1 -->"
ORCHESTRATION_STATE_STATUSES = {
    "in-progress",
    "ready-for-review",
    "failed",
    "blocked",
    "waiting-for-author",
    "waiting-for-ci",
    "ready-to-merge",
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


def current_head_sha() -> str:
    return run_capture(["git", "rev-parse", "HEAD"]).strip()


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
            "number,title,body,url,state,mergeStateStatus,headRefName,baseRefName,author,closingIssuesReferences,reviews",
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


def _first_json_object(raw: str) -> dict:
    start = raw.find("{")
    if start < 0:
        raise ValueError("state payload is missing JSON object")
    payload, _offset = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(payload, dict):
        raise ValueError("state payload JSON must be an object")
    return payload


def parse_orchestration_state_comment_body(body: str) -> tuple[dict | None, str | None]:
    if ORCHESTRATION_STATE_MARKER not in body:
        return None, None

    after_marker = body.split(ORCHESTRATION_STATE_MARKER, maxsplit=1)[1].strip()
    if not after_marker:
        return None, "marker found but payload is empty"

    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", after_marker, flags=re.DOTALL)
    candidates = fenced_matches if fenced_matches else [after_marker]

    parse_errors: list[str] = []
    for candidate in candidates:
        try:
            return _first_json_object(candidate), None
        except (ValueError, json.JSONDecodeError) as exc:
            parse_errors.append(str(exc))

    if parse_errors:
        return None, parse_errors[-1]
    return None, "unable to parse state payload"


def normalize_orchestration_state_status(state_payload: dict) -> str:
    status_raw = state_payload.get("status")
    if not isinstance(status_raw, str):
        status_raw = state_payload.get("state")
    return str(status_raw or "").strip().lower()


def select_latest_parseable_orchestration_state(
    comments: list[dict],
    source_label: str,
) -> tuple[dict | None, list[str]]:
    latest: dict | None = None
    warnings: list[str] = []

    for comment in comments:
        if not isinstance(comment, dict):
            continue

        body = str(comment.get("body") or "")
        payload, error = parse_orchestration_state_comment_body(body)
        if payload is None:
            if error:
                created_at = str(comment.get("created_at") or "unknown-time")
                url = str(comment.get("html_url") or "")
                context = f" at {url}" if url else ""
                warnings.append(
                    f"ignoring malformed orchestration state comment in {source_label}"
                    f" ({created_at}){context}: {error}"
                )
            continue

        created_at = str(comment.get("created_at") or "")
        candidate = {
            "source": source_label,
            "created_at": created_at,
            "url": str(comment.get("html_url") or ""),
            "comment_id": comment.get("id"),
            "payload": payload,
            "status": normalize_orchestration_state_status(payload),
        }
        if latest is None or created_at >= str(latest.get("created_at") or ""):
            latest = candidate

    return latest, warnings


def merge_latest_recovered_state(states: list[dict | None]) -> dict | None:
    latest: dict | None = None
    for state in states:
        if state is None:
            continue
        if latest is None or str(state.get("created_at") or "") >= str(latest.get("created_at") or ""):
            latest = state
    return latest


def format_recovered_state_context(state: dict) -> str:
    status = str(state.get("status") or "unknown")
    source = str(state.get("source") or "unknown")
    created_at = str(state.get("created_at") or "unknown-time")
    url = str(state.get("url") or "")
    details = f"status={status}; source={source}; created_at={created_at}"
    if url:
        details += f"; comment={url}"
    return details


def build_recovered_failure_context_note(state: dict) -> str:
    payload = state.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    reason = str(
        payload.get("failure")
        or payload.get("error")
        or payload.get("reason")
        or payload.get("summary")
        or ""
    ).strip()
    if not reason:
        reason = "No explicit failure details were provided in prior state comment."

    return (
        "Recovered previous orchestration failure context:\n"
        f"- {format_recovered_state_context(state)}\n"
        f"- failure-context: {reason}"
    )


def append_recovered_context_to_prompt(prompt: str, note: str | None) -> str:
    if not note:
        return prompt
    return f"{prompt.rstrip()}\n\n{note}\n"


def _canonical_feedback_text(body: str) -> str:
    return re.sub(r"\s+", " ", body.strip().lower())


def _is_actionable_feedback(body: str) -> bool:
    text = _canonical_feedback_text(body)
    if not text:
        return False

    if re.fullmatch(r"[\W_]+", text):
        return False

    non_actionable_exact = {
        "lgtm",
        "looks good",
        "looks good to me",
        "approved",
        "ship it",
        "thanks",
        "thank you",
        "great work",
        "+1",
        "done",
    }
    if text in non_actionable_exact:
        return False

    actionable_markers = [
        r"\bplease\b",
        r"\bcan you\b",
        r"\bshould\b",
        r"\bneed(?:s|ed)?\b",
        r"\bmust\b",
        r"\bfix\b",
        r"\bchange\b",
        r"\bupdate\b",
        r"\brename\b",
        r"\badd\b",
        r"\bremove\b",
        r"\bconsider\b",
        r"\bavoid\b",
        r"\buse\b",
        r"\bnit\b",
        r"\btodo\b",
        r"\bfollow up\b",
    ]
    for marker in actionable_markers:
        if re.search(marker, text):
            return True

    if "`" in body or "\n" in body:
        return True

    return False


def _dedupe_review_items(items: list[dict], stats: dict[str, int]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for item in items:
        item_type = str(item.get("type") or "")
        author = str(item.get("author") or "").strip().lower()
        body = _canonical_feedback_text(str(item.get("body") or ""))
        key = (author, body)
        if key in seen:
            if item_type == "review_comment":
                stats["comments_duplicates"] += 1
            elif item_type == "review_summary":
                stats["reviews_duplicates"] += 1
            elif item_type == "conversation_comment":
                stats["conversation_duplicates"] += 1
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def normalize_review_items(
    threads: list[dict],
    reviews: list[dict],
    conversation_comments: list[dict] | None = None,
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
        "comments_duplicates": 0,
        "comments_used": 0,
        "reviews_total": 0,
        "reviews_used": 0,
        "reviews_superseded": 0,
        "reviews_pr_author": 0,
        "reviews_empty": 0,
        "reviews_non_actionable": 0,
        "reviews_duplicates": 0,
        "conversation_total": 0,
        "conversation_used": 0,
        "conversation_pr_author": 0,
        "conversation_empty": 0,
        "conversation_non_actionable": 0,
        "conversation_duplicates": 0,
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
            stats["comments_used"] += 1

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

        state = str(review.get("state") or "").strip().upper()
        body = str(review.get("body") or "").strip()
        if not body:
            stats["reviews_empty"] += 1
            continue
        if state not in {"CHANGES_REQUESTED", "COMMENTED", "APPROVED"}:
            continue
        if state in {"COMMENTED", "APPROVED"} and not _is_actionable_feedback(body):
            stats["reviews_non_actionable"] += 1
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

    for comment in conversation_comments or []:
        if not isinstance(comment, dict):
            continue
        stats["conversation_total"] += 1

        body = str(comment.get("body") or "").strip()
        if not body:
            stats["conversation_empty"] += 1
            continue

        comment_author = str(comment.get("author") or "unknown")
        if author_login and comment_author.lower() == author_login:
            stats["conversation_pr_author"] += 1

        if not _is_actionable_feedback(body):
            stats["conversation_non_actionable"] += 1
            continue

        normalized.append(
            {
                "type": "conversation_comment",
                "author": comment_author,
                "body": body,
                "url": str(comment.get("url") or ""),
            }
        )
        stats["conversation_used"] += 1

    normalized = _dedupe_review_items(items=normalized, stats=stats)
    return normalized, stats


def format_review_filtering_stats(stats: dict[str, int]) -> str:
    inline_summary = (
        "inline="
        f"total:{stats.get('comments_total', 0)} "
        f"included:{stats.get('comments_used', 0)}(from_pr_author:{stats.get('comments_pr_author', 0)}) "
        f"excluded(outdated:{stats.get('comments_outdated', 0)}, "
        f"empty:{stats.get('comments_empty', 0)}, "
        f"duplicates:{stats.get('comments_duplicates', 0)})"
    )
    summary_review = (
        "review_summaries="
        f"total:{stats.get('reviews_total', 0)} "
        f"included:{stats.get('reviews_used', 0)}(from_pr_author:{stats.get('reviews_pr_author', 0)}) "
        f"excluded(superseded:{stats.get('reviews_superseded', 0)}, "
        f"empty:{stats.get('reviews_empty', 0)}, "
        f"non_actionable:{stats.get('reviews_non_actionable', 0)}, "
        f"duplicates:{stats.get('reviews_duplicates', 0)})"
    )
    conversation_summary = (
        "conversation="
        f"total:{stats.get('conversation_total', 0)} "
        f"included:{stats.get('conversation_used', 0)}(from_pr_author:{stats.get('conversation_pr_author', 0)}) "
        f"excluded(empty:{stats.get('conversation_empty', 0)}, "
        f"non_actionable:{stats.get('conversation_non_actionable', 0)}, "
        f"duplicates:{stats.get('conversation_duplicates', 0)})"
    )
    thread_summary = (
        "threads="
        f"total:{stats.get('threads_total', 0)} "
        f"excluded(resolved:{stats.get('threads_resolved', 0)}, "
        f"outdated:{stats.get('threads_outdated', 0)})"
    )
    return f"{thread_summary}; {inline_summary}; {summary_review}; {conversation_summary}"


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


def fetch_issue_comments(repo: str, issue_number: int) -> list[dict]:
    output = run_capture(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{issue_number}/comments",
            "--method",
            "GET",
            "-f",
            "per_page=100",
        ]
    )
    comments = json.loads(output)
    if not isinstance(comments, list):
        raise RuntimeError("Unexpected response from gh api while fetching issue comments")
    return comments


def fetch_pr_conversation_comments(repo: str, pr_number: int) -> list[dict]:
    comments = fetch_issue_comments(repo=repo, issue_number=pr_number)

    normalized_comments: list[dict] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        normalized_comments.append(
            {
                "author": str(user.get("login") or "unknown"),
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


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def short_error_text(error: str, max_len: int = 280) -> str:
    compact = " ".join(str(error).split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[: max_len - 3].rstrip()}..."


def parse_pr_number_from_url(pr_url: str) -> int | None:
    match = re.search(r"/pull/(\d+)(?:$|[/?#])", pr_url)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def build_orchestration_state(
    status: str,
    task_type: str,
    issue_number: int | None,
    pr_number: int | None,
    branch: str | None,
    base_branch: str | None,
    runner: str,
    agent: str,
    model: str | None,
    attempt: int,
    stage: str,
    next_action: str,
    error: str | None,
) -> dict:
    if status not in ORCHESTRATION_STATE_STATUSES:
        raise RuntimeError(f"Unsupported orchestration state status: {status}")
    if task_type not in {"issue", "pr"}:
        raise RuntimeError(f"Unsupported orchestration task type: {task_type}")

    return {
        "status": status,
        "task_type": task_type,
        "issue": issue_number,
        "pr": pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "runner": runner,
        "agent": agent,
        "model": model,
        "attempt": attempt,
        "stage": stage,
        "next_action": next_action,
        "error": error,
        "timestamp": utc_now_iso(),
    }


def format_orchestration_state_comment(state: dict) -> str:
    status = str(state.get("status") or "unknown")
    task_type = str(state.get("task_type") or "unknown")
    stage = str(state.get("stage") or "unknown")
    next_action = str(state.get("next_action") or "unknown")
    readable_header = (
        f"Orchestration state update: {status} ({task_type}, stage={stage}, next={next_action})."
    )
    return (
        f"{readable_header}\n\n"
        f"{ORCHESTRATION_STATE_MARKER}\n"
        f"```json\n{json.dumps(state, ensure_ascii=True, indent=2)}\n```"
    )


def post_orchestration_state_comment(
    repo: str,
    target_type: str,
    target_number: int,
    state: dict,
    dry_run: bool,
) -> None:
    if target_type not in {"issue", "pr"}:
        raise RuntimeError(f"Unsupported state comment target type: {target_type}")

    body = format_orchestration_state_comment(state)
    if dry_run:
        print(
            f"[dry-run] Would post orchestration state to {target_type} #{target_number}: "
            f"status={state.get('status')} stage={state.get('stage')}"
        )
        return

    run_command(
        [
            "gh",
            target_type,
            "comment",
            str(target_number),
            "--repo",
            repo,
            "--body",
            body,
        ]
    )


def safe_post_orchestration_state_comment(
    repo: str,
    target_type: str,
    target_number: int,
    state: dict,
    dry_run: bool,
) -> None:
    try:
        post_orchestration_state_comment(
            repo=repo,
            target_type=target_type,
            target_number=target_number,
            state=state,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to post orchestration state to {target_type} #{target_number}: {exc}",
            file=sys.stderr,
        )


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

        if item_type == "conversation_comment":
            comment_lines.append(
                (
                    f"{index}. Type: conversation_comment\n"
                    f"   Author: {author}\n"
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
    recovered_state: dict | None = None,
) -> tuple[str, str]:
    if force_issue_flow:
        return "issue-flow", "--force-issue-flow is set"

    recovered_status = ""
    if isinstance(recovered_state, dict):
        recovered_status = str(recovered_state.get("status") or "")

    if recovered_status == "ready-for-review" and linked_open_pr is not None:
        pr_number = linked_open_pr.get("number")
        return (
            "pr-review",
            f"recovered orchestration state is ready-for-review and linked open PR #{pr_number} exists",
        )

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


def list_conflicted_paths() -> list[str]:
    output = run_capture(["git", "diff", "--name-only", "--diff-filter=U"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def auto_resolve_merge_conflicts_with_base() -> int:
    conflicted_paths = list_conflicted_paths()
    if not conflicted_paths:
        raise RuntimeError("Merge failed, but no conflicted files were detected")

    for path in conflicted_paths:
        run_command(["git", "checkout", "--theirs", "--", path])

    run_command(["git", "add", "-A"])
    run_command(["git", "commit", "--no-edit"])
    return len(conflicted_paths)


def merge_sync_with_auto_resolution(remote_base_ref: str, branch_name: str) -> bool:
    before_sync_sha = current_head_sha()
    print(
        f"Sync attempt: merge reused branch '{branch_name}' with '{remote_base_ref}' "
        "using base-favored strategy"
    )

    try:
        run_command(["git", "merge", "--no-edit", "-X", "theirs", remote_base_ref])
    except RuntimeError:
        print(
            f"Conflict detected during merge sync for reused branch '{branch_name}'; "
            "auto-resolving by keeping selected base branch changes"
        )
        try:
            resolved_files_count = auto_resolve_merge_conflicts_with_base()
            print(
                f"Auto-resolved {resolved_files_count} conflicted file(s) "
                f"for reused branch '{branch_name}' via base-favored merge resolution"
            )
        except Exception as resolve_exc:  # noqa: BLE001
            command_succeeds(["git", "merge", "--abort"])
            raise RuntimeError(
                f"Failed to auto-resolve merge conflicts while syncing reused branch "
                f"'{branch_name}' with '{remote_base_ref}'. "
                "Resolve conflicts manually or rerun with --no-sync-reused-branch."
            ) from resolve_exc
    after_sync_sha = current_head_sha()
    synced = before_sync_sha != after_sync_sha
    if synced:
        print(f"Reused branch '{branch_name}' updated after sync")
    else:
        print(f"Reused branch '{branch_name}' already up to date with '{remote_base_ref}'")
    return synced


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


def sync_reused_branch_with_base(
    base_branch: str,
    branch_name: str,
    strategy: str,
    dry_run: bool,
) -> bool:
    if strategy not in {"rebase", "merge"}:
        raise RuntimeError(
            f"Unsupported sync strategy '{strategy}'. Use one of: rebase, merge"
        )

    remote_base_ref = f"origin/{base_branch}"

    if dry_run:
        print(
            f"[dry-run] Would sync reused branch '{branch_name}' with '{remote_base_ref}' "
            f"using '{strategy}' strategy"
        )
        return False

    print(
        f"Sync attempt: reused branch '{branch_name}' with '{remote_base_ref}' "
        f"using '{strategy}' strategy"
    )

    run_command(["git", "fetch", "origin", base_branch])

    if strategy == "merge":
        return merge_sync_with_auto_resolution(
            remote_base_ref=remote_base_ref,
            branch_name=branch_name,
        )

    before_sync_sha = current_head_sha()
    try:
        run_command(["git", "rebase", remote_base_ref])
    except RuntimeError:
        command_succeeds(["git", "rebase", "--abort"])
        print(
            f"Conflict detected during rebase sync for reused branch '{branch_name}'; "
            "switching to merge-based auto-resolution"
        )
        return merge_sync_with_auto_resolution(
            remote_base_ref=remote_base_ref,
            branch_name=branch_name,
        )

    after_sync_sha = current_head_sha()
    synced = before_sync_sha != after_sync_sha
    if synced:
        print(f"Reused branch '{branch_name}' updated after rebase sync")
    else:
        print(f"Reused branch '{branch_name}' already up to date with '{remote_base_ref}'")
    return synced


def commit_changes(issue: dict, dry_run: bool) -> str:
    message = f"Fix issue #{issue['number']}: {issue['title']}"
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
    run_command(["git", "add", "-A"])
    run_command(["git", "commit", "-m", message])
    return message


def push_branch(branch_name: str, dry_run: bool, force_with_lease: bool = False) -> None:
    command = ["git", "push", "-u", "origin", branch_name]
    if force_with_lease:
        command.insert(3, "--force-with-lease")

    if dry_run:
        if force_with_lease:
            print(
                f"[dry-run] Would push branch '{branch_name}' to origin with --force-with-lease"
            )
        else:
            print(f"[dry-run] Would push branch '{branch_name}' to origin")
        return
    run_command(command)


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
        "sync_reused_branch",
        "sync_strategy",
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
        "sync_reused_branch",
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

    if "sync_strategy" in config:
        if config["sync_strategy"] not in {"rebase", "merge"}:
            raise RuntimeError("Local config key 'sync_strategy' must be one of: rebase, merge")
        validated["sync_strategy"] = config["sync_strategy"]

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
        "--sync-reused-branch",
        dest="sync_reused_branch",
        action="store_true",
        default=BUILTIN_DEFAULTS["sync_reused_branch"],
        help=(
            "Sync reused issue branches with the selected base branch before running the "
            "agent. Enabled by default."
        ),
    )
    parser.add_argument(
        "--no-sync-reused-branch",
        dest="sync_reused_branch",
        action="store_false",
        help="Disable sync for reused issue branches before the agent step.",
    )
    parser.add_argument(
        "--sync-strategy",
        default=BUILTIN_DEFAULTS["sync_strategy"],
        choices=["rebase", "merge"],
        help=(
            "Strategy to sync reused issue branches with base before agent run "
            "(default: rebase)."
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
            failure_stage = "pr_review_init"
            pr_state_context: dict[str, int | str | None] = {
                "issue": None,
                "pr": None,
                "branch": None,
                "base_branch": None,
            }
            if type(pr_number_arg) is not int:
                raise RuntimeError("--pr must be an integer pull request number")

            failure_stage = "fetch_pr"
            pull_request = fetch_pull_request(repo=repo, number=pr_number_arg)
            pr_state_context["pr"] = pr_number_arg
            pr_state = str(pull_request.get("state") or "").strip().upper()
            if pr_state != "OPEN":
                print(f"PR #{pr_number_arg} is not open (state: {pr_state}); skipping.")
                return 0

            recovered_pr_state: dict | None = None
            try:
                pr_comments = fetch_issue_comments(repo=repo, issue_number=pr_number_arg)
                recovered_pr_state, pr_state_warnings = select_latest_parseable_orchestration_state(
                    comments=pr_comments,
                    source_label=f"pr #{pr_number_arg}",
                )
                for warning in pr_state_warnings:
                    print(f"Warning: {warning}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(
                    "Warning: unable to recover orchestration state from "
                    f"PR #{pr_number_arg} comments: {exc}",
                    file=sys.stderr,
                )
            if recovered_pr_state is not None:
                prefix = "[dry-run] " if args.dry_run else ""
                print(
                    f"{prefix}Recovered orchestration state context: "
                    f"{format_recovered_state_context(recovered_pr_state)}"
                )

            failure_stage = "fetch_review_feedback"
            threads = fetch_pr_review_threads(repo=repo, number=pr_number_arg)
            conversation_comments = fetch_pr_conversation_comments(
                repo=repo,
                pr_number=pr_number_arg,
            )
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
                conversation_comments=conversation_comments,
                pr_author_login=pr_author_login,
            )
            print(
                "Review prompt sources: "
                f"{format_review_filtering_stats(review_stats)}"
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

            pr_state_context["branch"] = active_branch
            pr_state_context["base_branch"] = str(pull_request.get("baseRefName") or "").strip() or None
            safe_post_orchestration_state_comment(
                repo=repo,
                target_type="pr",
                target_number=pr_number_arg,
                dry_run=args.dry_run,
                state=build_orchestration_state(
                    status="in-progress",
                    task_type="pr",
                    issue_number=None,
                    pr_number=pr_number_arg,
                    branch=active_branch,
                    base_branch=str(pr_state_context["base_branch"] or "") or None,
                    runner=args.runner,
                    agent=args.agent,
                    model=args.model,
                    attempt=1,
                    stage="agent_run",
                    next_action="wait_for_agent_result",
                    error=None,
                ),
            )

            if not review_items:
                safe_post_orchestration_state_comment(
                    repo=repo,
                    target_type="pr",
                    target_number=pr_number_arg,
                    dry_run=args.dry_run,
                    state=build_orchestration_state(
                        status="waiting-for-author",
                        task_type="pr",
                        issue_number=None,
                        pr_number=pr_number_arg,
                        branch=active_branch,
                        base_branch=str(pr_state_context["base_branch"] or "") or None,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        attempt=1,
                        stage="review_feedback",
                        next_action="await_new_review_comments",
                        error="No actionable review comments found",
                    ),
                )
                print(
                    "No actionable review comments found "
                    f"for PR #{pr_number_arg}; nothing to do."
                )
                return 0

            linked_issues = load_linked_issue_context(repo=repo, pull_request=pull_request)
            prompt = build_pr_review_prompt(
                pull_request=pull_request,
                review_items=review_items,
                linked_issues=linked_issues,
            )
            if (
                recovered_pr_state is not None
                and str(recovered_pr_state.get("status") or "") == "failed"
            ):
                prompt = append_recovered_context_to_prompt(
                    prompt,
                    build_recovered_failure_context_note(recovered_pr_state),
                )

            failure_stage = "agent_run"
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
                safe_post_orchestration_state_comment(
                    repo=repo,
                    target_type="pr",
                    target_number=pr_number_arg,
                    dry_run=False,
                    state=build_orchestration_state(
                        status="waiting-for-author",
                        task_type="pr",
                        issue_number=None,
                        pr_number=pr_number_arg,
                        branch=active_branch,
                        base_branch=str(pr_state_context["base_branch"] or "") or None,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        attempt=1,
                        stage="post_agent_check",
                        next_action="await_more_feedback_or_manual_changes",
                        error="Agent produced no repository changes",
                    ),
                )
                print(f"No changes detected for PR #{pr_number_arg}; skipping commit and push")
                if pr_followup_branch_prefix:
                    run_command(["git", "checkout", base_branch_for_run])
                return 0

            failure_stage = "commit_push"
            commit_pr_review_changes(pull_request=pull_request, dry_run=args.dry_run)
            if pr_followup_branch_prefix:
                push_branch(branch_name=active_branch, dry_run=args.dry_run)
                print(f"Pushed follow-up branch for PR #{pr_number_arg}: {active_branch}")
            else:
                push_current_branch(dry_run=args.dry_run)

            safe_post_orchestration_state_comment(
                repo=repo,
                target_type="pr",
                target_number=pr_number_arg,
                dry_run=args.dry_run,
                state=build_orchestration_state(
                    status="waiting-for-ci",
                    task_type="pr",
                    issue_number=None,
                    pr_number=pr_number_arg,
                    branch=active_branch,
                    base_branch=str(pr_state_context["base_branch"] or "") or None,
                    runner=args.runner,
                    agent=args.agent,
                    model=args.model,
                    attempt=1,
                    stage="changes_pushed",
                    next_action="wait_for_ci",
                    error=None,
                ),
            )

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
            if pr_number_arg is not None:
                failed_pr_number = pr_state_context.get("pr")
                if type(failed_pr_number) is int:
                    safe_post_orchestration_state_comment(
                        repo=repo,
                        target_type="pr",
                        target_number=failed_pr_number,
                        dry_run=args.dry_run,
                        state=build_orchestration_state(
                            status="failed",
                            task_type="pr",
                            issue_number=None,
                            pr_number=failed_pr_number,
                            branch=str(pr_state_context.get("branch") or "") or None,
                            base_branch=str(pr_state_context.get("base_branch") or "") or None,
                            runner=args.runner,
                            agent=args.agent,
                            model=args.model,
                            attempt=1,
                            stage=failure_stage,
                            next_action="inspect_error_and_retry",
                            error=short_error_text(str(exc)),
                        ),
                    )
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
            failure_stage = "issue_setup"
            linked_open_pr: dict | None = None
            recovered_state: dict | None = None
            mode = "issue-flow"
            mode_reason = "batch issue processing"
            force_override_applied = False
            skip_agent_run = False
            state_target_type = "issue"
            state_target_number = issue["number"]
            state_pr_number: int | None = None
            if issue_number_arg is not None and has_force_issue_flow_flag:
                linked_open_pr = find_open_pr_for_issue(repo=repo, issue_number=issue["number"])

                recovered_issue_state: dict | None = None
                try:
                    issue_comments = fetch_issue_comments(repo=repo, issue_number=issue["number"])
                    (
                        recovered_issue_state,
                        issue_state_warnings,
                    ) = select_latest_parseable_orchestration_state(
                        comments=issue_comments,
                        source_label=f"issue #{issue['number']}",
                    )
                    for warning in issue_state_warnings:
                        print(f"Warning: {warning}", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001
                    print(
                        "Warning: unable to recover orchestration state from "
                        f"issue #{issue['number']} comments: {exc}",
                        file=sys.stderr,
                    )

                recovered_pr_state: dict | None = None
                if linked_open_pr is not None:
                    linked_pr_number = linked_open_pr.get("number")
                    if type(linked_pr_number) is int:
                        try:
                            linked_pr_comments = fetch_issue_comments(
                                repo=repo,
                                issue_number=linked_pr_number,
                            )
                            (
                                recovered_pr_state,
                                pr_state_warnings,
                            ) = select_latest_parseable_orchestration_state(
                                comments=linked_pr_comments,
                                source_label=f"pr #{linked_pr_number}",
                            )
                            for warning in pr_state_warnings:
                                print(f"Warning: {warning}", file=sys.stderr)
                        except Exception as exc:  # noqa: BLE001
                            print(
                                "Warning: unable to recover orchestration state from "
                                f"PR #{linked_pr_number} comments: {exc}",
                                file=sys.stderr,
                            )

                recovered_state = merge_latest_recovered_state(
                    [recovered_issue_state, recovered_pr_state]
                )
                if recovered_state is not None:
                    prefix = "[dry-run] " if args.dry_run else ""
                    print(
                        f"{prefix}Recovered orchestration state context: "
                        f"{format_recovered_state_context(recovered_state)}"
                    )

                recovered_status = ""
                if recovered_state is not None:
                    recovered_status = str(recovered_state.get("status") or "")
                if recovered_status == "waiting-for-author" and not force_issue_flow:
                    print(
                        f"Skipping issue #{issue['number']} due to recovered orchestration state "
                        "waiting-for-author (use --force-issue-flow to override)."
                    )
                    continue
                if recovered_status == "waiting-for-author" and force_issue_flow:
                    force_override_applied = True
                    print(
                        "Recovered state is waiting-for-author, but continuing because "
                        "--force-issue-flow is set."
                    )

                mode, mode_reason = choose_execution_mode(
                    issue_number=issue["number"],
                    linked_open_pr=linked_open_pr,
                    force_issue_flow=force_issue_flow,
                    recovered_state=recovered_state,
                )

            body = (issue.get("body") or "").strip()
            if mode == "issue-flow" and not body and not args.include_empty:
                print(f"Skipping issue #{issue['number']} (empty body)")
                continue

            processed += 1

            prompt_override: str | None = None
            if (
                recovered_state is not None
                and str(recovered_state.get("status") or "") == "failed"
            ):
                print(
                    "Recovered failed orchestration state for rerun: "
                    f"{format_recovered_state_context(recovered_state)}"
                )
                prompt_override = append_recovered_context_to_prompt(
                    build_prompt(issue),
                    build_recovered_failure_context_note(recovered_state),
                )
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
                state_target_type = "pr"
                state_target_number = pr_number
                state_pr_number = pr_number

                print(
                    f"Auto-switch to PR-review mode for issue #{issue['number']}: {mode_reason}."
                )
                pull_request = fetch_pull_request(repo=repo, number=pr_number)
                merge_state = str(pull_request.get("mergeStateStatus") or "").strip().upper()
                should_force_sync_rerun = merge_state in {"DIRTY", "CONFLICTING"}
                if should_force_sync_rerun:
                    print(
                        f"Linked PR #{pr_number} is not mergeable with base yet "
                        f"(mergeStateStatus={merge_state}); rerun will auto-sync and resolve routine conflicts"
                    )
                thread_items = fetch_pr_review_threads(repo=repo, number=pr_number)
                conversation_comments = fetch_pr_conversation_comments(
                    repo=repo,
                    pr_number=pr_number,
                )
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
                    conversation_comments=conversation_comments,
                    pr_author_login=pr_author_login,
                )
                print(
                    "Review prompt sources: "
                    f"{format_review_filtering_stats(_review_stats)}"
                )
                issue_branch = str(linked_open_pr.get("headRefName") or "").strip()
                if not issue_branch:
                    raise RuntimeError(
                        f"Linked PR #{pr_number} has empty headRefName; cannot select working branch"
                    )
                target_base_branch = str(linked_open_pr.get("baseRefName") or base_branch).strip()
                if not target_base_branch:
                    target_base_branch = base_branch

                safe_post_orchestration_state_comment(
                    repo=repo,
                    target_type=state_target_type,
                    target_number=state_target_number,
                    dry_run=args.dry_run,
                    state=build_orchestration_state(
                        status="in-progress",
                        task_type="pr",
                        issue_number=issue["number"],
                        pr_number=state_pr_number,
                        branch=issue_branch,
                        base_branch=target_base_branch,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        attempt=1,
                        stage="agent_run",
                        next_action="wait_for_agent_result",
                        error=None,
                    ),
                )

                if not review_items:
                    if should_force_sync_rerun:
                        skip_agent_run = True
                        print(
                            f"No actionable review comments for linked PR #{pr_number}; "
                            f"continuing with sync-only rerun because mergeStateStatus={merge_state}"
                        )
                    else:
                        safe_post_orchestration_state_comment(
                            repo=repo,
                            target_type=state_target_type,
                            target_number=state_target_number,
                            dry_run=args.dry_run,
                            state=build_orchestration_state(
                                status="waiting-for-author",
                                task_type="pr",
                                issue_number=issue["number"],
                                pr_number=state_pr_number,
                                branch=issue_branch,
                                base_branch=target_base_branch,
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=1,
                                stage="review_feedback",
                                next_action="await_new_review_comments",
                                error="No actionable review comments found",
                            ),
                        )
                        print(
                            f"No actionable review comments for linked PR #{pr_number}; "
                            "skipping issue run."
                        )
                        continue

                if review_items:
                    linked_issues = load_linked_issue_context(repo=repo, pull_request=pull_request)
                    prompt_override = build_pr_review_prompt(
                        pull_request=pull_request,
                        review_items=review_items,
                        linked_issues=linked_issues,
                    )
                    if (
                        recovered_state is not None
                        and str(recovered_state.get("status") or "") == "failed"
                    ):
                        prompt_override = append_recovered_context_to_prompt(
                            prompt_override,
                            build_recovered_failure_context_note(recovered_state),
                        )

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
                    if force_override_applied:
                        print(
                            "Proceeding despite waiting-for-author state because --force-issue-flow is set."
                        )
                issue_branch = branch_name_for_issue(issue=issue, prefix=args.branch_prefix)
                target_base_branch = base_branch

            if mode == "issue-flow":
                safe_post_orchestration_state_comment(
                    repo=repo,
                    target_type=state_target_type,
                    target_number=state_target_number,
                    dry_run=args.dry_run,
                    state=build_orchestration_state(
                        status="in-progress",
                        task_type="issue",
                        issue_number=issue["number"],
                        pr_number=None,
                        branch=issue_branch,
                        base_branch=target_base_branch,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        attempt=1,
                        stage="agent_run",
                        next_action="wait_for_agent_result",
                        error=None,
                    ),
                )

            failure_stage = "prepare_branch"
            branch_status = prepare_issue_branch(
                base_branch=target_base_branch,
                branch_name=issue_branch,
                dry_run=args.dry_run,
                fail_on_existing=args.fail_on_existing,
            )
            print(f"Branch status for issue #{issue['number']}: {branch_status}")

            reused_branch_sync_changed = False

            if branch_status == "reused":
                if args.sync_reused_branch:
                    failure_stage = "sync_branch"
                    reused_branch_sync_changed = sync_reused_branch_with_base(
                        base_branch=target_base_branch,
                        branch_name=issue_branch,
                        strategy=args.sync_strategy,
                        dry_run=args.dry_run,
                    )
                else:
                    prefix = "[dry-run] " if args.dry_run else ""
                    print(
                        f"{prefix}Skipping reused-branch sync for '{issue_branch}' "
                        f"(selected base: '{target_base_branch}', strategy: '{args.sync_strategy}')"
                    )
            elif args.dry_run:
                print(
                    f"[dry-run] Reused-branch sync not needed for '{issue_branch}' "
                    f"(branch status: created; selected base: '{target_base_branch}'; "
                    f"strategy: '{args.sync_strategy}')"
                )

            if skip_agent_run:
                print(
                    f"Skipping agent run for issue #{issue['number']} in pr-review mode: "
                    "no actionable review comments; running sync-only path"
                )
            else:
                failure_stage = "agent_run"
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
                if branch_status == "reused" and args.sync_reused_branch and reused_branch_sync_changed:
                    print(
                        f"No file changes from agent for issue #{issue['number']}; "
                        "pushing sync-only branch updates"
                    )
                    used_force_with_lease = args.sync_strategy == "rebase"
                    push_branch(
                        branch_name=issue_branch,
                        dry_run=False,
                        force_with_lease=used_force_with_lease,
                    )
                    print(
                        f"Sync-only push result for issue #{issue['number']}: "
                        f"branch '{issue_branch}' pushed "
                        f"(force-with-lease: {'yes' if used_force_with_lease else 'no'})"
                    )
                    if mode == "pr-review" and linked_open_pr is not None:
                        linked_pr_number = linked_open_pr.get("number")
                        if type(linked_pr_number) is int:
                            print(
                                f"PR #{linked_pr_number} rerun sync pushed; "
                                "GitHub mergeability should be recalculated without manual conflict steps"
                            )
                    pr_status, pr_url = ensure_pr(
                        repo=repo,
                        base_branch=target_base_branch,
                        branch_name=issue_branch,
                        issue=issue,
                        dry_run=False,
                        fail_on_existing=args.fail_on_existing,
                    )
                    if pr_url:
                        touched_prs.append(pr_url)
                        print(f"PR status for issue #{issue['number']}: {pr_status} ({pr_url})")
                    if mode == "pr-review":
                        safe_post_orchestration_state_comment(
                            repo=repo,
                            target_type="pr",
                            target_number=state_target_number,
                            dry_run=False,
                            state=build_orchestration_state(
                                status="waiting-for-ci",
                                task_type="pr",
                                issue_number=issue["number"],
                                pr_number=state_pr_number,
                                branch=issue_branch,
                                base_branch=target_base_branch,
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=1,
                                stage="changes_pushed",
                                next_action="wait_for_ci",
                                error=None,
                            ),
                        )
                    elif mode == "issue-flow":
                        safe_post_orchestration_state_comment(
                            repo=repo,
                            target_type="issue",
                            target_number=issue["number"],
                            dry_run=False,
                            state=build_orchestration_state(
                                status="ready-for-review",
                                task_type="issue",
                                issue_number=issue["number"],
                                pr_number=parse_pr_number_from_url(pr_url),
                                branch=issue_branch,
                                base_branch=target_base_branch,
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=1,
                                stage="pr_ready",
                                next_action="wait_for_review",
                                error=None,
                            ),
                        )
                    run_command(["git", "checkout", base_branch])
                    continue

                print(
                    f"No changes detected for issue #{issue['number']}; skipping commit and PR"
                )
                safe_post_orchestration_state_comment(
                    repo=repo,
                    target_type=state_target_type,
                    target_number=state_target_number,
                    dry_run=False,
                    state=build_orchestration_state(
                        status="waiting-for-author",
                        task_type="issue" if mode == "issue-flow" else "pr",
                        issue_number=issue["number"],
                        pr_number=state_pr_number,
                        branch=issue_branch,
                        base_branch=target_base_branch,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        attempt=1,
                        stage="post_agent_check",
                        next_action="await_more_context",
                        error="No changes produced",
                    ),
                )
                run_command(["git", "checkout", base_branch])
                continue

            failure_stage = "commit_push"
            commit_changes(issue=issue, dry_run=args.dry_run)
            push_branch(
                branch_name=issue_branch,
                dry_run=args.dry_run,
                force_with_lease=(
                    branch_status == "reused"
                    and args.sync_reused_branch
                    and args.sync_strategy == "rebase"
                    and reused_branch_sync_changed
                ),
            )
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
                if mode == "issue-flow":
                    safe_post_orchestration_state_comment(
                        repo=repo,
                        target_type="issue",
                        target_number=issue["number"],
                        dry_run=args.dry_run,
                        state=build_orchestration_state(
                            status="ready-for-review",
                            task_type="issue",
                            issue_number=issue["number"],
                            pr_number=parse_pr_number_from_url(pr_url),
                            branch=issue_branch,
                            base_branch=target_base_branch,
                            runner=args.runner,
                            agent=args.agent,
                            model=args.model,
                            attempt=1,
                            stage="pr_ready",
                            next_action="wait_for_review",
                            error=None,
                        ),
                    )
                else:
                    safe_post_orchestration_state_comment(
                        repo=repo,
                        target_type="pr",
                        target_number=state_target_number,
                        dry_run=args.dry_run,
                        state=build_orchestration_state(
                            status="waiting-for-ci",
                            task_type="pr",
                            issue_number=issue["number"],
                            pr_number=state_pr_number,
                            branch=issue_branch,
                            base_branch=target_base_branch,
                            runner=args.runner,
                            agent=args.agent,
                            model=args.model,
                            attempt=1,
                            stage="changes_pushed",
                            next_action="wait_for_ci",
                            error=None,
                        ),
                    )

            if not args.dry_run:
                run_command(["git", "checkout", base_branch])
        except Exception as exc:  # noqa: BLE001
            failures += 1
            safe_post_orchestration_state_comment(
                repo=repo,
                target_type=state_target_type,
                target_number=state_target_number,
                dry_run=args.dry_run,
                state=build_orchestration_state(
                    status="failed",
                    task_type="issue" if mode == "issue-flow" else "pr",
                    issue_number=issue["number"],
                    pr_number=state_pr_number,
                    branch=locals().get("issue_branch", None),
                    base_branch=locals().get("target_base_branch", None),
                    runner=args.runner,
                    agent=args.agent,
                    model=args.model,
                    attempt=1,
                    stage=failure_stage,
                    next_action="inspect_error_and_retry",
                    error=short_error_text(str(exc)),
                ),
            )
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
