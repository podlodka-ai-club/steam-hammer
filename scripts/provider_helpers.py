"""Provider adapters for tracker and code host integrations.

This module keeps the runner entrypoint focused on orchestration flow by
isolating provider interfaces, concrete adapters, and Jira comment helpers.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
import urllib.parse
from typing import Callable


@dataclass(frozen=True)
class ProviderRuntime:
    tracker_github: str
    tracker_jira: str
    codehost_github: str
    parse_tracker: Callable[[object], str]
    parse_codehost: Callable[[object], str]
    normalize_issue_number: Callable[[object, str], int | str]
    jira_credentials_from_env: Callable[[], dict[str, str]]
    jira_request_json: Callable[..., object]
    fetch_jira_issue: Callable[[str], dict]
    fetch_jira_issues: Callable[..., list[dict]]
    jira_description_to_text: Callable[[object], str]
    get_fetch_issue: Callable[[], Callable[..., dict]]
    get_fetch_issues: Callable[[], Callable[..., list[dict]]]
    get_fetch_issue_comments: Callable[[], Callable[..., list[dict]]]
    get_fetch_jira_issue_comments: Callable[[], Callable[[str], list[dict]]]
    get_post_jira_issue_comment: Callable[[], Callable[[str, str], None]]
    get_run_command: Callable[[], Callable[[list[str]], object]]
    get_run_capture: Callable[[], Callable[[list[str]], str]]
    get_create_decomposition_child_issue: Callable[[], Callable[..., dict]]
    get_ensure_agent_failure_label: Callable[[], Callable[..., None]]
    get_format_issue_ref: Callable[[], Callable[..., str]]
    get_detect_repo: Callable[[], Callable[[], str]]
    get_detect_default_branch: Callable[[], Callable[[str], str]]
    get_find_open_pr_for_issue: Callable[[], Callable[..., dict | None]]
    get_fetch_pull_request: Callable[[], Callable[..., dict]]
    get_fetch_pr_review_threads: Callable[[], Callable[..., list[dict]]]
    get_fetch_pr_conversation_comments: Callable[[], Callable[..., list[dict]]]
    get_read_pr_ci_status_for_pull_request: Callable[[], Callable[..., dict]]
    get_load_linked_issue_context: Callable[[], Callable[..., list[dict]]]
    get_ensure_pr: Callable[[], Callable[..., tuple[str, str]]]


class TrackerProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    def supports_issue_labels(self) -> bool:
        return False

    @abc.abstractmethod
    def get_issue(self, repo: str, issue_id: int | str) -> dict:
        raise NotImplementedError

    @abc.abstractmethod
    def list_issues(self, repo: str, state: str, limit: int) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_issue_comments(self, repo: str, issue_id: int | str) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def post_issue_comment(self, repo: str, issue_id: int | str, body: str) -> None:
        raise NotImplementedError

    def create_child_issue(
        self,
        repo: str,
        parent_issue: dict,
        child: dict,
        created_dependencies: dict[int, dict],
        dry_run: bool,
        parent_branch: str | None = None,
        base_branch: str | None = None,
    ) -> dict:
        _ = (parent_branch, base_branch)
        raise RuntimeError(f"Tracker provider '{self.name}' does not support child issue creation yet")

    def add_issue_label(self, repo: str, issue_id: int | str, label_name: str, dry_run: bool) -> None:
        _ = (repo, issue_id, label_name, dry_run)

    def remove_issue_label(self, repo: str, issue_id: int | str, label_name: str, dry_run: bool) -> None:
        _ = (repo, issue_id, label_name, dry_run)

    def issue_has_label(self, repo: str, issue_id: int | str, label_name: str) -> bool:
        _ = (repo, issue_id, label_name)
        return False


class CodeHostProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def detect_repo(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def detect_default_branch(self, repo: str) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def find_open_pr_for_issue(self, repo: str, issue: dict) -> dict | None:
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_pull_request(self, repo: str, number: int) -> dict:
        raise NotImplementedError

    @abc.abstractmethod
    def list_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_pr_review_threads(self, repo: str, number: int) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_pr_conversation_comments(self, repo: str, pr_number: int) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def read_pr_ci_status_for_pull_request(self, repo: str, pull_request: dict) -> dict:
        raise NotImplementedError

    @abc.abstractmethod
    def load_pr_linked_issue_context(self, repo: str, pull_request: dict) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_pr(
        self,
        repo: str,
        base_branch: str,
        branch_name: str,
        issue: dict,
        dry_run: bool,
        fail_on_existing: bool,
        stacked_base_context: str | None = None,
    ) -> tuple[str, str]:
        raise NotImplementedError

    @abc.abstractmethod
    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        raise NotImplementedError


class GitHubTrackerProvider(TrackerProvider):
    def __init__(self, runtime: ProviderRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return self._runtime.tracker_github

    @property
    def supports_issue_labels(self) -> bool:
        return True

    def get_issue(self, repo: str, issue_id: int | str) -> dict:
        if type(issue_id) is not int:
            raise RuntimeError(f"GitHub tracker requires integer issue numbers, got {issue_id!r}")
        return self._runtime.get_fetch_issue()(repo=repo, number=issue_id)

    def list_issues(self, repo: str, state: str, limit: int) -> list[dict]:
        return self._runtime.get_fetch_issues()(repo=repo, state=state, limit=limit)

    def list_issue_comments(self, repo: str, issue_id: int | str) -> list[dict]:
        return self._runtime.get_fetch_issue_comments()(repo=repo, issue_number=issue_id)

    def post_issue_comment(self, repo: str, issue_id: int | str, body: str) -> None:
        self._runtime.get_run_command()(
            [
                "gh",
                "issue",
                "comment",
                str(issue_id),
                "--repo",
                repo,
                "--body",
                body,
            ]
        )

    def create_child_issue(
        self,
        repo: str,
        parent_issue: dict,
        child: dict,
        created_dependencies: dict[int, dict],
        dry_run: bool,
        parent_branch: str | None = None,
        base_branch: str | None = None,
    ) -> dict:
        return self._runtime.get_create_decomposition_child_issue()(
            repo=repo,
            parent_issue=parent_issue,
            child=child,
            created_dependencies=created_dependencies,
            dry_run=dry_run,
            parent_branch=parent_branch,
            base_branch=base_branch,
        )

    def add_issue_label(self, repo: str, issue_id: int | str, label_name: str, dry_run: bool) -> None:
        self._runtime.get_ensure_agent_failure_label()(repo=repo, dry_run=dry_run)
        if dry_run:
            issue_ref = self._runtime.get_format_issue_ref()(issue_id, tracker=self.name)
            print(f"[dry-run] Would add label '{label_name}' to issue {issue_ref}")
            return
        self._runtime.get_run_command()(
            [
                "gh",
                "issue",
                "edit",
                str(issue_id),
                "--repo",
                repo,
                "--add-label",
                label_name,
            ]
        )

    def remove_issue_label(self, repo: str, issue_id: int | str, label_name: str, dry_run: bool) -> None:
        if dry_run:
            issue_ref = self._runtime.get_format_issue_ref()(issue_id, tracker=self.name)
            print(f"[dry-run] Would remove label '{label_name}' from issue {issue_ref} if present")
            return
        if not self.issue_has_label(repo=repo, issue_id=issue_id, label_name=label_name):
            return
        self._runtime.get_run_command()(
            [
                "gh",
                "issue",
                "edit",
                str(issue_id),
                "--repo",
                repo,
                "--remove-label",
                label_name,
            ]
        )

    def issue_has_label(self, repo: str, issue_id: int | str, label_name: str) -> bool:
        labels_output = self._runtime.get_run_capture()(
            [
                "gh",
                "issue",
                "view",
                str(issue_id),
                "--repo",
                repo,
                "--json",
                "labels",
                "--jq",
                ".labels[].name",
            ]
        )
        labels = [line.strip() for line in labels_output.splitlines() if line.strip()]
        return label_name in labels


def _jira_comment_to_internal_shape(comment_payload: dict, issue_key: str, base_url: str, runtime: ProviderRuntime) -> dict:
    author = comment_payload.get("author") if isinstance(comment_payload.get("author"), dict) else {}
    comment_id = comment_payload.get("id")
    comment_url = ""
    if comment_id is not None:
        comment_url = f"{base_url}/browse/{issue_key}?focusedCommentId={comment_id}#comment-{comment_id}"
    return {
        "id": comment_id,
        "body": runtime.jira_description_to_text(comment_payload.get("body")),
        "created_at": str(comment_payload.get("created") or ""),
        "html_url": comment_url,
        "author": str(author.get("displayName") or author.get("accountId") or "unknown"),
    }


def jira_text_to_adf(body: str) -> dict:
    paragraphs: list[dict] = []
    for line in body.splitlines():
        if line.strip():
            paragraphs.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
            )
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    if not paragraphs:
        paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs}


def fetch_jira_issue_comments(issue_key: str, runtime: ProviderRuntime) -> list[dict]:
    credentials = runtime.jira_credentials_from_env()
    normalized_key = str(runtime.normalize_issue_number(issue_key, runtime.tracker_jira))
    payload = runtime.jira_request_json(
        method="GET",
        url=(
            f"{credentials['base_url']}/rest/api/3/issue/"
            f"{urllib.parse.quote(normalized_key)}/comment?maxResults=100"
        ),
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected response fetching Jira comments for {normalized_key}")
    comments = payload.get("comments")
    if not isinstance(comments, list):
        raise RuntimeError(f"Unexpected Jira comments payload for {normalized_key}")
    return [
        _jira_comment_to_internal_shape(item, issue_key=normalized_key, base_url=credentials["base_url"], runtime=runtime)
        for item in comments
        if isinstance(item, dict)
    ]


def post_jira_issue_comment(issue_key: str, body: str, runtime: ProviderRuntime) -> None:
    credentials = runtime.jira_credentials_from_env()
    normalized_key = str(runtime.normalize_issue_number(issue_key, runtime.tracker_jira))
    runtime.jira_request_json(
        method="POST",
        url=f"{credentials['base_url']}/rest/api/3/issue/{urllib.parse.quote(normalized_key)}/comment",
        payload={"body": jira_text_to_adf(body)},
    )


class JiraTrackerProvider(TrackerProvider):
    def __init__(self, runtime: ProviderRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return self._runtime.tracker_jira

    def get_issue(self, repo: str, issue_id: int | str) -> dict:
        _ = repo
        return self._runtime.fetch_jira_issue(str(issue_id))

    def list_issues(self, repo: str, state: str, limit: int) -> list[dict]:
        _ = repo
        jira_jql = {
            "open": "status != Done ORDER BY created DESC",
            "closed": "status = Done ORDER BY created DESC",
            "all": "ORDER BY created DESC",
        }[state]
        return self._runtime.fetch_jira_issues(jql=jira_jql, limit=limit)

    def list_issue_comments(self, repo: str, issue_id: int | str) -> list[dict]:
        _ = repo
        return self._runtime.get_fetch_jira_issue_comments()(issue_key=str(issue_id))

    def post_issue_comment(self, repo: str, issue_id: int | str, body: str) -> None:
        _ = repo
        self._runtime.get_post_jira_issue_comment()(issue_key=str(issue_id), body=body)


class GitHubCodeHostProvider(CodeHostProvider):
    def __init__(self, runtime: ProviderRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return self._runtime.codehost_github

    def detect_repo(self) -> str:
        return self._runtime.get_detect_repo()()

    def detect_default_branch(self, repo: str) -> str:
        return self._runtime.get_detect_default_branch()(repo)

    def find_open_pr_for_issue(self, repo: str, issue: dict) -> dict | None:
        return self._runtime.get_find_open_pr_for_issue()(repo=repo, issue=issue)

    def fetch_pull_request(self, repo: str, number: int) -> dict:
        return self._runtime.get_fetch_pull_request()(repo=repo, number=number)

    def list_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        return self._runtime.get_fetch_issue_comments()(repo=repo, issue_number=pr_number)

    def fetch_pr_review_threads(self, repo: str, number: int) -> list[dict]:
        return self._runtime.get_fetch_pr_review_threads()(repo=repo, number=number)

    def fetch_pr_conversation_comments(self, repo: str, pr_number: int) -> list[dict]:
        return self._runtime.get_fetch_pr_conversation_comments()(repo=repo, pr_number=pr_number)

    def read_pr_ci_status_for_pull_request(self, repo: str, pull_request: dict) -> dict:
        return self._runtime.get_read_pr_ci_status_for_pull_request()(repo=repo, pull_request=pull_request)

    def load_pr_linked_issue_context(self, repo: str, pull_request: dict) -> list[dict]:
        return self._runtime.get_load_linked_issue_context()(repo=repo, pull_request=pull_request)

    def ensure_pr(
        self,
        repo: str,
        base_branch: str,
        branch_name: str,
        issue: dict,
        dry_run: bool,
        fail_on_existing: bool,
        stacked_base_context: str | None = None,
    ) -> tuple[str, str]:
        return self._runtime.get_ensure_pr()(
            repo=repo,
            base_branch=base_branch,
            branch_name=branch_name,
            issue=issue,
            dry_run=dry_run,
            fail_on_existing=fail_on_existing,
            stacked_base_context=stacked_base_context,
        )

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        self._runtime.get_run_command()(
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


class UnsupportedCodeHostProvider(CodeHostProvider):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def _unsupported(self) -> RuntimeError:
        return RuntimeError(
            f"Code host provider '{self._name}' is not implemented yet. "
            "Core orchestration now routes through the provider interface, so add a provider adapter instead of rewriting the flow."
        )

    def detect_repo(self) -> str:
        raise self._unsupported()

    def detect_default_branch(self, repo: str) -> str:
        _ = repo
        raise self._unsupported()

    def find_open_pr_for_issue(self, repo: str, issue: dict) -> dict | None:
        _ = (repo, issue)
        raise self._unsupported()

    def fetch_pull_request(self, repo: str, number: int) -> dict:
        _ = (repo, number)
        raise self._unsupported()

    def list_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        _ = (repo, pr_number)
        raise self._unsupported()

    def fetch_pr_review_threads(self, repo: str, number: int) -> list[dict]:
        _ = (repo, number)
        raise self._unsupported()

    def fetch_pr_conversation_comments(self, repo: str, pr_number: int) -> list[dict]:
        _ = (repo, pr_number)
        raise self._unsupported()

    def read_pr_ci_status_for_pull_request(self, repo: str, pull_request: dict) -> dict:
        _ = (repo, pull_request)
        raise self._unsupported()

    def load_pr_linked_issue_context(self, repo: str, pull_request: dict) -> list[dict]:
        _ = (repo, pull_request)
        raise self._unsupported()

    def ensure_pr(
        self,
        repo: str,
        base_branch: str,
        branch_name: str,
        issue: dict,
        dry_run: bool,
        fail_on_existing: bool,
        stacked_base_context: str | None = None,
    ) -> tuple[str, str]:
        _ = (repo, base_branch, branch_name, issue, dry_run, fail_on_existing, stacked_base_context)
        raise self._unsupported()

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        _ = (repo, pr_number, body)
        raise self._unsupported()


def resolve_tracker_provider(tracker: str, runtime: ProviderRuntime) -> TrackerProvider:
    normalized = runtime.parse_tracker(tracker)
    if normalized == runtime.tracker_github:
        return GitHubTrackerProvider(runtime)
    if normalized == runtime.tracker_jira:
        return JiraTrackerProvider(runtime)
    raise RuntimeError(f"Unsupported tracker provider '{tracker}'")


def resolve_codehost_provider(codehost: str, runtime: ProviderRuntime) -> CodeHostProvider:
    normalized = runtime.parse_codehost(codehost)
    if normalized == runtime.codehost_github:
        return GitHubCodeHostProvider(runtime)
    return UnsupportedCodeHostProvider(normalized)
