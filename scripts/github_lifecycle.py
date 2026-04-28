from __future__ import annotations

import json
import re
from typing import Callable


def detect_repo(*, run_capture: Callable[[list[str]], str]) -> str:
    output = run_capture([
        "gh",
        "repo",
        "view",
        "--json",
        "nameWithOwner",
        "--jq",
        ".nameWithOwner",
    ])
    repo = output.strip()
    if not repo:
        raise RuntimeError("Unable to detect GitHub repository. Use --repo owner/name.")
    return repo


def detect_default_branch(repo: str, *, run_capture: Callable[[list[str]], str]) -> str:
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


def fetch_issues(
    repo: str,
    state: str,
    limit: int,
    *,
    run_capture: Callable[[list[str]], str],
    tracker_github: str,
) -> list[dict]:
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
            "number,title,body,url,state,labels,author,assignees,createdAt,updatedAt",
        ]
    )
    issues = json.loads(output)
    if not isinstance(issues, list):
        raise RuntimeError("Unexpected response from gh issue list")
    for issue in issues:
        if isinstance(issue, dict):
            issue.setdefault("tracker", tracker_github)
    return issues


def fetch_issue(
    repo: str,
    number: int,
    *,
    run_capture: Callable[[list[str]], str],
    tracker_github: str,
) -> dict:
    output = run_capture(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url,state,labels,author,assignees,createdAt,updatedAt",
        ]
    )
    issue = json.loads(output)
    if not isinstance(issue, dict):
        raise RuntimeError(f"Unexpected response fetching issue #{number}")
    issue.setdefault("tracker", tracker_github)
    return issue


def split_repo_name(repo: str) -> tuple[str, str]:
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        raise RuntimeError(f"Invalid repo format '{repo}'. Expected owner/name.")
    return owner, name


def fetch_pull_request(repo: str, number: int, *, run_capture: Callable[[list[str]], str]) -> dict:
    output = run_capture(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url,state,mergeStateStatus,mergeable,isDraft,reviewDecision,headRefName,headRefOid,baseRefName,author,closingIssuesReferences,reviews,files",
        ]
    )
    pull_request = json.loads(output)
    if not isinstance(pull_request, dict):
        raise RuntimeError(f"Unexpected response fetching PR #{number}")
    return pull_request


def fetch_pr_review_threads(repo: str, number: int, *, run_capture: Callable[[list[str]], str]) -> list[dict]:
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


def pr_links_issue(
    pr: dict,
    issue: dict,
    *,
    issue_tracker: Callable[[dict], str],
    tracker_github: str,
    format_issue_ref_from_issue: Callable[[dict], str],
) -> bool:
    references = pr.get("closingIssuesReferences")
    if isinstance(references, list):
        for reference in references:
            if not isinstance(reference, dict):
                continue
            if issue_tracker(issue) == tracker_github and reference.get("number") == issue.get("number"):
                return True

    issue_ref = format_issue_ref_from_issue(issue)
    issue_ref_lower = issue_ref.lower()
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    if issue_ref_lower in title.lower() or issue_ref_lower in body.lower():
        return True

    head_ref = str(pr.get("headRefName") or "")
    if issue_tracker(issue) == tracker_github:
        issue_number = issue.get("number")
        if re.search(rf"(^|[^0-9]){issue_number}([^0-9]|$)", head_ref):
            return True
    elif issue_ref_lower in head_ref.lower():
        return True

    return False


def find_open_pr_for_issue(
    repo: str,
    issue: dict,
    *,
    run_capture: Callable[[list[str]], str],
    issue_tracker: Callable[[dict], str],
    tracker_github: str,
    format_issue_ref_from_issue: Callable[[dict], str],
) -> dict | None:
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
        if isinstance(pr, dict) and pr_links_issue(
            pr,
            issue=issue,
            issue_tracker=issue_tracker,
            tracker_github=tracker_github,
            format_issue_ref_from_issue=format_issue_ref_from_issue,
        ):
            return pr
    return None


def fetch_pr_review_comments(repo: str, pr_number: int, *, run_capture: Callable[[list[str]], str]) -> list[dict]:
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


def fetch_issue_comments(repo: str, issue_number: int, *, run_capture: Callable[[list[str]], str]) -> list[dict]:
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


def fetch_pr_conversation_comments(
    repo: str,
    pr_number: int,
    *,
    fetch_issue_comments: Callable[[str, int], list[dict]],
) -> list[dict]:
    comments = fetch_issue_comments(repo, pr_number)

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


def branch_name_for_issue(
    issue: dict,
    prefix: str,
    *,
    issue_tracker: Callable[[dict], str],
    tracker_jira: str,
    slugify: Callable[[str], str],
) -> str:
    tracker = issue_tracker(issue)
    issue_ref = str(issue.get("number") or "").strip()
    if tracker == tracker_jira:
        issue_ref = issue_ref.lower()
    return f"{prefix}/{issue_ref}-{slugify(issue['title'])}"


def sanitize_branch_for_path(branch_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", branch_name).strip("-") or "pr-branch"


def open_pr(
    repo: str,
    base_branch: str,
    branch_name: str,
    issue: dict,
    dry_run: bool,
    *,
    run_capture: Callable[[list[str]], str],
    format_issue_ref_from_issue: Callable[[dict], str],
    issue_commit_title: Callable[[dict], str],
    issue_tracker: Callable[[dict], str],
    tracker_github: str,
    stacked_base_context: str | None = None,
) -> str:
    issue_ref = format_issue_ref_from_issue(issue)
    title = issue_commit_title(issue)
    body = (
        "## Summary\n"
        f"- Implements fix for {issue_ref}\n"
        f"- Source issue: {issue['url']}\n\n"
    )
    if issue_tracker(issue) == tracker_github:
        body += f"Closes {issue_ref}\n"
    if stacked_base_context:
        body += (
            "\n## Stack Context\n"
            f"- Stacked on current branch: `{stacked_base_context}`\n"
            f"- Base for this PR is `{stacked_base_context}` (not repository default branch)\n"
        )
    if dry_run:
        print(f"[dry-run] Would create PR '{title}' from '{branch_name}' to '{base_branch}'")
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


def find_existing_pr(repo: str, base_branch: str, branch_name: str, *, run_capture: Callable[[list[str]], str]) -> dict | None:
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
        raise RuntimeError(f"Multiple open PRs found for head '{branch_name}'. Resolve ambiguity manually.")

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
    *,
    find_existing_pr: Callable[[str, str, str], dict | None],
    open_pr: Callable[[str, str, str, dict, bool, str | None], str],
    stacked_base_context: str | None = None,
) -> tuple[str, str]:
    existing_pr = find_existing_pr(repo, base_branch, branch_name)
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
                print(f"[dry-run] Would reuse existing PR #{pr_number} from '{branch_name}' to '{base_branch}'")
        else:
            if existing_base and existing_base != base_branch:
                print(
                    f"Reusing existing PR #{pr_number}: {pr_url} "
                    f"(base '{existing_base}', selected base '{base_branch}')"
                )
            else:
                print(f"Reusing existing PR #{pr_number}: {pr_url}")

        return "reused", pr_url

    pr_url = open_pr(repo, base_branch, branch_name, issue, dry_run, stacked_base_context)
    return "created", pr_url
