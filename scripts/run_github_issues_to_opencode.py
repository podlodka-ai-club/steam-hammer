#!/usr/bin/env python3

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import re
import queue as _queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import signal
from typing import Callable

if __package__ in {None, ""}:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

from scripts.orchestration_state import (
    CLARIFICATION_REQUEST_MARKER,
    DECOMPOSITION_PLAN_MARKER,
    ORCHESTRATION_CLAIM_MARKER,
    ORCHESTRATION_STATE_MARKER,
    build_orchestration_claim,
    is_active_orchestration_claim,
    latest_clarification_request_from_agent_output,
    next_orchestration_attempt,
    normalize_orchestration_state_status,
    parse_clarification_request_text,
    parse_decomposition_plan_comment_body,
    parse_orchestration_claim_comment_body,
    parse_orchestration_state_comment_body,
    select_latest_parseable_decomposition_plan,
    select_latest_parseable_orchestration_claim,
    select_latest_parseable_orchestration_state,
)
from scripts import branch_recovery as _branch_recovery
from scripts import github_lifecycle as _github_lifecycle
from scripts import merge_result_verification as _merge_result_verification
from scripts.provider_helpers import (
    CodeHostProvider,
    GitHubCodeHostProvider as _GitHubCodeHostProvider,
    GitHubTrackerProvider as _GitHubTrackerProvider,
    JiraTrackerProvider as _JiraTrackerProvider,
    ProviderRuntime,
    TrackerProvider,
    UnsupportedCodeHostProvider as _UnsupportedCodeHostProvider,
    fetch_jira_issue_comments as _provider_fetch_jira_issue_comments,
    jira_text_to_adf as _provider_jira_text_to_adf,
    post_jira_issue_comment as _provider_post_jira_issue_comment,
)
from scripts.project_config import (
    CODEHOST_BITBUCKET,
    CODEHOST_CHOICES,
    CODEHOST_CUSTOM_PROXY,
    CODEHOST_GITHUB,
    PRESET_TIER_ORDER,
    ROUTING_RULE_TASK_TYPES,
    TRACKER_CHOICES,
    TRACKER_GITHUB,
    TRACKER_JIRA,
    WORKFLOW_HOOK_ALIASES,
    configured_recovery_focused_commands,
    configured_setup_command,
    configured_setup_commands,
    configured_workflow_commands,
    configured_workflow_hooks,
    load_project_config,
    parse_codehost as _parse_codehost,
    parse_tracker as _parse_tracker,
    project_cli_defaults,
    validate_project_config,
    workflow_hooks,
    workflow_merge_policy,
    workflow_readiness_policy,
)


LOCAL_CONFIG_RELATIVE_PATH = "local-config.json"
PROJECT_CONFIG_RELATIVE_PATH = "project-config.json"
BUILTIN_DEFAULTS = {
    "tracker": "github",
    "codehost": "github",
    "state": "open",
    "limit": 10,
    "runner": "claude",
    "agent": "build",
    "model": None,
    "agent_timeout_seconds": 900,
    "agent_idle_timeout_seconds": None,
    "token_budget": None,
    "opencode_auto_approve": False,
    "track_tokens": False,
    "branch_prefix": "issue-fix",
    "include_empty": False,
    "stop_on_error": False,
    "fail_on_existing": False,
    "force_issue_flow": False,
    "skip_if_pr_exists": True,
    "skip_if_branch_exists": True,
    "force_reprocess": False,
    "conflict_recovery_only": False,
    "sync_reused_branch": True,
    "sync_strategy": "rebase",
    "base_branch": "default",
    "decompose": "auto",
    "create_child_issues": False,
    "preset": None,
    "max_attempts": 1,
    "escalate_to_preset": None,
    "dir": ".",
}

POST_BATCH_VERIFICATION_DEFAULT_COMMANDS = _merge_result_verification.POST_BATCH_VERIFICATION_DEFAULT_COMMANDS
CENTRAL_RUNNER_PATH_PREFIXES = _merge_result_verification.CENTRAL_RUNNER_PATH_PREFIXES
DOC_ONLY_PATH_PREFIXES = _merge_result_verification.DOC_ONLY_PATH_PREFIXES
DOC_ONLY_FILE_EXTENSIONS = _merge_result_verification.DOC_ONLY_FILE_EXTENSIONS

JIRA_ENV_VARS = {
    "base_url": "JIRA_BASE_URL",
    "email": "JIRA_EMAIL",
    "api_token": "JIRA_API_TOKEN",
}
JIRA_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")

AGENT_FAILURE_REPORT_MARKER = "<!-- orchestration-agent-failure:v1 -->"
SCOPE_DECISION_MARKER = "<!-- orchestration-scope:v1 -->"
DECOMPOSITION_CHILD_ORDER_PREFIX = "Step"
AGENT_FAILURE_LABEL_NAME = "auto:agent-failed"
AGENT_FAILURE_LABEL_COLOR = "B60205"
AGENT_FAILURE_LABEL_DESCRIPTION = "Automation run failed for this issue"
RECOMMENDED_OPENCODE_MODEL = "openai/gpt-4o"
OLLAMA_MODEL_PREFIX = "ollama/"
OLLAMA_PREFLIGHT_TIMEOUT_SECONDS = 30
SIGKILL_EXIT_DESCRIPTION = (
    "This usually indicates a hard kill (SIGKILL), commonly from resource limits or environment-level"
    " termination rather than an argument/model syntax error."
)
ORCHESTRATION_STATE_STATUSES = {
    "in-progress",
    "ready-for-review",
    "failed",
    "blocked",
    "waiting-for-author",
    "waiting-for-ci",
    "ready-to-merge",
}
AUTONOMOUS_CLAIM_TTL_SECONDS = 3600
AUTONOMOUS_BATCH_SINGLE_PASS_STATUSES = frozenset(
    {"ready-for-review", "waiting-for-ci", "ready-to-merge"}
)
AUTONOMOUS_SESSION_SKIP_STATUSES = frozenset({"ready-for-review"})
AUTONOMOUS_QUEUE_STATUS_RANKS = {
    "ready-to-merge": 0,
    "waiting-for-ci": 1,
    "ready-for-review": 2,
    "in-progress": 3,
    "pr-review": 3,
    "issue-flow": 4,
    "waiting-for-author": 5,
    "blocked": 6,
    "failed": 7,
}
ORCHESTRATION_DEPENDENCIES_MARKER = "<!-- orchestration-dependencies:v1 -->"
DECOMPOSITION_CHILD_STATUSES = ("planned", "created", "in-progress", "done", "blocked")
KNOWN_NO_EXTENSION_REQUIRED_FILES = frozenset(
    {
        "readme",
        "changelog",
        "dockerfile",
        "docker-compose",
        "makefile",
        "gitattributes",
        "gitignore",
        "license",
        "authors",
    }
)
REQUIRED_FILE_SECTION_HEADERS = re.compile(
    r"(?im)^\s*#{0,6}\s*(?:[\-*]\s*)?(?:required files|required file|files required|acceptance criteria|acceptance|definition of done|done criteria)\b"
)
REQUIRED_FILE_SECTION_BREAK_HEADER = re.compile(r"(?im)^\s*#{1,6}\b")
REQUIRED_FILE_HINT_LINE = re.compile(
    r"(?i)(?:required|must\s+(?:change|update|modify|touch|add|create)|needs?\s+(?:to\s+)?change|evidence\s+of\s+change)"
)
FILE_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+(?:\.[A-Za-z0-9._-]+)?|[A-Za-z0-9._-]+\.[A-Za-z0-9][A-Za-z0-9._-]+"
)
AUTONOMOUS_DEPENDENCY_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:depends on|blocked by)\s*:?\s*(.+)$"
)
GITHUB_ISSUE_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9])#(\d+)\b")


def _as_positive_int(value: object) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized < 1:
        return None
    return normalized


def _as_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _normalize_child_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status in DECOMPOSITION_CHILD_STATUSES:
        return status
    return "planned"


def _safe_join_sorted(values: object) -> str:
    if not isinstance(values, list):
        return ""
    normalized: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item:
            normalized.append(item)
    return ", ".join(sorted(normalized))


def _normalize_match_list(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        item = str(value or "").strip().lower()
        if item:
            normalized.append(item)
    return normalized


def _first_json_object(raw: str) -> dict:
    start = raw.find("{")
    if start < 0:
        raise ValueError("payload is missing JSON object")
    payload, _offset = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(payload, dict):
        raise ValueError("payload JSON must be an object")
    return payload


def _parse_created_issue_reference(raw: str) -> dict[str, object]:
    issue_url_match = re.search(r"https?://\S+/issues/(\d+)\b", raw)
    if issue_url_match is None:
        raise RuntimeError("Unexpected response from gh issue create")
    issue_number = _as_positive_int(issue_url_match.group(1))
    if issue_number is None:
        raise RuntimeError("Created issue response missing integer number")
    return {
        "number": issue_number,
        "url": issue_url_match.group(0),
    }


def gh_issue_create(repo: str, title: str, body: str) -> dict[str, object]:
    create_command = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
    ]
    try:
        output = run_capture(create_command + ["--json", "number,url"])
    except RuntimeError as exc:
        if "unknown flag: --json" not in str(exc):
            raise
        return _parse_created_issue_reference(run_capture(create_command))

    created = json.loads(output)
    if not isinstance(created, dict):
        raise RuntimeError("Unexpected response from gh issue create")
    issue_number = created.get("number")
    if type(issue_number) is not int:
        raise RuntimeError("Created issue response missing integer number")
    return {
        "number": issue_number,
        "url": str(created.get("url") or ""),
    }


def _extract_issue_references_from_text(raw: str, tracker: str) -> list[int | str]:
    if tracker == TRACKER_JIRA:
        seen: set[str] = set()
        matches: list[str] = []
        for match in JIRA_ISSUE_KEY_RE.finditer(raw.upper()):
            issue_key = match.group(0)
            if issue_key not in seen:
                seen.add(issue_key)
                matches.append(issue_key)
        return matches

    seen_numbers: set[int] = set()
    numbers: list[int] = []
    for match in GITHUB_ISSUE_REFERENCE_RE.finditer(raw):
        issue_number = _as_positive_int(match.group(1))
        if issue_number is None or issue_number in seen_numbers:
            continue
        seen_numbers.add(issue_number)
        numbers.append(issue_number)
    return numbers


def _normalize_dependency_refs(raw_values: object, tracker: str) -> list[int | str]:
    if not isinstance(raw_values, list):
        return []
    normalized: list[int | str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        try:
            issue_ref = normalize_issue_number(raw_value, tracker)
        except RuntimeError:
            continue
        issue_key = str(issue_ref)
        if issue_key in seen:
            continue
        seen.add(issue_key)
        normalized.append(issue_ref)
    return normalized


def _dependency_refs_from_marker_payload(body: str, tracker: str) -> list[int | str]:
    if ORCHESTRATION_DEPENDENCIES_MARKER not in body:
        return []
    after_marker = body.split(ORCHESTRATION_DEPENDENCIES_MARKER, maxsplit=1)[1].strip()
    if not after_marker:
        return []

    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", after_marker, flags=re.DOTALL)
    candidates = fenced_matches if fenced_matches else [after_marker]
    for candidate in candidates:
        try:
            payload = _first_json_object(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        dependency_refs = _normalize_dependency_refs(payload.get("depends_on"), tracker)
        blocked_by_refs = _normalize_dependency_refs(payload.get("blocked_by"), tracker)
        return dependency_refs + [ref for ref in blocked_by_refs if str(ref) not in {str(dep) for dep in dependency_refs}]
    return []


def parse_issue_dependency_references(issue: dict, comments: list[dict] | None = None) -> list[int | str]:
    tracker = issue_tracker(issue)
    self_ref = str(issue.get("number") or "").strip()
    refs: list[int | str] = []
    seen: set[str] = set()

    text_sources = [str(issue.get("body") or "")]
    if isinstance(comments, list):
        text_sources.extend(str(comment.get("body") or "") for comment in comments if isinstance(comment, dict))

    for text in text_sources:
        for issue_ref in _dependency_refs_from_marker_payload(text, tracker):
            issue_key = str(issue_ref)
            if issue_key and issue_key != self_ref and issue_key not in seen:
                seen.add(issue_key)
                refs.append(issue_ref)
        for match in AUTONOMOUS_DEPENDENCY_LINE_RE.finditer(text):
            dependency_line = str(match.group(1) or "")
            for issue_ref in _extract_issue_references_from_text(dependency_line, tracker):
                issue_key = str(issue_ref)
                if issue_key and issue_key != self_ref and issue_key not in seen:
                    seen.add(issue_key)
                    refs.append(issue_ref)

    return refs


def is_trackable_issue_number(value: object) -> bool:
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return bool(value.strip())
    return False


def normalize_issue_number(value: object, tracker: str) -> int | str:
    if tracker == TRACKER_GITHUB:
        if isinstance(value, int):
            if value <= 0:
                raise RuntimeError(f"Invalid GitHub issue number: {value}")
            return value
        if not isinstance(value, str):
            raise RuntimeError(f"Invalid GitHub issue value: {value!r}")
        text = value.strip()
        if not text.isdigit():
            raise RuntimeError(f"Invalid GitHub issue number: {text}")
        number = int(text)
        if number <= 0:
            raise RuntimeError(f"Invalid GitHub issue number: {text}")
        return number

    if tracker == TRACKER_JIRA:
        if not isinstance(value, str):
            raise RuntimeError(f"Invalid Jira issue key: {value!r}")
        text = value.strip()
        if not JIRA_ISSUE_KEY_RE.match(text):
            raise RuntimeError(f"Invalid Jira issue key: {text}")
        return text

    raise RuntimeError(f"Unsupported tracker: {tracker}")


def issue_tracker(issue: dict) -> str:
    return _parse_tracker(issue.get("tracker") or TRACKER_GITHUB)


def format_issue_ref(issue_number: object, tracker: str = TRACKER_GITHUB) -> str:
    normalized_tracker = _parse_tracker(tracker)
    if normalized_tracker == TRACKER_JIRA:
        return str(issue_number)
    return f"#{issue_number}"


def format_issue_label(issue_number: object, tracker: str = TRACKER_GITHUB) -> str:
    return f"issue {format_issue_ref(issue_number, tracker=tracker)}"


def _format_stored_issue_ref(issue_number: object) -> str | None:
    if isinstance(issue_number, int):
        return format_issue_ref(issue_number, tracker=TRACKER_GITHUB)
    if isinstance(issue_number, str):
        normalized = issue_number.strip()
        if normalized:
            return normalized
    return None


def format_issue_ref_from_issue(issue: dict) -> str:
    return format_issue_ref(issue.get("number"), tracker=issue_tracker(issue))


def format_issue_label_from_issue(issue: dict) -> str:
    return format_issue_label(issue.get("number"), tracker=issue_tracker(issue))


def _provider_runtime() -> ProviderRuntime:
    return ProviderRuntime(
        tracker_github=TRACKER_GITHUB,
        tracker_jira=TRACKER_JIRA,
        codehost_github=CODEHOST_GITHUB,
        parse_tracker=_parse_tracker,
        parse_codehost=_parse_codehost,
        normalize_issue_number=normalize_issue_number,
        jira_credentials_from_env=_jira_credentials_from_env,
        jira_request_json=_jira_request_json,
        fetch_jira_issue=fetch_jira_issue,
        fetch_jira_issues=fetch_jira_issues,
        jira_description_to_text=jira_description_to_text,
        get_fetch_issue=lambda: fetch_issue,
        get_fetch_issues=lambda: fetch_issues,
        get_fetch_issue_comments=lambda: fetch_issue_comments,
        get_fetch_jira_issue_comments=lambda: fetch_jira_issue_comments,
        get_post_jira_issue_comment=lambda: post_jira_issue_comment,
        get_run_command=lambda: run_command,
        get_run_capture=lambda: run_capture,
        get_create_decomposition_child_issue=lambda: create_decomposition_child_issue,
        get_ensure_agent_failure_label=lambda: ensure_agent_failure_label,
        get_format_issue_ref=lambda: format_issue_ref,
        get_detect_repo=lambda: detect_repo,
        get_detect_default_branch=lambda: detect_default_branch,
        get_find_open_pr_for_issue=lambda: find_open_pr_for_issue,
        get_fetch_pull_request=lambda: fetch_pull_request,
        get_fetch_pr_review_threads=lambda: fetch_pr_review_threads,
        get_fetch_pr_conversation_comments=lambda: fetch_pr_conversation_comments,
        get_read_pr_ci_status_for_pull_request=lambda: read_pr_ci_status_for_pull_request,
        get_load_linked_issue_context=lambda: load_linked_issue_context,
        get_ensure_pr=lambda: ensure_pr,
    )


class GitHubTrackerProvider(_GitHubTrackerProvider):
    def __init__(self) -> None:
        super().__init__(_provider_runtime())


class JiraTrackerProvider(_JiraTrackerProvider):
    def __init__(self) -> None:
        super().__init__(_provider_runtime())


class GitHubCodeHostProvider(_GitHubCodeHostProvider):
    def __init__(self) -> None:
        super().__init__(_provider_runtime())


class UnsupportedCodeHostProvider(_UnsupportedCodeHostProvider):
    pass


def jira_text_to_adf(body: str) -> dict:
    return _provider_jira_text_to_adf(body)


def fetch_jira_issue_comments(issue_key: str) -> list[dict]:
    return _provider_fetch_jira_issue_comments(issue_key=issue_key, runtime=_provider_runtime())


def post_jira_issue_comment(issue_key: str, body: str) -> None:
    _provider_post_jira_issue_comment(issue_key=issue_key, body=body, runtime=_provider_runtime())


ACTIVE_TRACKER_PROVIDER: TrackerProvider | None = None
ACTIVE_CODEHOST_PROVIDER: CodeHostProvider | None = None


def resolve_tracker_provider(tracker: str) -> TrackerProvider:
    normalized = _parse_tracker(tracker)
    if normalized == TRACKER_GITHUB:
        return GitHubTrackerProvider()
    if normalized == TRACKER_JIRA:
        return JiraTrackerProvider()
    raise RuntimeError(f"Unsupported tracker provider '{tracker}'")


def resolve_codehost_provider(codehost: str) -> CodeHostProvider:
    normalized = _parse_codehost(codehost)
    if normalized == CODEHOST_GITHUB:
        return GitHubCodeHostProvider()
    return UnsupportedCodeHostProvider(normalized)


def configure_active_providers(tracker_provider: TrackerProvider, codehost_provider: CodeHostProvider) -> None:
    global ACTIVE_TRACKER_PROVIDER, ACTIVE_CODEHOST_PROVIDER
    ACTIVE_TRACKER_PROVIDER = tracker_provider
    ACTIVE_CODEHOST_PROVIDER = codehost_provider


def current_tracker_provider() -> TrackerProvider:
    return ACTIVE_TRACKER_PROVIDER or GitHubTrackerProvider()


def current_codehost_provider() -> CodeHostProvider:
    return ACTIVE_CODEHOST_PROVIDER or GitHubCodeHostProvider()


def issue_commit_title(issue: dict) -> str:
    issue_ref = format_issue_ref_from_issue(issue)
    if issue_tracker(issue) == TRACKER_JIRA:
        return f"Fix {issue_ref}: {issue['title']}"
    return f"Fix issue {issue_ref}: {issue['title']}"


def _jira_credentials_from_env() -> dict[str, str]:
    credentials: dict[str, str] = {}
    missing: list[str] = []
    for key, env_var in JIRA_ENV_VARS.items():
        value = str(os.environ.get(env_var) or "").strip()
        if not value:
            missing.append(env_var)
            continue
        credentials[key] = value

    if missing:
        raise RuntimeError(
            "Missing required Jira environment variables: " + ", ".join(missing)
        )

    credentials["base_url"] = credentials["base_url"].rstrip("/")
    return credentials


def validate_provider_requirements(tracker: str, codehost: str, pr_mode_requested: bool) -> None:
    normalized_tracker = _parse_tracker(tracker)
    normalized_codehost = _parse_codehost(codehost)
    if pr_mode_requested and normalized_codehost != CODEHOST_GITHUB:
        raise RuntimeError("--pr / --from-review-comments mode only supports --codehost github")
    if normalized_tracker == TRACKER_JIRA:
        _jira_credentials_from_env()


def _jira_text_fragments(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_jira_text_fragments(item))
        return fragments
    if not isinstance(value, dict):
        return []

    node_type = str(value.get("type") or "")
    text_value = str(value.get("text") or "")
    content = value.get("content")
    fragments: list[str] = []

    if text_value:
        fragments.append(text_value)
    if isinstance(content, list):
        child_parts = _jira_text_fragments(content)
        if child_parts:
            separator = "\n" if node_type in {"paragraph", "heading", "bulletList", "orderedList", "listItem"} else ""
            child_text = separator.join(part for part in child_parts if part)
            if child_text:
                fragments.append(child_text)

    if node_type in {"paragraph", "heading", "listItem"} and fragments:
        return ["".join(fragments).strip(), "\n"]
    if node_type in {"bulletList", "orderedList"} and fragments:
        return ["\n".join(part.strip() for part in fragments if str(part).strip()), "\n"]
    return fragments


def jira_description_to_text(description: object) -> str:
    if isinstance(description, str):
        return description.strip()
    if description is None:
        return ""
    fragments = _jira_text_fragments(description)
    text = "".join(fragments)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _jira_issue_to_internal_shape(issue_payload: dict, base_url: str) -> dict:
    key = str(issue_payload.get("key") or "").strip()
    fields = issue_payload.get("fields")
    if not key or not isinstance(fields, dict):
        raise RuntimeError("Unexpected response from Jira issue API")

    return {
        "number": key,
        "title": str(fields.get("summary") or "").strip(),
        "body": jira_description_to_text(fields.get("description")),
        "url": f"{base_url}/browse/{key}",
        "tracker": TRACKER_JIRA,
    }


def _jira_request_json(method: str, url: str, payload: dict | None = None) -> object:
    credentials = _jira_credentials_from_env()
    auth_token = base64.b64encode(
        f"{credentials['email']}:{credentials['api_token']}".encode("utf-8")
    ).decode("ascii")
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {auth_token}",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        detail = f"HTTP {exc.code}"
        if body:
            detail = f"{detail}: {body}"
        raise RuntimeError(f"Jira API request failed for {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Jira API request failed for {url}: {exc.reason}") from exc


def fetch_jira_issue(issue_key: str) -> dict:
    credentials = _jira_credentials_from_env()
    normalized_key = str(normalize_issue_number(issue_key, TRACKER_JIRA))
    payload = _jira_request_json(
        method="GET",
        url=f"{credentials['base_url']}/rest/api/3/issue/{urllib.parse.quote(normalized_key)}",
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected response fetching Jira issue {normalized_key}")
    return _jira_issue_to_internal_shape(payload, credentials["base_url"])


def fetch_jira_issues(jql: str, limit: int) -> list[dict]:
    credentials = _jira_credentials_from_env()
    payload = _jira_request_json(
        method="POST",
        url=f"{credentials['base_url']}/rest/api/3/issue/search",
        payload={
            "jql": jql,
            "maxResults": limit,
            "fields": ["summary", "description", "assignee"],
        },
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected response from Jira issue search")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise RuntimeError("Unexpected response from Jira issue search")
    return [_jira_issue_to_internal_shape(item, credentials["base_url"]) for item in issues if isinstance(item, dict)]


def _issue_author_login(issue: dict) -> str:
    author_payload = issue.get("author") if isinstance(issue, dict) else None
    if isinstance(author_payload, dict):
        return str(author_payload.get("login") or "").strip().lower()
    return ""


def _issue_assignee_logins(issue: dict) -> list[str]:
    assignees_payload = issue.get("assignees") if isinstance(issue, dict) else None
    if not isinstance(assignees_payload, list):
        return []

    assignees: list[str] = []
    for assignee in assignees_payload:
        if not isinstance(assignee, dict):
            continue
        login = str(assignee.get("login") or "").strip().lower()
        if login:
            assignees.append(login)
    return assignees


def _issue_label_names(issue: dict) -> list[str]:
    labels_payload = issue.get("labels") if isinstance(issue, dict) else None
    if not isinstance(labels_payload, list):
        return []

    labels: list[str] = []
    for label in labels_payload:
        if not isinstance(label, dict):
            continue
        name = str(label.get("name") or "").strip().lower()
        if name:
            labels.append(name)
    return labels


def _parse_iso_timestamp(value: object) -> datetime | None:
    text = _as_optional_string(value)
    if text is None:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _issue_priority_rank(issue: dict, ordered_labels: list[str]) -> int:
    if not ordered_labels:
        return len(ordered_labels)
    issue_labels = set(_issue_label_names(issue))
    for index, label in enumerate(ordered_labels):
        if label in issue_labels:
            return index
    return len(ordered_labels)


def project_scope_defaults(project_config: dict) -> dict:
    scope = project_config.get("scope") if isinstance(project_config, dict) else None
    if not isinstance(scope, dict):
        return {}

    defaults = scope.get("defaults")
    if not isinstance(defaults, dict):
        return {}
    return defaults


def evaluate_issue_scope(issue: dict, scope_defaults: dict) -> dict:
    labels_config = scope_defaults.get("labels") if isinstance(scope_defaults, dict) else None
    authors_config = scope_defaults.get("authors") if isinstance(scope_defaults, dict) else None
    assignees_config = scope_defaults.get("assignees") if isinstance(scope_defaults, dict) else None
    priority_config = scope_defaults.get("priority") if isinstance(scope_defaults, dict) else None
    freshness_config = scope_defaults.get("freshness") if isinstance(scope_defaults, dict) else None

    allow_labels = _normalize_match_list(
        labels_config.get("allow") if isinstance(labels_config, dict) else None
    )
    deny_labels = _normalize_match_list(
        labels_config.get("deny") if isinstance(labels_config, dict) else None
    )
    allow_authors = _normalize_match_list(
        authors_config.get("allow") if isinstance(authors_config, dict) else None
    )
    deny_authors = _normalize_match_list(
        authors_config.get("deny") if isinstance(authors_config, dict) else None
    )
    allow_assignees = _normalize_match_list(
        assignees_config.get("allow") if isinstance(assignees_config, dict) else None
    )
    deny_assignees = _normalize_match_list(
        assignees_config.get("deny") if isinstance(assignees_config, dict) else None
    )
    allow_priority = _normalize_match_list(
        priority_config.get("allow") if isinstance(priority_config, dict) else None
    )
    deny_priority = _normalize_match_list(
        priority_config.get("deny") if isinstance(priority_config, dict) else None
    )
    priority_order = _normalize_match_list(
        priority_config.get("order") if isinstance(priority_config, dict) else None
    )
    max_age_days = (
        freshness_config.get("max_age_days") if isinstance(freshness_config, dict) else None
    )
    max_idle_days = (
        freshness_config.get("max_idle_days") if isinstance(freshness_config, dict) else None
    )

    issue_labels = set(_issue_label_names(issue))
    issue_author = _issue_author_login(issue)
    issue_assignees = set(_issue_assignee_logins(issue))

    matched_deny_labels = sorted(label for label in deny_labels if label in issue_labels)
    if matched_deny_labels:
        return {
            "eligible": False,
            "reason": f"matched deny label(s): {', '.join(matched_deny_labels)}",
            "matched": {"labels_deny": matched_deny_labels},
        }

    if allow_labels:
        matched_allow_labels = sorted(label for label in allow_labels if label in issue_labels)
        if not matched_allow_labels:
            return {
                "eligible": False,
                "reason": (
                    "missing required allow label "
                    f"(expected one of: {', '.join(sorted(allow_labels))})"
                ),
                "matched": {"labels_allow": []},
            }

    if issue_author and issue_author in deny_authors:
        return {
            "eligible": False,
            "reason": f"author '{issue_author}' is denied by scope authors.deny",
            "matched": {"author_deny": [issue_author]},
        }

    if allow_authors:
        if issue_author not in allow_authors:
            allow_text = ", ".join(sorted(allow_authors))
            author_text = issue_author or "unknown"
            return {
                "eligible": False,
                "reason": (
                    f"author '{author_text}' is not in scope authors.allow ({allow_text})"
                ),
                "matched": {"author_allow": []},
            }

    matched_deny_assignees = sorted(assignee for assignee in deny_assignees if assignee in issue_assignees)
    if matched_deny_assignees:
        return {
            "eligible": False,
            "reason": f"matched denied assignee(s): {', '.join(matched_deny_assignees)}",
            "matched": {"assignees_deny": matched_deny_assignees},
        }

    if allow_assignees:
        matched_allow_assignees = sorted(
            assignee for assignee in allow_assignees if assignee in issue_assignees
        )
        if not matched_allow_assignees:
            return {
                "eligible": False,
                "reason": (
                    "missing required assignee "
                    f"(expected one of: {', '.join(sorted(allow_assignees))})"
                ),
                "matched": {"assignees_allow": []},
            }

    matched_deny_priority = sorted(label for label in deny_priority if label in issue_labels)
    if matched_deny_priority:
        return {
            "eligible": False,
            "reason": f"matched deny priority label(s): {', '.join(matched_deny_priority)}",
            "matched": {"priority_deny": matched_deny_priority},
        }

    if allow_priority:
        matched_allow_priority = sorted(label for label in allow_priority if label in issue_labels)
        if not matched_allow_priority:
            return {
                "eligible": False,
                "reason": (
                    "missing required priority label "
                    f"(expected one of: {', '.join(sorted(allow_priority))})"
                ),
                "matched": {"priority_allow": []},
            }

    now = datetime.now(timezone.utc)
    created_at = _parse_iso_timestamp(issue.get("createdAt"))
    updated_at = _parse_iso_timestamp(issue.get("updatedAt"))
    if isinstance(max_age_days, int) and max_age_days > 0 and created_at is not None:
        age_days = (now - created_at).total_seconds() / 86400
        if age_days > max_age_days:
            return {
                "eligible": False,
                "reason": f"issue is too old for autonomous scope ({age_days:.1f}d > {max_age_days}d)",
                "matched": {"freshness_max_age_days": max_age_days},
            }

    if isinstance(max_idle_days, int) and max_idle_days > 0 and updated_at is not None:
        idle_days = (now - updated_at).total_seconds() / 86400
        if idle_days > max_idle_days:
            return {
                "eligible": False,
                "reason": f"issue is too stale for autonomous scope ({idle_days:.1f}d > {max_idle_days}d idle)",
                "matched": {"freshness_max_idle_days": max_idle_days},
            }

    return {
        "eligible": True,
        "reason": "scope rules passed",
        "matched": {
            "priority_rank": _issue_priority_rank(issue, priority_order),
        },
    }


def _autonomous_queue_sort_metadata(repo: str, issue: dict) -> dict[str, object]:
    metadata: dict[str, object] = {
        "status": "issue-flow",
        "status_rank": AUTONOMOUS_QUEUE_STATUS_RANKS["issue-flow"],
        "merge_risk_rank": 3,
    }
    codehost_provider = current_codehost_provider()

    try:
        linked_open_pr = codehost_provider.find_open_pr_for_issue(repo=repo, issue=issue)
    except Exception:
        return metadata

    if not isinstance(linked_open_pr, dict):
        return metadata

    metadata["status"] = "pr-review"
    metadata["status_rank"] = AUTONOMOUS_QUEUE_STATUS_RANKS["pr-review"]
    metadata["merge_risk_rank"] = 2

    pr_number = _as_positive_int(linked_open_pr.get("number"))
    if pr_number is None:
        return metadata

    try:
        pull_request = codehost_provider.fetch_pull_request(repo=repo, number=pr_number)
    except Exception:
        return metadata

    merge_readiness_state = classify_pr_merge_readiness_state(
        merge_state=str(pull_request.get("mergeStateStatus") or ""),
        mergeable=str(pull_request.get("mergeable") or ""),
    )
    review_decision = derive_pr_review_decision(pull_request)
    is_draft = bool(pull_request.get("isDraft"))
    if not is_draft and merge_readiness_state == "clean" and review_decision == "APPROVED":
        metadata["status"] = "ready-to-merge"
        metadata["status_rank"] = AUTONOMOUS_QUEUE_STATUS_RANKS["ready-to-merge"]
    elif merge_readiness_state in {"stale", "conflicting"}:
        metadata["status"] = merge_readiness_state
        metadata["status_rank"] = AUTONOMOUS_QUEUE_STATUS_RANKS["pr-review"]

    changed_paths = extract_pull_request_changed_file_paths(pull_request)
    if _touches_central_runner_files(changed_paths):
        metadata["merge_risk_rank"] = 0
    elif changed_paths:
        metadata["merge_risk_rank"] = 1
    return metadata


def sort_autonomous_issues(
    issues: list[dict],
    scope_defaults: dict,
    repo: str | None = None,
) -> list[dict]:
    priority_config = scope_defaults.get("priority") if isinstance(scope_defaults, dict) else None
    priority_order = _normalize_match_list(
        priority_config.get("order") if isinstance(priority_config, dict) else None
    )
    queue_metadata: dict[int, dict[str, object]] = {}
    if repo:
        for issue in issues:
            queue_metadata[id(issue)] = _autonomous_queue_sort_metadata(repo=repo, issue=issue)

    def sort_key(issue: dict) -> tuple[int, int, int, float, int]:
        metadata = queue_metadata.get(id(issue), {})
        updated_at = _parse_iso_timestamp(issue.get("updatedAt"))
        updated_ts = updated_at.timestamp() if updated_at is not None else 0.0
        issue_number = issue.get("number")
        numeric_issue = issue_number if type(issue_number) is int else 0
        status_rank = metadata.get("status_rank")
        merge_risk_rank = metadata.get("merge_risk_rank")
        return (
            int(status_rank) if isinstance(status_rank, int) else AUTONOMOUS_QUEUE_STATUS_RANKS["issue-flow"],
            int(merge_risk_rank) if isinstance(merge_risk_rank, int) else 3,
            _issue_priority_rank(issue, priority_order),
            -updated_ts,
            -numeric_issue,
        )

    return sorted(issues, key=sort_key)


def _fetch_issue_comments_for_dependency_resolution(repo: str, issue: dict) -> list[dict]:
    issue_ref = issue.get("number")
    if issue_ref is None:
        return []
    return current_tracker_provider().list_issue_comments(repo=repo, issue_id=issue_ref)


def _fetch_dependency_issue(repo: str, issue_ref: int | str, tracker: str) -> dict | None:
    try:
        normalized_ref = normalize_issue_number(issue_ref, tracker)
    except RuntimeError:
        return None
    return current_tracker_provider().get_issue(repo=repo, issue_id=normalized_ref)


def split_autonomous_issues_by_dependency_state(
    repo: str,
    issues: list[dict],
) -> tuple[list[dict], list[dict]]:
    open_lookup: dict[tuple[str, str], dict] = {}
    dependency_issue_cache: dict[tuple[str, str], dict | None] = {}
    comments_cache: dict[tuple[str, str], list[dict]] = {}
    runnable: list[dict] = []
    blocked: list[dict] = []

    for issue in issues:
        tracker = issue_tracker(issue)
        issue_number = issue.get("number")
        if issue_number is None:
            continue
        issue_key = (tracker, str(issue_number))
        open_lookup[issue_key] = issue
        dependency_issue_cache[issue_key] = issue

    for issue in issues:
        tracker = issue_tracker(issue)
        issue_number = issue.get("number")
        if issue_number is None:
            runnable.append(issue)
            continue

        issue_key = (tracker, str(issue_number))
        issue_comments = comments_cache.get(issue_key)
        if issue_comments is None:
            issue_comments = _fetch_issue_comments_for_dependency_resolution(repo=repo, issue=issue)
            comments_cache[issue_key] = issue_comments

        dependency_refs = parse_issue_dependency_references(issue, comments=issue_comments)
        blocking_refs: list[int | str] = []
        unresolved_refs: list[int | str] = []

        for dependency_ref in dependency_refs:
            dependency_key = (tracker, str(dependency_ref))
            dependency_issue = dependency_issue_cache.get(dependency_key)
            if dependency_issue is None and dependency_key not in dependency_issue_cache:
                dependency_issue = _fetch_dependency_issue(
                    repo=repo,
                    issue_ref=dependency_ref,
                    tracker=tracker,
                )
                if isinstance(dependency_issue, dict):
                    dependency_issue.setdefault("tracker", tracker)
                dependency_issue_cache[dependency_key] = dependency_issue

            dependency_issue = dependency_issue_cache.get(dependency_key)
            if dependency_issue is None:
                unresolved_refs.append(dependency_ref)
                continue

            dependency_state = str(dependency_issue.get("state") or "").strip().lower()
            if dependency_key in open_lookup or dependency_state == "open":
                blocking_refs.append(dependency_ref)

        if blocking_refs or unresolved_refs:
            blocked_entry = {
                "issue": issue,
                "depends_on": dependency_refs,
                "open_dependencies": blocking_refs,
                "unresolved_dependencies": unresolved_refs,
            }
            open_dependency_labels = ", ".join(
                format_issue_ref(dependency_ref, tracker=tracker) for dependency_ref in blocking_refs
            )
            unresolved_dependency_labels = ", ".join(
                format_issue_ref(dependency_ref, tracker=tracker) for dependency_ref in unresolved_refs
            )
            reasons: list[str] = []
            if open_dependency_labels:
                reasons.append(f"open dependencies {open_dependency_labels}")
            if unresolved_dependency_labels:
                reasons.append(f"unresolved dependencies {unresolved_dependency_labels}")
            blocked_entry["reason"] = "; ".join(reasons) if reasons else "dependency state is unresolved"
            blocked.append(blocked_entry)
            continue

        runnable.append(issue)

    return runnable, blocked


def format_autonomous_dependency_blocker(blocked_entry: dict) -> str:
    issue = blocked_entry.get("issue") if isinstance(blocked_entry, dict) else None
    if not isinstance(issue, dict):
        return "blocked issue"
    issue_label = format_issue_label_from_issue(issue)
    reason = _as_optional_string(blocked_entry.get("reason")) or "dependency state is unresolved"
    return f"{issue_label} skipped: {reason}"


def load_autonomous_session_state(path: str | None) -> dict:
    if not path:
        return {"processed_issues": {}, "checkpoint": {}}
    load_error: OSError | json.JSONDecodeError | None = None
    payload: object | None = None
    for attempt in range(3):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            load_error = None
            break
        except FileNotFoundError:
            return {"processed_issues": {}, "checkpoint": {}}
        except (OSError, json.JSONDecodeError) as exc:
            load_error = exc
            if attempt < 2:
                time.sleep(0.05)
    if load_error is not None:
        print(f"Warning: unable to load autonomous session state from {path}: {load_error}", file=sys.stderr)
        return {"processed_issues": {}, "checkpoint": {}}
    if not isinstance(payload, dict):
        return {"processed_issues": {}, "checkpoint": {}}
    processed_issues = payload.get("processed_issues")
    if not isinstance(processed_issues, dict):
        processed_issues = {}
    checkpoint = payload.get("checkpoint")
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    return {"processed_issues": processed_issues, "checkpoint": checkpoint}


def save_autonomous_session_state(path: str | None, state: dict) -> None:
    if not path:
        return
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="autonomous-session-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        print(f"Warning: unable to save autonomous session state to {path}: {exc}", file=sys.stderr)
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def autonomous_session_processed_issue_numbers(state: dict) -> set[int]:
    processed = state.get("processed_issues") if isinstance(state, dict) else None
    if not isinstance(processed, dict):
        return set()
    issue_numbers: set[int] = set()
    for raw_issue_number in processed.keys():
        issue_number = _as_positive_int(raw_issue_number)
        if issue_number is not None:
            issue_numbers.add(issue_number)
    return issue_numbers


def autonomous_session_issue_status(state: dict, issue_number: int) -> str | None:
    processed = state.get("processed_issues") if isinstance(state, dict) else None
    if not isinstance(processed, dict):
        return None
    issue_entry = processed.get(str(issue_number))
    if not isinstance(issue_entry, dict):
        return None
    status = _as_optional_string(issue_entry.get("status"))
    return status if status else None


def mark_autonomous_session_issue_processed(state: dict, issue_number: int, status: str) -> dict:
    if status not in AUTONOMOUS_BATCH_SINGLE_PASS_STATUSES:
        return state
    processed = state.get("processed_issues") if isinstance(state, dict) else None
    if not isinstance(processed, dict):
        processed = {}
        state["processed_issues"] = processed
    processed[str(issue_number)] = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return state


def filter_autonomous_issues_for_single_pass(
    issues: list[dict], session_state: dict
) -> tuple[list[dict], list[int]]:
    if not autonomous_session_processed_issue_numbers(session_state):
        return list(issues), []
    filtered: list[dict] = []
    skipped: list[int] = []
    for issue in issues:
        issue_number = _as_positive_int(issue.get("number"))
        if issue_number is None:
            filtered.append(issue)
            continue
        processed_status = autonomous_session_issue_status(session_state, issue_number)
        if processed_status in AUTONOMOUS_SESSION_SKIP_STATUSES:
            skipped.append(issue_number)
            continue
        filtered.append(issue)
    return filtered, skipped


def _compact_autonomous_status_counts(counts: dict | None) -> str:
    if not isinstance(counts, dict):
        return "processed=0, failures=0"
    parts = [
        f"processed={int(counts.get('processed') or 0)}",
        f"failures={int(counts.get('failures') or 0)}",
    ]
    optional_fields = (
        ("skipped_existing_pr", "skipped_existing_pr"),
        ("skipped_existing_branch", "skipped_existing_branch"),
        ("skipped_blocked_dependencies", "skipped_blocked_dependencies"),
        ("skipped_out_of_scope", "skipped_out_of_scope"),
    )
    for key, label in optional_fields:
        value = int(counts.get(key) or 0)
        if value > 0:
            parts.append(f"{label}={value}")
    return ", ".join(parts)


def update_autonomous_session_checkpoint(
    state: dict,
    *,
    run_id: str,
    phase: str,
    batch_index: int,
    total_batches: int,
    counts: dict[str, int],
    done: list[str] | None,
    current: str | None,
    next_items: list[str] | None,
    issue_pr_actions: list[str] | None,
    in_progress: list[str] | None,
    blockers: list[str] | None,
    next_checkpoint: str | None,
    verification: dict[str, object] | None = None,
) -> dict:
    existing_checkpoint = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
    checkpoint = {
        "run_id": run_id,
        "phase": str(phase or "running").strip() or "running",
        "batch_index": max(batch_index, 0),
        "total_batches": max(total_batches, 0),
        "counts": {
            "processed": int(counts.get("processed") or 0),
            "failures": int(counts.get("failures") or 0),
            "skipped_existing_pr": int(counts.get("skipped_existing_pr") or 0),
            "skipped_existing_branch": int(counts.get("skipped_existing_branch") or 0),
            "skipped_blocked_dependencies": int(counts.get("skipped_blocked_dependencies") or 0),
            "skipped_out_of_scope": int(counts.get("skipped_out_of_scope") or 0),
        },
        "done": [item for item in done or [] if str(item).strip()],
        "current": _as_optional_string(current),
        "next": [item for item in next_items or [] if str(item).strip()],
        "issue_pr_actions": [item for item in issue_pr_actions or [] if str(item).strip()],
        "in_progress": [item for item in in_progress or [] if str(item).strip()],
        "blockers": [item for item in blockers or [] if str(item).strip()],
        "next_checkpoint": _as_optional_string(next_checkpoint),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    preserved_verification = (
        existing_checkpoint.get("verification")
        if isinstance(existing_checkpoint.get("verification"), dict)
        else None
    )
    if verification is not None:
        checkpoint["verification"] = verification
    elif preserved_verification is not None:
        checkpoint["verification"] = preserved_verification
    state["checkpoint"] = checkpoint
    return state


def format_autonomous_session_status_summary(state: dict) -> str:
    checkpoint = state.get("checkpoint") if isinstance(state, dict) else None
    if not isinstance(checkpoint, dict) or not checkpoint:
        processed_issue_count = len(autonomous_session_processed_issue_numbers(state))
        return (
            "Autonomous session status\n"
            f"Done: processed issues recorded={processed_issue_count}\n"
            "Current: no active checkpoint has been recorded yet\n"
            "Next: start or resume the autonomous batch loop\n"
            "Issue/PR actions: none\n"
            "In progress: none\n"
            "Blockers: none\n"
            "Next checkpoint: when the first batch starts"
        )

    done_items = checkpoint.get("done") if isinstance(checkpoint.get("done"), list) else []
    next_items = checkpoint.get("next") if isinstance(checkpoint.get("next"), list) else []
    actions = checkpoint.get("issue_pr_actions") if isinstance(checkpoint.get("issue_pr_actions"), list) else []
    in_progress = checkpoint.get("in_progress") if isinstance(checkpoint.get("in_progress"), list) else []
    blockers = checkpoint.get("blockers") if isinstance(checkpoint.get("blockers"), list) else []
    verification = checkpoint.get("verification") if isinstance(checkpoint.get("verification"), dict) else None
    batch_index = int(checkpoint.get("batch_index") or 0)
    total_batches = int(checkpoint.get("total_batches") or 0)
    phase = str(checkpoint.get("phase") or "running").strip() or "running"
    updated_at = _as_optional_string(checkpoint.get("updated_at")) or "unknown"
    counts_summary = _compact_autonomous_status_counts(
        checkpoint.get("counts") if isinstance(checkpoint.get("counts"), dict) else None
    )

    lines = [
        f"Autonomous session status: {phase}",
        f"Batch: {batch_index}/{total_batches}" if total_batches > 0 else "Batch: not started",
        f"Done: {'; '.join(done_items) if done_items else 'none yet'}",
        f"Current: {_as_optional_string(checkpoint.get('current')) or 'idle'}",
        f"Next: {'; '.join(next_items) if next_items else 'no queued batches'}",
        f"Issue/PR actions: {'; '.join(actions) if actions else 'none'}",
        f"In progress: {'; '.join(in_progress) if in_progress else 'none'}",
        f"Blockers: {'; '.join(blockers) if blockers else 'none'}",
        f"Next checkpoint: {_as_optional_string(checkpoint.get('next_checkpoint')) or 'after the next autonomous batch'}",
        f"Counts: {counts_summary}",
        f"Updated: {updated_at}",
    ]
    if isinstance(verification, dict):
        verification_status = str(verification.get("status") or "unknown")
        verification_summary = _as_optional_string(verification.get("summary")) or verification_status
        follow_up_issue = (
            verification.get("follow_up_issue")
            if isinstance(verification.get("follow_up_issue"), dict)
            else None
        )
        verification_line = f"Verification: {verification_summary}"
        if follow_up_issue is not None:
            follow_up_status = _as_optional_string(follow_up_issue.get("status"))
            issue_ref = _format_stored_issue_ref(follow_up_issue.get("issue_number"))
            if follow_up_status == "created" and issue_ref is not None:
                verification_line += f"; follow-up issue {issue_ref} created"
            elif follow_up_status:
                verification_line += f"; follow-up={follow_up_status}"
        lines.append(verification_line)
    return "\n".join(lines)


def preview_autonomous_issue_queue(issues: list[dict], start_index: int, limit: int = 3) -> list[str]:
    if limit <= 0:
        return []
    preview: list[str] = []
    for issue in issues[start_index : start_index + limit]:
        preview.append(format_issue_label_from_issue(issue))
    remaining = max(len(issues) - (start_index + limit), 0)
    if remaining > 0:
        preview.append(f"{remaining} more queued")
    return preview


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


def run_check_command(
    command: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str, int]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env)
    except FileNotFoundError as exc:
        return False, "", str(exc), 127
    return (
        result.returncode == 0,
        (result.stdout or "").strip(),
        (result.stderr or "").strip(),
        result.returncode,
    )


def describe_exit_code(return_code: int) -> str:
    if return_code >= 0:
        return f"exit code {return_code}"

    signal_number = -return_code
    try:
        signal_name = signal.Signals(signal_number).name
    except ValueError:
        return f"terminated by signal {signal_number}"

    return f"terminated by {signal_name} ({signal_number})"


def classify_opencode_failure(return_code: int, model: str | None) -> str | None:
    if return_code != -signal.SIGKILL:
        return None

    details = [
        SIGKILL_EXIT_DESCRIPTION,
        f"For stability, run with --runner opencode --agent build --model {RECOMMENDED_OPENCODE_MODEL} first.",
    ]

    if model and model != RECOMMENDED_OPENCODE_MODEL:
        details.append(f"This run used model '{model}', which is different from the current recommended baseline.")

    return " ".join(details)


def _ollama_model_name(model: str | None) -> str | None:
    normalized_model = str(model or "").strip()
    if not normalized_model.startswith(OLLAMA_MODEL_PREFIX):
        return None

    local_model = normalized_model[len(OLLAMA_MODEL_PREFIX):].strip()
    if not local_model:
        raise RuntimeError(
            "OpenCode model 'ollama/' is missing the local Ollama model name. "
            "Use a value like 'ollama/qwen3.5:2b'."
        )
    return local_model


def validate_opencode_model_backend(runner: str, model: str | None) -> None:
    if runner != "opencode":
        return

    local_model = _ollama_model_name(model)
    if local_model is None:
        return

    ollama_path = shutil.which("ollama")
    if not ollama_path:
        raise RuntimeError(
            f"OpenCode model '{model}' requires the local `ollama` CLI, but it was not found in PATH. "
            "Install/start Ollama or use a known-working non-Ollama model/backend."
        )

    try:
        result = subprocess.run(
            [ollama_path, "show", local_model],
            capture_output=True,
            text=True,
            timeout=OLLAMA_PREFLIGHT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Timed out after {OLLAMA_PREFLIGHT_TIMEOUT_SECONDS}s while validating local Ollama model "
            f"'{local_model}' for OpenCode. Confirm the Ollama backend is healthy, then retry, "
            "or use a known-working model/backend."
        ) from exc

    if result.returncode == 0:
        return

    detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "ollama show failed"
    raise RuntimeError(
        f"Unable to validate local Ollama model '{local_model}' for OpenCode: {detail}. "
        f"Confirm `ollama show {local_model}` succeeds, pull the model if needed, then retry, "
        "or use a known-working model/backend."
    )


def _label_already_exists_error(message: str) -> bool:
    return "already exists" in str(message).lower()


MERGE_METHOD_FLAGS = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}
MERGEABLE_READY_STATES = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}

CI_PENDING_CHECK_RUN_STATUSES = {"queued", "in_progress", "requested", "waiting", "pending"}
CI_SUCCESS_CHECK_RUN_CONCLUSIONS = {"success", "neutral", "skipped"}
CI_FAILURE_CHECK_RUN_CONCLUSIONS = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
}
CI_FAILURE_COMMIT_STATES = {"error", "failure"}
CI_WAIT_POLL_INTERVAL_SECONDS = 10
CI_WAIT_MAX_POLLS = 30
CI_LOG_MAX_CHECKS = 3
CI_LOG_EXCERPT_MAX_CHARS = 4000
CI_TRANSIENT_LOG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)rate limit|too many requests|secondary rate limit"), "rate limited"),
    (re.compile(r"(?i)connection reset|connection refused|connection timed out|network is unreachable"), "network failure"),
    (re.compile(r"(?i)timed out while|context deadline exceeded|timeout awaiting|TLS handshake timeout"), "network timeout"),
    (re.compile(r"(?i)502 bad gateway|503 service unavailable|504 gateway timeout"), "upstream service outage"),
    (re.compile(r"(?i)runner lost communication|runner has received a shutdown signal|hosted runner|resource temporarily unavailable"), "runner infrastructure issue"),
)


def failure_state_for_stage(failure_stage: str) -> str:
    return "blocked" if failure_stage in {"workflow_setup", "workflow_hooks", "workflow_checks", "residual_untracked_validation", "token_budget", "branch_context_validation"} else "failed"


def failure_next_action_for_stage(failure_stage: str) -> str:
    if failure_stage == "workflow_setup":
        return "fix_workflow_setup_and_retry"
    if failure_stage == "workflow_hooks":
        return "fix_workflow_hook_and_retry"
    if failure_stage == "workflow_checks":
        return "fix_workflow_checks_and_retry"
    if failure_stage == "merge_execution":
        return "inspect_merge_requirements_and_retry"
    if failure_stage == "residual_untracked_validation":
        return "stage_or-remove-residual-untracked-files"
    if failure_stage == "token_budget":
        return "raise_token_budget_or_split_issue"
    if failure_stage == "cost_budget":
        return "raise_cost_budget_or_split_issue"
    if failure_stage == "branch_context_validation":
        return "restore_worker_branch_context_and_retry"
    return "inspect_error_and_retry"


class WorkflowCheckFailure(RuntimeError):
    def __init__(self, failed_check: dict, checks: list[dict]):
        self.failed_check = failed_check
        self.checks = checks

        check_name = str(failed_check.get("name") or "check")
        exit_code = failed_check.get("exit_code")
        error = str(failed_check.get("error") or "")
        evidence = ""
        if error:
            evidence = f": {error}"
        elif failed_check.get("stderr_excerpt"):
            evidence = f": {failed_check['stderr_excerpt']}"
        elif failed_check.get("stdout_excerpt"):
            evidence = f": {failed_check['stdout_excerpt']}"

        super().__init__(
            f"Workflow check '{check_name}' failed"
            f" (exit code {exit_code if exit_code is not None else 'unknown'}){evidence}"
        )


class WorkflowHookFailure(RuntimeError):
    def __init__(self, failed_hook: dict, hooks: list[dict]):
        self.failed_hook = failed_hook
        self.hooks = hooks

        hook_name = str(failed_hook.get("hook") or "hook")
        command = str(failed_hook.get("command") or "")
        exit_code = failed_hook.get("exit_code")
        error = str(failed_hook.get("error") or "")
        evidence = f": {error}" if error else ""

        super().__init__(
            f"Workflow hook '{hook_name}' failed"
            f" (exit code {exit_code if exit_code is not None else 'unknown'})"
            f" while running: {command}{evidence}"
        )


class ResidualUntrackedFilesError(RuntimeError):
    def __init__(self, files: list[str], stage: str):
        self.files = files
        self.stage = stage
        super().__init__(
            "Residual untracked files detected during"
            f" {stage}: {', '.join(sorted(files))}"
        )


class TokenBudgetExceededError(RuntimeError):
    def __init__(self, budget: int, reached: int, item_label: str):
        self.budget = budget
        self.reached = reached
        self.item_label = item_label
        super().__init__(
            f"Agent stopped: token budget of {_format_budget_message_count(budget) or budget} exceeded "
            f"(reached ~{_format_budget_message_count(reached) or reached}). Use --token-budget to raise the limit or split the issue."
        )


class CostBudgetExceededError(RuntimeError):
    def __init__(self, budget: float, reached: float, item_label: str):
        self.budget = budget
        self.reached = reached
        self.item_label = item_label
        super().__init__(
            f"Agent stopped: cost budget of ${budget:.4f} exceeded "
            f"(reached ~${reached:.4f}). Lower the task scope, switch to a cheaper preset/model, or raise the configured budget."
        )


class MergeRequestNotAcceptedError(RuntimeError):
    def __init__(self, *, status: str, next_action: str, message: str):
        self.status = status
        self.next_action = next_action
        self.message = message
        super().__init__(message)


class RecoveryVerificationFailure(RuntimeError):
    def __init__(self, *, scope: str, verification: dict[str, object]):
        self.scope = scope
        self.verification = verification
        detail = _as_optional_string(verification.get("error")) or _as_optional_string(verification.get("summary"))
        super().__init__(detail or f"{scope.capitalize()} recovery verification failed")


class BranchContextMismatchError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        expected_branch: str,
        actual_branch: str,
        expected_repo_root: str,
        actual_repo_root: str,
    ):
        self.operation = operation
        self.expected_branch = expected_branch
        self.actual_branch = actual_branch
        self.expected_repo_root = expected_repo_root
        self.actual_repo_root = actual_repo_root
        super().__init__(
            f"Refusing to {operation}: expected branch '{expected_branch}' in repo '{expected_repo_root}', "
            f"but current context is branch '{actual_branch}' in repo '{actual_repo_root}'"
        )


def _run_workflow_shell_command(
    *,
    kind: str,
    name: str,
    command_text: str,
    dry_run: bool,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    if dry_run:
        print(f"[dry-run] Would run workflow {kind} '{name}': {command_text}")
        return {
            "name": name,
            "command": command_text,
            "status": "dry-run",
            "exit_code": None,
        }

    print(f"Running workflow {kind} '{name}': {command_text}")
    try:
        result = subprocess.run(
            ["bash", "-lc", command_text],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Workflow {kind} '{name}' failed to start: {exc}"
        ) from exc

    stdout_text = (result.stdout or "").strip()
    stderr_text = (result.stderr or "").strip()
    command_result: dict[str, object] = {
        "name": name,
        "command": command_text,
        "status": "passed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
    }
    if stdout_text:
        command_result["stdout_excerpt"] = _workflow_output_excerpt(stdout_text)
    if stderr_text:
        command_result["stderr_excerpt"] = _workflow_output_excerpt(stderr_text)

    if result.returncode == 0:
        print(f"Workflow {kind} '{name}' passed")
        return command_result

    evidence = stderr_text or stdout_text or "command failed"
    raise RuntimeError(
        f"Workflow {kind} '{name}' failed with exit code {result.returncode}: "
        f"{_workflow_output_excerpt(evidence)}"
    )


def run_workflow_hook(
    *,
    hooks: dict[str, str],
    hook_name: str,
    dry_run: bool,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object] | None:
    command_text = hooks.get(hook_name)
    if not command_text:
        return None
    return _run_workflow_shell_command(
        kind="hook",
        name=hook_name,
        command_text=command_text,
        dry_run=dry_run,
        cwd=cwd,
        env=env,
    )


def run_configured_workflow_hooks(
    *,
    hook_name: str,
    configured_hooks: dict[str, list[str]],
    dry_run: bool,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    context: dict[str, str] | None = None,
) -> list[dict]:
    hook_name = WORKFLOW_HOOK_ALIASES.get(hook_name, hook_name)
    commands = configured_hooks.get(hook_name) or []
    if not commands:
        return []

    merged_env = os.environ.copy()
    if env:
        merged_env.update({key: str(value) for key, value in env.items() if value is not None})
    if context:
        merged_env.update({key: str(value) for key, value in context.items() if value is not None})

    results: list[dict] = []
    for index, command_text in enumerate(commands, start=1):
        result = _run_workflow_shell_command(
            kind="hook",
            name=f"{hook_name}[{index}]",
            command_text=command_text,
            dry_run=dry_run,
            cwd=cwd,
            env=merged_env,
        )
        result["hook"] = hook_name
        results.append(result)
    return results


def _check_name_key(value: object) -> str:
    return str(value or "").strip().lower()


def _pull_request_changed_paths(pull_request: dict | None) -> list[str]:
    return _merge_result_verification.pull_request_changed_paths(pull_request)


def _is_docs_only_path(path: str) -> bool:
    return _merge_result_verification.is_docs_only_path(path)


def _touches_central_runner_files(changed_paths: list[str]) -> bool:
    return _merge_result_verification.touches_central_runner_files(changed_paths)


def list_open_pull_requests(repo: str, limit: int = 100) -> list[dict]:
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
            str(limit),
            "--json",
            "number,title,url,headRefName,baseRefName",
        ]
    )
    prs = json.loads(output)
    if not isinstance(prs, list):
        raise RuntimeError("Unexpected response from gh pr list while loading open PRs")
    return [pr for pr in prs if isinstance(pr, dict)]


def determine_merge_result_verification_need(repo: str, pull_request: dict) -> dict[str, object]:
    return _merge_result_verification.determine_merge_result_verification_need(
        repo=repo,
        pull_request=pull_request,
        list_open_pull_requests=list_open_pull_requests,
        fetch_pull_request=fetch_pull_request,
    )


def _summarize_merge_result_verification_results(results: list[dict[str, object]]) -> str:
    return _merge_result_verification.summarize_merge_result_verification_results(results)


def merge_result_verification_commands(
    *,
    project_config: dict,
    cwd: str | None,
) -> list[tuple[str, str]]:
    return _merge_result_verification.merge_result_verification_commands(
        project_config=project_config,
        cwd=cwd,
        detect_post_batch_verification_commands=detect_post_batch_verification_commands,
    )


def verify_pull_request_merge_result(
    *,
    repo: str,
    pull_request: dict,
    project_config: dict,
    repo_dir: str,
    dry_run: bool,
) -> dict[str, object]:
    return _merge_result_verification.verify_pull_request_merge_result(
        repo=repo,
        pull_request=pull_request,
        project_config=project_config,
        repo_dir=repo_dir,
        dry_run=dry_run,
        determine_need=determine_merge_result_verification_need,
        resolve_commands=merge_result_verification_commands,
        run_command=run_command,
        run_check_command=run_check_command,
        workflow_output_excerpt=_workflow_output_excerpt,
        short_error_text=short_error_text,
    )


def count_approving_reviews(pull_request: dict) -> dict[str, object]:
    reviews = pull_request.get("reviews")
    if not isinstance(reviews, list):
        reviews = []

    pr_author_payload = pull_request.get("author") if isinstance(pull_request, dict) else None
    pr_author_login = ""
    if isinstance(pr_author_payload, dict):
        pr_author_login = str(pr_author_payload.get("login") or "").strip().lower()

    latest_reviews_by_author: dict[str, dict[str, str]] = {}
    for review in sorted((item for item in reviews if isinstance(item, dict)), key=_submitted_at_key):
        author_payload = review.get("author") if isinstance(review.get("author"), dict) else None
        author_login = str(author_payload.get("login") or "").strip().lower() if author_payload else ""
        if not author_login or author_login == pr_author_login:
            continue
        latest_reviews_by_author[author_login] = {
            "state": str(review.get("state") or "").strip().upper(),
            "submittedAt": str(review.get("submittedAt") or ""),
        }

    approved_by = sorted(
        author for author, review in latest_reviews_by_author.items() if review.get("state") == "APPROVED"
    )
    return {
        "approved_count": len(approved_by),
        "approved_by": approved_by,
        "latest_review_states": {author: review.get("state") or "" for author, review in latest_reviews_by_author.items()},
    }


def evaluate_pr_readiness(*args, **kwargs) -> dict[str, object]:
    if args:
        if len(args) < 3:
            raise TypeError("evaluate_pr_readiness expected at least 3 positional arguments")
        project_config = args[0]
        pull_request = args[1]
        ci_status = args[2]
        required_file_validation = kwargs.get("required_file_validation") or {"status": "passed"}
    else:
        pull_request = kwargs["pull_request"]
        ci_status = kwargs["ci_status"]
        required_file_validation = kwargs.get("required_file_validation") or {"status": "passed"}
        project_config = kwargs["project_config"]

    readiness_policy = workflow_readiness_policy(project_config)
    required_checks = readiness_policy.get("required_checks")
    if not isinstance(required_checks, list):
        required_checks = []

    ci_checks = ci_status.get("checks") if isinstance(ci_status.get("checks"), list) else []
    ci_overall = str(ci_status.get("overall") or "").strip().lower()
    ci_checks_by_name = {
        _check_name_key(check.get("name")): check
        for check in ci_checks
        if isinstance(check, dict) and _check_name_key(check.get("name"))
    }

    if bool(readiness_policy.get("require_green_checks")):
        failing_ci_checks = [
            check for check in ci_checks if isinstance(check, dict) and str(check.get("state") or "") == "failure"
        ]
        pending_ci_checks = [
            check for check in ci_checks if isinstance(check, dict) and str(check.get("state") or "") == "pending"
        ]
        if ci_overall == "failure" or failing_ci_checks:
            summary = format_failing_ci_checks_summary(failing_ci_checks)
            return {
                "status": "blocked",
                "next_action": "inspect_failing_ci_checks",
                "error": short_error_text(summary or "CI checks are failing"),
            }
        if ci_overall == "pending" or pending_ci_checks:
            return {
                "status": "waiting-for-ci",
                "next_action": "wait_for_ci",
                "error": "Waiting for CI checks to finish",
            }
        if not ci_checks:
            return {
                "status": "waiting-for-ci",
                "next_action": "wait_for_ci",
                "error": "Waiting for CI checks to start",
            }

    matched_required_checks: list[dict] = []
    missing_required_checks: list[str] = []
    for required_name in required_checks:
        matched = ci_checks_by_name.get(_check_name_key(required_name))
        if matched is None:
            missing_required_checks.append(required_name)
            continue
        matched_required_checks.append(matched)

    failing_required_checks = [
        check for check in matched_required_checks if str(check.get("state") or "") == "failure"
    ]
    pending_required_checks = [
        check for check in matched_required_checks if str(check.get("state") or "") == "pending"
    ]
    if failing_required_checks:
        summary = format_failing_ci_checks_summary(failing_required_checks)
        return {
            "status": "blocked",
            "next_action": "inspect_failing_ci_checks",
            "error": short_error_text(summary),
        }

    if missing_required_checks or pending_required_checks:
        waiting_parts: list[str] = []
        if missing_required_checks:
            waiting_parts.append("missing required checks: " + ", ".join(missing_required_checks))
        if pending_required_checks:
            waiting_parts.append(
                "pending required checks: "
                + ", ".join(str(check.get("name") or "unknown-check") for check in pending_required_checks)
            )
        return {
            "status": "waiting-for-ci",
            "next_action": "wait_for_ci",
            "error": short_error_text("; ".join(waiting_parts)) if waiting_parts else None,
        }

    if (
        readiness_policy.get("require_required_file_evidence") is not False
        and required_file_validation.get("status") == "blocked"
    ):
        missing_files = required_file_validation.get("missing_files")
        missing_summary = ", ".join(sorted(str(file) for file in missing_files or []))
        return {
            "status": "blocked",
            "next_action": "update_pr_with_required_files",
            "error": f"Missing required file evidence: {missing_summary}",
        }

    if bool(readiness_policy.get("require_mergeable")):
        merge_state = str(pull_request.get("mergeStateStatus") or "").strip().upper()
        if merge_state and merge_state not in MERGEABLE_READY_STATES:
            return {
                "status": "blocked",
                "next_action": "resolve_mergeability_blockers",
                "error": f"PR merge state is not ready: {merge_state}",
            }

    required_approvals = int(readiness_policy.get("required_approvals") or 0)
    if bool(readiness_policy.get("require_review")) and required_approvals < 1:
        required_approvals = 1

    approvals = count_approving_reviews(pull_request)
    approved_count = int(approvals.get("approved_count") or 0)
    if required_approvals > approved_count:
        return {
            "status": "ready-for-review",
            "next_action": "await_required_approval" if args else "wait_for_review",
            "error": f"Waiting for required approvals: {approved_count}/{required_approvals}",
        }

    return {
        "status": "ready-to-merge",
        "next_action": "ready_for_merge",
        "error": None,
    }


def count_current_pr_approvals(reviews: list[dict], pr_author_login: str = "") -> int:
    return int(
        count_approving_reviews(
            {
                "reviews": reviews,
                "author": {"login": pr_author_login},
            }
        ).get("approved_count")
        or 0
    )


def build_workflow_hook_env(
    *,
    repo: str,
    mode: str,
    issue_number: int | str | None,
    pr_number: int | None,
    branch: str | None,
    base_branch: str | None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["ORCHESTRATOR_REPO"] = str(repo)
    env["ORCHESTRATOR_MODE"] = str(mode)
    if issue_number is not None:
        env["ORCHESTRATOR_ISSUE"] = str(issue_number)
    if pr_number is not None:
        env["ORCHESTRATOR_PR"] = str(pr_number)
    if branch:
        env["ORCHESTRATOR_BRANCH"] = str(branch)
    if base_branch:
        env["ORCHESTRATOR_BASE_BRANCH"] = str(base_branch)
    return env


def _workflow_output_excerpt(text: str, max_len: int = 600) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def run_configured_workflow_checks(
    checks: list[tuple[str, str]],
    dry_run: bool,
    cwd: str | None = None,
) -> list[dict]:
    if not checks:
        return []

    results: list[dict] = []
    for check_name, command_text in checks:
        if dry_run:
            print(f"[dry-run] Would run workflow check '{check_name}': {command_text}")
            results.append(
                {
                    "name": check_name,
                    "command": command_text,
                    "status": "dry-run",
                    "exit_code": None,
                }
            )
            continue

        print(f"Running workflow check '{check_name}': {command_text}")
        ok, stdout_text, stderr_text, exit_code = run_check_command(
            ["bash", "-lc", command_text],
            cwd=cwd,
        )
        result = {
            "name": check_name,
            "command": command_text,
            "status": "passed" if ok else "failed",
            "exit_code": exit_code,
        }
        if stdout_text:
            result["stdout_excerpt"] = _workflow_output_excerpt(stdout_text)
        if stderr_text:
            result["stderr_excerpt"] = _workflow_output_excerpt(stderr_text)

        results.append(result)

        if ok:
            print(f"Workflow check '{check_name}' passed")
            continue

        print(
            f"Workflow check '{check_name}' failed with exit code {exit_code}",
            file=sys.stderr,
        )
        raise WorkflowCheckFailure(failed_check=result, checks=results)

    return results


def detect_post_batch_verification_commands(cwd: str | None = None) -> list[tuple[str, str]]:
    target_dir = os.path.abspath(cwd or os.getcwd())
    commands: list[tuple[str, str]] = []
    if os.path.isdir(os.path.join(target_dir, "tests")):
        commands.append(POST_BATCH_VERIFICATION_DEFAULT_COMMANDS[0])
    if os.path.isfile(os.path.join(target_dir, "go.mod")):
        commands.append(POST_BATCH_VERIFICATION_DEFAULT_COMMANDS[1])
    return commands


def _summarize_post_batch_verification_results(results: list[dict]) -> str:
    command_count = len(results)
    passed_count = sum(1 for result in results if str(result.get("status") or "") == "passed")
    failed = [result for result in results if str(result.get("status") or "") == "failed"]
    if failed:
        failed_names = ", ".join(str(result.get("name") or "command") for result in failed)
        return f"failed ({passed_count}/{command_count} passed; failed: {failed_names})"
    return f"passed ({passed_count}/{command_count} commands)"


def _summarize_recovery_verification_results(results: list[dict[str, object]]) -> str:
    if not results:
        return "passed (0 commands)"
    failed = [result for result in results if str(result.get("status") or "") == "failed"]
    if failed:
        failed_names = ", ".join(str(result.get("name") or "command") for result in failed)
        return f"failed ({len(results) - len(failed)}/{len(results)} passed; failed: {failed_names})"
    return f"passed ({len(results)}/{len(results)} commands)"


def full_repo_verification_commands(
    *,
    project_config: dict,
    cwd: str | None,
) -> list[tuple[str, str]]:
    commands = configured_workflow_commands(project_config)
    if commands:
        return commands
    return detect_post_batch_verification_commands(cwd=cwd)


def run_recovery_focused_verification(
    *,
    checks: list[tuple[str, str]],
    branch_name: str,
    repo_dir: str,
    dry_run: bool,
) -> list[dict[str, object]]:
    if not checks:
        return []

    if dry_run:
        print(
            f"[dry-run] Would run focused recovery verification in a fresh clone for branch '{branch_name}'"
        )
        return run_configured_workflow_checks(checks=checks, dry_run=True)

    clone_dir = tempfile.mkdtemp(prefix=f"recovery-verify-{sanitize_branch_for_path(branch_name)}-")
    try:
        run_command(["git", "clone", "--quiet", repo_dir, clone_dir])
        run_command(["git", "-C", clone_dir, "checkout", branch_name])
        return run_configured_workflow_checks(checks=checks, dry_run=False, cwd=clone_dir)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def run_forced_recovery_verification(
    *,
    branch_name: str,
    project_config: dict,
    repo_dir: str,
    dry_run: bool,
) -> dict[str, object]:
    focused_checks = configured_recovery_focused_commands(project_config)
    full_repo_checks = full_repo_verification_commands(project_config=project_config, cwd=repo_dir)
    if not full_repo_checks:
        raise RuntimeError(
            "Conflict recovery verification requires configured workflow commands or detectable full-repo checks."
        )

    results: list[dict[str, object]] = []
    if focused_checks:
        print(
            f"Running focused recovery verification for branch '{branch_name}' in a fresh clone"
        )
        try:
            focused_results = run_recovery_focused_verification(
                checks=focused_checks,
                branch_name=branch_name,
                repo_dir=repo_dir,
                dry_run=dry_run,
            )
        except WorkflowCheckFailure as exc:
            failed_results = exc.checks if isinstance(exc.checks, list) else [exc.failed_check]
            raise RecoveryVerificationFailure(
                scope="focused",
                verification={
                    "status": "failed",
                    "summary": _summarize_recovery_verification_results(failed_results),
                    "error": f"Focused recovery verification failed: {exc}",
                    "commands": failed_results,
                },
            ) from exc
        results.extend(focused_results)

    print(f"Running full-repo recovery verification for branch '{branch_name}'")
    try:
        full_repo_results = run_configured_workflow_checks(
            checks=full_repo_checks,
            dry_run=dry_run,
            cwd=repo_dir,
        )
    except WorkflowCheckFailure as exc:
        failed_results = results + (exc.checks if isinstance(exc.checks, list) else [exc.failed_check])
        raise RecoveryVerificationFailure(
            scope="full-repo",
            verification={
                "status": "failed",
                "summary": _summarize_recovery_verification_results(failed_results),
                "error": f"Full-repo recovery verification failed: {exc}",
                "commands": failed_results,
            },
        ) from exc
    results.extend(full_repo_results)

    summary = _summarize_recovery_verification_results(results)
    print(f"Forced recovery verification result for branch '{branch_name}': {summary}")
    return {
        "status": "dry-run" if dry_run else "passed",
        "summary": summary,
        "commands": results,
    }


def format_post_batch_verification_issue_body(
    *,
    repo: str,
    verification: dict[str, object],
    touched_prs: list[str] | None = None,
) -> str:
    commands = verification.get("commands") if isinstance(verification.get("commands"), list) else []
    summary = _as_optional_string(verification.get("summary")) or "post-batch verification failed"
    next_action = _as_optional_string(verification.get("next_action")) or "inspect_verification_failures"

    lines = [
        "Automated post-batch verification detected a repository regression.",
        "",
        f"Repository: {repo}",
        f"Result: {summary}",
        f"Next action: {_humanize_status_token(next_action)}",
    ]
    if touched_prs:
        lines.extend(["", "Touched PRs:"])
        lines.extend(f"- {pr_url}" for pr_url in touched_prs)
    lines.extend(["", "Verification commands:"])
    for result in commands:
        if not isinstance(result, dict):
            continue
        name = str(result.get("name") or "command")
        command_text = str(result.get("command") or "")
        status = str(result.get("status") or "unknown")
        exit_code = result.get("exit_code")
        detail = f"- `{name}`: {status}"
        if exit_code is not None:
            detail += f" (exit code {exit_code})"
        if command_text:
            detail += f"\n  - command: `{command_text}`"
        evidence = _as_optional_string(result.get("stderr_excerpt")) or _as_optional_string(result.get("stdout_excerpt"))
        if evidence:
            detail += f"\n  - evidence: {evidence}"
        lines.append(detail)
    lines.extend(["", "Please fix the failing verification command(s) and rerun the post-batch verification path."])
    return "\n".join(lines)


def create_post_batch_follow_up_issue(
    *,
    repo: str,
    verification: dict[str, object],
    touched_prs: list[str] | None,
    dry_run: bool,
) -> dict[str, object]:
    commands = verification.get("commands") if isinstance(verification.get("commands"), list) else []
    failed = [result for result in commands if isinstance(result, dict) and str(result.get("status") or "") == "failed"]
    first_failed = failed[0] if failed else None
    failed_name = str(first_failed.get("name") or "verification") if isinstance(first_failed, dict) else "verification"
    title = f"Post-batch verification failed: {failed_name}"
    body = format_post_batch_verification_issue_body(
        repo=repo,
        verification=verification,
        touched_prs=touched_prs,
    )
    if dry_run:
        print(f"[dry-run] Would create follow-up issue: {title}")
        return {
            "status": "recommended",
            "title": title,
            "body": body,
            "issue_number": None,
            "issue_url": None,
        }

    created = gh_issue_create(repo, title, body)
    issue_number = created.get("number")
    if type(issue_number) is not int:
        raise RuntimeError("Created follow-up issue response missing integer number")
    return {
        "status": "created",
        "title": title,
        "body": body,
        "issue_number": issue_number,
        "issue_url": str(created.get("url") or ""),
    }


def run_post_batch_verification(
    *,
    repo: str,
    tracker: str,
    cwd: str | None,
    dry_run: bool,
    create_followup_issue: bool,
    touched_prs: list[str] | None = None,
) -> dict[str, object]:
    commands = detect_post_batch_verification_commands(cwd=cwd)
    if not commands:
        return {
            "status": "not-applicable",
            "summary": "not-applicable (no verification commands detected)",
            "commands": [],
            "next_action": "configure_post_batch_verification",
            "follow_up_issue": {"status": "not-needed"},
        }

    results: list[dict[str, object]] = []
    for check_name, command_text in commands:
        if dry_run:
            print(f"[dry-run] Would run post-batch verification '{check_name}': {command_text}")
            results.append(
                {
                    "name": check_name,
                    "command": command_text,
                    "status": "dry-run",
                    "exit_code": None,
                }
            )
            continue

        print(f"Running post-batch verification '{check_name}': {command_text}")
        ok, stdout_text, stderr_text, exit_code = run_check_command(
            ["bash", "-lc", command_text],
            cwd=cwd,
        )
        result: dict[str, object] = {
            "name": check_name,
            "command": command_text,
            "status": "passed" if ok else "failed",
            "exit_code": exit_code,
        }
        if stdout_text:
            result["stdout_excerpt"] = _workflow_output_excerpt(stdout_text)
        if stderr_text:
            result["stderr_excerpt"] = _workflow_output_excerpt(stderr_text)
        results.append(result)
        if ok:
            print(f"Post-batch verification '{check_name}' passed")
        else:
            print(
                f"Post-batch verification '{check_name}' failed with exit code {exit_code}",
                file=sys.stderr,
            )

    failed = [result for result in results if str(result.get("status") or "") == "failed"]
    if any(str(result.get("status") or "") == "dry-run" for result in results):
        return {
            "status": "dry-run",
            "summary": f"dry-run ({len(results)} commands)",
            "commands": results,
            "next_action": "run_post_batch_verification",
            "follow_up_issue": {"status": "not-requested"},
        }

    verification: dict[str, object] = {
        "status": "failed" if failed else "passed",
        "summary": _summarize_post_batch_verification_results(results),
        "commands": results,
        "next_action": "inspect_verification_failures" if failed else "none",
    }

    if not failed:
        verification["follow_up_issue"] = {"status": "not-needed"}
        return verification

    if create_followup_issue and tracker == TRACKER_GITHUB:
        follow_up_issue = create_post_batch_follow_up_issue(
            repo=repo,
            verification=verification,
            touched_prs=touched_prs,
            dry_run=dry_run,
        )
        verification["follow_up_issue"] = follow_up_issue
        verification["next_action"] = "fix_regression_from_follow_up_issue"
        return verification

    follow_up_issue = {
        "status": "recommended",
        "title": f"Post-batch verification failed: {str(failed[0].get('name') or 'verification')}",
        "body": format_post_batch_verification_issue_body(
            repo=repo,
            verification=verification,
            touched_prs=touched_prs,
        ),
    }
    verification["follow_up_issue"] = follow_up_issue
    verification["next_action"] = "create_follow_up_issue_and_fix_regression"
    return verification


def current_head_sha() -> str:
    return run_capture(["git", "rev-parse", "HEAD"]).strip()


def detect_repo() -> str:
    return _github_lifecycle.detect_repo(run_capture=run_capture)


def detect_default_branch(repo: str) -> str:
    return _github_lifecycle.detect_default_branch(repo, run_capture=run_capture)


def fetch_issues(repo: str, state: str, limit: int) -> list[dict]:
    return _github_lifecycle.fetch_issues(
        repo,
        state,
        limit,
        run_capture=run_capture,
        tracker_github=TRACKER_GITHUB,
    )


def fetch_issue(repo: str, number: int) -> dict:
    return _github_lifecycle.fetch_issue(
        repo,
        number,
        run_capture=run_capture,
        tracker_github=TRACKER_GITHUB,
    )


KNOWN_IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
MARKDOWN_IMAGE_URL_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMAGE_SRC_RE = re.compile(r"<img[^>]+src=['\"]([^'\"]+)['\"][^>]*>", re.IGNORECASE)
PLAIN_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\)\]]+\.(?:png|jpg|jpeg|gif|webp|bmp|svg|avif|heic|tif|tiff)(?:\?[^\s\)\]]*)?",
    re.IGNORECASE,
)


def _normalize_image_url(url: str) -> str:
    return url.strip().strip("`\"'<> ").rstrip(")];,.")


def _url_has_image_extension(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    _, ext = os.path.splitext((path or "").lower())
    if ext in KNOWN_IMAGE_EXTENSIONS:
        return True
    return False


def _is_image_attachment_candidate(
    url: str,
    filename: str | None = None,
    mime_type: str | None = None,
) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    if not url.lower().startswith(("http://", "https://")):
        return False

    if isinstance(mime_type, str) and mime_type.lower().startswith("image/"):
        return True

    if isinstance(filename, str) and _url_has_image_extension(filename):
        return True

    return _url_has_image_extension(url)


def _extract_markdown_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in MARKDOWN_IMAGE_URL_RE.finditer(text):
        if not match.groups():
            continue
        raw_url = _normalize_image_url(match.group(1))
        if raw_url and _is_image_attachment_candidate(raw_url) and raw_url not in seen:
            seen.add(raw_url)
            urls.append(raw_url)
    return urls


def _extract_html_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in HTML_IMAGE_SRC_RE.finditer(text):
        if not match.groups():
            continue
        raw_url = _normalize_image_url(match.group(1))
        if raw_url and _is_image_attachment_candidate(raw_url) and raw_url not in seen:
            seen.add(raw_url)
            urls.append(raw_url)
    return urls


def _extract_plain_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in PLAIN_IMAGE_URL_RE.finditer(text):
        raw_url = _normalize_image_url(match.group(0))
        if raw_url and raw_url not in seen and _is_image_attachment_candidate(raw_url):
            seen.add(raw_url)
            urls.append(raw_url)
    return urls


def _collect_issue_attachment_urls(payload: dict) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    fields_payload = payload.get("fields")
    if isinstance(fields_payload, dict):
        attachment_payload = fields_payload.get("attachment")
    else:
        attachment_payload = None

    for attachment_list in (payload.get("attachments"), fields_payload.get("attachment") if isinstance(fields_payload, dict) else None):
        if not isinstance(attachment_list, list):
            continue
        for attachment in attachment_list:
            if not isinstance(attachment, dict):
                continue
            filename = str(attachment.get("filename") or attachment.get("name") or "")
            mime_type = str(attachment.get("mimeType") or attachment.get("contentType") or "")
            for key in ("url", "content", "downloadUrl", "download_url", "self", "uri"):
                raw_url = attachment.get(key)
                if not isinstance(raw_url, str):
                    continue
                candidate = _normalize_image_url(raw_url)
                if (
                    candidate
                    and candidate not in seen
                    and _is_image_attachment_candidate(candidate, filename=filename, mime_type=mime_type)
                ):
                    seen.add(candidate)
                    urls.append(candidate)

    return urls


def collect_issue_image_urls(issue: dict) -> list[str]:
    if not isinstance(issue, dict):
        return []

    image_urls: list[str] = []
    seen: set[str] = set()

    def add_urls(values: list[str]) -> None:
        for image_url in values:
            if image_url in seen:
                continue
            seen.add(image_url)
            image_urls.append(image_url)

    for body_key in ("body", "bodyText", "body_html", "bodyHTML"):
        text = issue.get(body_key)
        if not isinstance(text, str):
            continue

        add_urls(_extract_markdown_image_urls(text))
        add_urls(_extract_html_image_urls(text))
        add_urls(_extract_plain_image_urls(text))

    add_urls(_collect_issue_attachment_urls(issue))

    return image_urls


def _safe_image_extension(content_type: str, url: str) -> str:
    if content_type:
        normalized_content_type = content_type.split(";", maxsplit=1)[0].strip().lower()
        if normalized_content_type:
            mapped_ext = mimetypes.guess_extension(normalized_content_type)
            if mapped_ext and mapped_ext.lower() in KNOWN_IMAGE_EXTENSIONS:
                return mapped_ext.lower()
    if _url_has_image_extension(url):
        return os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    return ".bin"


def download_image(url: str, destination_dir: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "steam-hammer-issue-image-fetcher/1.0",
            "Accept": "image/*",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        content_disposition = str(response.headers.get("Content-Disposition") or "")
        if "pdf" in content_type:
            raise RuntimeError(f"non-image content type received: {content_type}")

        ext = _safe_image_extension(content_type, url)
        if ext == ".bin":
            print(
                f"Warning: unable to infer image extension for {url}; saving as .bin",
                file=sys.stderr,
            )

        basename = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        filename = f"{basename}{ext}"
        if content_disposition:
            for part in content_disposition.split(";"):
                token = part.strip().lower()
                if token.startswith("filename="):
                    candidate = token.split("=", 1)[1].strip().strip('"')
                    base = os.path.basename(candidate)
                    if base:
                        filename = base
                    break

        destination_path = os.path.join(destination_dir, filename)
        with open(destination_path, "wb") as file:
            shutil.copyfileobj(response, file)

    return destination_path


def download_issue_images(image_urls: list[str], destination_dir: str, issue_number: int | None = None) -> list[str]:
    downloaded: list[str] = []
    seen: set[str] = set()

    for image_url in image_urls:
        if image_url in seen:
            continue
        seen.add(image_url)

        try:
            destination_path = download_image(url=image_url, destination_dir=destination_dir)
            downloaded.append(destination_path)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            issue_label = f"issue #{issue_number}" if issue_number is not None else "issue"
            print(
                f"Warning: failed to download attachment for {issue_label}: {image_url}. {exc}",
                file=sys.stderr,
            )

    return downloaded


def split_repo_name(repo: str) -> tuple[str, str]:
    return _github_lifecycle.split_repo_name(repo)


def _normalize_required_file_path(value: str) -> str:
    candidate = value.strip().strip("`\"'()[]{}<>.,;:")
    if not candidate:
        return ""
    candidate = candidate.replace("\\", "/")
    while "//" in candidate:
        candidate = candidate.replace("//", "/")
    return candidate.lstrip("./")


def _is_valid_file_path(value: str) -> bool:
    if not value:
        return False
    if "//" in value:
        return False
    if value.startswith("http://") or value.startswith("https://"):
        return False
    base = os.path.basename(value)
    if not base:
        return False
    if base.startswith(".") and base in {".gitignore", ".gitattributes", ".npmrc"}:
        return True
    if "." not in base:
        return base.lower() in KNOWN_NO_EXTENSION_REQUIRED_FILES
    if value.endswith("/"):
        return False
    if "/" not in value and value.startswith("#"):
        return False
    base_without_ext = os.path.basename(value).rpartition(".")
    extension = base_without_ext[2].lower()
    if not extension:
        return False
    return len(extension) <= 12


def extract_required_file_paths_from_text(text: str) -> list[str]:
    if not text:
        return []

    required_files: list[str] = []
    normalized_seen: set[str] = set()
    in_required_section = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if REQUIRED_FILE_SECTION_BREAK_HEADER.match(stripped):
            if in_required_section:
                in_required_section = False
            if REQUIRED_FILE_SECTION_HEADERS.match(stripped):
                in_required_section = True
            continue

        if REQUIRED_FILE_SECTION_HEADERS.match(stripped):
            in_required_section = True
            continue

        line_candidates = []
        for code_match in re.finditer(r"`([^`]+)`", stripped):
            line_candidates.append(code_match.group(1))

        if in_required_section or REQUIRED_FILE_HINT_LINE.search(stripped):
            line_candidates.append(stripped)

        for raw_candidate in line_candidates:
            for token_match in FILE_PATH_TOKEN_RE.finditer(raw_candidate):
                token = _normalize_required_file_path(token_match.group(0))
                if not token or token in normalized_seen:
                    continue
                if _is_valid_file_path(token):
                    normalized_seen.add(token)
                    required_files.append(token)

    return required_files


def collect_required_file_references_from_pr_context(
    pull_request: dict,
    linked_issues: list[dict] | None = None,
) -> list[str]:
    references: list[str] = []
    linked_issues_payload = linked_issues if isinstance(linked_issues, list) else []

    references.extend(extract_required_file_paths_from_text(str(pull_request.get("body") or "")))

    for issue in linked_issues_payload:
        if not isinstance(issue, dict):
            continue
        body = str(issue.get("body") or "").strip()
        if body:
            references.extend(extract_required_file_paths_from_text(body))

    return _dedupe_preserve_order(references)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = _normalize_required_file_path(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def extract_pull_request_changed_file_paths(pull_request: dict) -> list[str]:
    raw_files = pull_request.get("files")
    if not isinstance(raw_files, list):
        return []

    seen: set[str] = set()
    changed: list[str] = []
    for file_payload in raw_files:
        path = None
        if isinstance(file_payload, str):
            path = file_payload
        elif isinstance(file_payload, dict):
            for key in ("path", "filePath", "filename"):
                value = file_payload.get(key)
                if isinstance(value, str) and value.strip():
                    path = value
                    break
        if not isinstance(path, str):
            continue
        normalized = _normalize_required_file_path(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            changed.append(normalized)
    return changed


def validate_required_files_in_pr(pull_request: dict, linked_issues: list[dict] | None = None) -> dict[str, object]:
    required_files = collect_required_file_references_from_pr_context(
        pull_request=pull_request,
        linked_issues=linked_issues,
    )
    changed_paths = extract_pull_request_changed_file_paths(pull_request)
    changed_lookup = {path: path for path in changed_paths}
    changed_basenames = {os.path.basename(path): path for path in changed_paths}

    if not required_files:
        return {
            "status": "not-applicable",
            "required_file_count": 0,
            "required_files": [],
            "matched_files": [],
            "missing_files": [],
            "changed_file_count": len(changed_paths),
        }

    matched: list[str] = []
    missing: list[str] = []
    for required in required_files:
        required_normalized = _normalize_required_file_path(required)
        required_basename = os.path.basename(required_normalized)
        if (
            required_normalized in changed_lookup
            or required in changed_lookup
            or required_basename in changed_basenames
            or any(path.endswith(f"/{required_normalized}") for path in changed_paths)
        ):
            matched.append(required_normalized)
        else:
            missing.append(required_normalized)

    return {
        "status": "passed" if not missing else "blocked",
        "required_file_count": len(required_files),
        "required_files": required_files,
        "matched_files": matched,
        "missing_files": missing,
        "changed_file_count": len(changed_paths),
    }


def fetch_pull_request(repo: str, number: int) -> dict:
    return _github_lifecycle.fetch_pull_request(repo, number, run_capture=run_capture)


def fetch_pr_review_threads(repo: str, number: int) -> list[dict]:
    return _github_lifecycle.fetch_pr_review_threads(repo, number, run_capture=run_capture)


def _submitted_at_key(review: dict) -> str:
    value = review.get("submittedAt")
    if not isinstance(value, str):
        return ""
    return value


def latest_reviews_by_author(reviews: list[dict]) -> dict[str, dict]:
    latest_review_by_author: dict[str, dict] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_author = "unknown"
        author_payload = review.get("author")
        if isinstance(author_payload, dict):
            review_author = str(author_payload.get("login") or "unknown")
        key = review_author.lower()

        existing = latest_review_by_author.get(key)
        if existing is None or _submitted_at_key(review) >= _submitted_at_key(existing):
            latest_review_by_author[key] = review
    return latest_review_by_author


def derive_pr_review_decision(pull_request: dict) -> str:
    explicit = str(pull_request.get("reviewDecision") or "").strip().upper()
    if explicit in {"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
        return explicit

    reviews = pull_request.get("reviews")
    if not isinstance(reviews, list):
        return "UNKNOWN"

    latest_by_author = latest_reviews_by_author(reviews)
    latest_states = {
        str(review.get("state") or "").strip().upper()
        for review in latest_by_author.values()
        if isinstance(review, dict)
    }
    if "CHANGES_REQUESTED" in latest_states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in latest_states:
        return "APPROVED"
    return "UNKNOWN"


def evaluate_pr_merge_readiness(
    pull_request: dict,
    merge_policy: dict,
    merge_result_verification: dict[str, object] | None = None,
) -> dict:
    merge_state = str(pull_request.get("mergeStateStatus") or "").strip().upper() or "UNKNOWN"
    mergeable = str(pull_request.get("mergeable") or "").strip().upper() or "UNKNOWN"
    merge_readiness_state = classify_pr_merge_readiness_state(
        merge_state=merge_state,
        mergeable=mergeable,
    )
    is_draft = bool(pull_request.get("isDraft")) or merge_state == "DRAFT"
    review_decision = derive_pr_review_decision(pull_request)
    auto_merge_enabled = bool(merge_policy.get("auto", False))
    merge_method = str(merge_policy.get("method") or "squash")

    readiness = {
        "merge_state_status": merge_state,
        "mergeable": mergeable,
        "merge_readiness_state": merge_readiness_state,
        "review_decision": review_decision,
        "is_draft": is_draft,
        "auto_merge_enabled": auto_merge_enabled,
        "merge_method": merge_method,
        "status": "ready-to-merge",
        "stage": "merge_gate",
        "next_action": "ready_for_merge",
        "error": None,
    }
    if merge_result_verification is not None:
        readiness["merge_result_verification"] = merge_result_verification

    if is_draft:
        readiness.update(
            {
                "status": "waiting-for-author",
                "next_action": "mark_pr_ready_for_review",
                "error": "PR is still marked as draft",
            }
        )
        return readiness

    if merge_readiness_state == "conflicting":
        readiness.update(
            {
                "status": "blocked",
                "next_action": "resolve_merge_conflicts",
                "error": f"PR is not mergeable yet (mergeStateStatus={merge_state})",
            }
        )
        return readiness

    if merge_readiness_state == "stale":
        readiness.update(
            {
                "status": "blocked",
                "next_action": "sync_pr_with_base",
                "error": f"PR branch is stale and must be synced with base (mergeStateStatus={merge_state})",
            }
        )
        return readiness

    if review_decision == "CHANGES_REQUESTED":
        readiness.update(
            {
                "status": "waiting-for-author",
                "next_action": "address_requested_changes",
                "error": "Review state still has requested changes",
            }
        )
        return readiness

    if review_decision == "REVIEW_REQUIRED":
        readiness.update(
            {
                "status": "waiting-for-author",
                "next_action": "await_required_approval",
                "error": "Required approving review is still missing",
            }
        )
        return readiness

    if merge_readiness_state == "unknown":
        readiness.update(
            {
                "status": "blocked",
                "next_action": "inspect_merge_requirements",
                "error": f"GitHub has not marked this PR mergeable yet (mergeStateStatus={merge_state})",
            }
        )
        return readiness

    verification_status = str((merge_result_verification or {}).get("status") or "").strip().lower()
    verification_summary = _as_optional_string((merge_result_verification or {}).get("summary"))
    if verification_status == "failed":
        readiness.update(
            {
                "status": "blocked",
                "next_action": "inspect_merge_result_verification",
                "error": verification_summary or "Merge-result verification failed",
            }
        )
        return readiness

    return readiness


def classify_pr_merge_readiness_state(*, merge_state: str, mergeable: str) -> str:
    normalized_merge_state = merge_state.strip().upper() or "UNKNOWN"
    normalized_mergeable = mergeable.strip().upper() or "UNKNOWN"
    if normalized_mergeable == "CONFLICTING" or normalized_merge_state in {"DIRTY", "CONFLICTING"}:
        return "conflicting"
    if normalized_merge_state == "BEHIND":
        return "stale"
    if normalized_mergeable == "MERGEABLE":
        return "clean"
    return "unknown"


def run_merge_for_pull_request(repo: str, pr_number: int, merge_policy: dict, dry_run: bool) -> None:
    method = str(merge_policy.get("method") or "squash").strip().lower() or "squash"
    merge_flag = MERGE_METHOD_FLAGS[method]
    command = ["gh", "pr", "merge", str(pr_number), "--repo", repo, "--auto", merge_flag]
    if dry_run:
        print(
            f"[dry-run] Would request GitHub auto-merge for PR #{pr_number} "
            f"using method '{method}'"
        )
        return

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return

    stderr = str(result.stderr or "").strip()
    stdout = str(result.stdout or "").strip()
    detail = stderr or stdout or f"Command failed: {' '.join(command)}"
    normalized = detail.lower()
    if "auto-merge" in normalized and (
        "not enabled" in normalized
        or "not allowed" in normalized
        or "enablepullrequestautomerge" in normalized
        or "disabled" in normalized
    ):
        raise MergeRequestNotAcceptedError(
            status="ready-to-merge",
            next_action="merge_manually_or_enable_auto_merge",
            message=short_error_text(
                "Repository policy rejected GitHub auto-merge; merge manually or enable auto-merge for this repository."
            ),
        )
    if (
        "review required" in normalized
        or "required approving review" in normalized
        or "required status check" in normalized
        or "protected branch" in normalized
        or "pull request is not mergeable" in normalized
        or "base branch policy" in normalized
    ):
        raise MergeRequestNotAcceptedError(
            status="blocked",
            next_action="inspect_merge_requirements",
            message=short_error_text(detail),
        )
    raise RuntimeError(detail)


def finalize_pr_after_ci_success(
    repo: str,
    pr_number: int,
    linked_issues: list[dict] | None,
    merge_policy: dict,
    target_type: str,
    target_number: int,
    issue_number: int | None,
    branch: str | None,
    base_branch: str | None,
    runner: str,
    agent: str,
    model: str | None,
    attempt: int,
    ci_checks: list[dict] | None,
    decomposition: dict | None,
    project_config: dict,
    repo_dir: str,
    dry_run: bool,
) -> dict:
    pull_request = current_codehost_provider().fetch_pull_request(repo=repo, number=pr_number)
    required_file_validation = validate_required_files_in_pr(
        pull_request=pull_request,
        linked_issues=linked_issues,
    )
    if required_file_validation.get("status") == "blocked":
        missing_files = required_file_validation.get("missing_files")
        missing_summary = ", ".join(sorted(str(file) for file in missing_files))
        state = build_orchestration_state(
            status="blocked",
            task_type="pr",
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            runner=runner,
            agent=agent,
            model=model,
            attempt=attempt,
            stage="ci_checks",
            next_action="update_pr_with_required_files",
            error=f"Missing required file evidence: {missing_summary}",
            ci_checks=ci_checks,
            decomposition=decomposition,
            required_file_validation=required_file_validation,
            merge_policy=merge_policy,
        )
        safe_post_orchestration_state_comment(
            repo=repo,
            target_type=target_type,
            target_number=target_number,
            dry_run=dry_run,
            state=state,
        )
        print(
            f"PR #{pr_number} CI passed but required file evidence check failed. "
            f"Missing files: {missing_summary}"
        )
        return state

    readiness = evaluate_pr_readiness(
        pull_request=pull_request,
        ci_status={"checks": ci_checks or []},
        required_file_validation=required_file_validation,
        project_config=project_config,
    )
    readiness_status = str(readiness.get("status") or "blocked")
    if readiness_status != "ready-to-merge":
        state = build_orchestration_state(
            status=readiness_status,
            task_type="pr",
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            runner=runner,
            agent=agent,
            model=model,
            attempt=attempt,
            stage="ci_checks",
            next_action=str(readiness.get("next_action") or "ready_for_merge"),
            error=_as_optional_string(readiness.get("error")),
            ci_checks=ci_checks,
            decomposition=decomposition,
            required_file_validation=required_file_validation,
            merge_policy=merge_policy,
        )
        safe_post_orchestration_state_comment(
            repo=repo,
            target_type=target_type,
            target_number=target_number,
            dry_run=dry_run,
            state=state,
        )
        if readiness_status == "ready-for-review":
            print(
                f"CI checks passed for PR #{pr_number}, but review requirements are not met yet: "
                f"{_as_optional_string(readiness.get('error')) or 'waiting for review'}"
            )
        elif readiness_status == "waiting-for-ci":
            print(
                f"PR #{pr_number} is still waiting for required checks: "
                f"{_as_optional_string(readiness.get('error')) or 'waiting for CI'}"
            )
        else:
            print(
                f"PR #{pr_number} is not ready to merge yet: "
                f"{_as_optional_string(readiness.get('error')) or 'readiness policy blocked'}"
            )
        return state

    merge_result_verification = verify_pull_request_merge_result(
        repo=repo,
        pull_request=pull_request,
        project_config=project_config,
        repo_dir=repo_dir,
        dry_run=dry_run,
    )
    merge_readiness = evaluate_pr_merge_readiness(
        pull_request=pull_request,
        merge_policy=merge_policy,
        merge_result_verification=merge_result_verification,
    )
    readiness_status = str(merge_readiness.get("status") or "blocked")
    next_action = str(merge_readiness.get("next_action") or "inspect_merge_requirements")
    error = _as_optional_string(merge_readiness.get("error"))

    if readiness_status != "ready-to-merge":
        state = build_orchestration_state(
            status=readiness_status,
            task_type="pr",
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            runner=runner,
            agent=agent,
            model=model,
            attempt=attempt,
            stage="merge_gate",
            next_action=next_action,
            error=error,
            ci_checks=ci_checks,
            decomposition=decomposition,
            required_file_validation=required_file_validation,
            merge_readiness=merge_readiness,
            merge_policy=merge_policy,
        )
        safe_post_orchestration_state_comment(
            repo=repo,
            target_type=target_type,
            target_number=target_number,
            dry_run=dry_run,
            state=state,
        )
        if readiness_status == "waiting-for-author":
            print(
                f"CI checks passed for PR #{pr_number}, but merge is waiting on human action: {error}"
            )
        else:
            print(f"CI checks passed for PR #{pr_number}, but merge is blocked: {error}")
        return state

    state_stage = "merge_gate"
    state_next_action = "ready_for_merge"
    if merge_policy.get("auto"):
        try:
            run_merge_for_pull_request(
                repo=repo,
                pr_number=pr_number,
                merge_policy=merge_policy,
                dry_run=dry_run,
            )
            state_stage = "merge_execution"
            state_next_action = "await_github_auto_merge"
            print(
                f"CI checks passed for PR #{pr_number}; merge gate passed and GitHub auto-merge was requested."
            )
        except MergeRequestNotAcceptedError as exc:
            state = build_orchestration_state(
                status=exc.status,
                task_type="pr",
                issue_number=issue_number,
                pr_number=pr_number,
                branch=branch,
                base_branch=base_branch,
                runner=runner,
                agent=agent,
                model=model,
                attempt=attempt,
                stage="merge_gate",
                next_action=exc.next_action,
                error=exc.message,
                ci_checks=ci_checks,
                decomposition=decomposition,
                required_file_validation=required_file_validation,
                merge_readiness=merge_readiness,
                merge_policy=merge_policy,
            )
            safe_post_orchestration_state_comment(
                repo=repo,
                target_type=target_type,
                target_number=target_number,
                dry_run=dry_run,
                state=state,
            )
            print(f"CI checks passed for PR #{pr_number}, but auto-merge was not accepted: {exc.message}")
            return state
    else:
        print(
            f"CI checks passed for PR #{pr_number}; merge gate passed and auto-merge is disabled, marking ready-to-merge."
        )

    state = build_orchestration_state(
        status="ready-to-merge",
        task_type="pr",
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        runner=runner,
        agent=agent,
        model=model,
        attempt=attempt,
        stage=state_stage,
        next_action=state_next_action,
        error=None,
        ci_checks=ci_checks,
        decomposition=decomposition,
        required_file_validation=required_file_validation,
        merge_readiness=merge_readiness,
        merge_policy=merge_policy,
    )
    safe_post_orchestration_state_comment(
        repo=repo,
        target_type=target_type,
        target_number=target_number,
        dry_run=dry_run,
        state=state,
    )
    return state


def build_decomposition_rollup_from_plan_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {
            "parent_issue": None,
            "counts": {status: 0 for status in DECOMPOSITION_CHILD_STATUSES},
            "children": [],
            "blockers": [],
            "progress": {"completed": 0, "total": 0, "percent": 0},
            "next_child": None,
            "next_action_hint": "no_children",
            "source": "plan",
        }

    plan_parent_issue = _as_positive_int(payload.get("parent_issue"))
    proposed_children = normalize_decomposition_proposed_children(payload)
    created_children = _normalize_created_children(payload.get("created_children") or [])
    created_by_order = {
        child.get("order"): child for child in created_children if isinstance(child.get("order"), int)
    }

    blockers_raw = payload.get("blockers")
    blockers = [str(blocker).strip() for blocker in blockers_raw if str(blocker).strip()] if isinstance(blockers_raw, list) else []

    effective_status_by_order: dict[int, str] = {}
    for child in proposed_children:
        order = child.get("order")
        if type(order) is not int:
            continue
        created = created_by_order.get(order)
        created = created if isinstance(created, dict) else {}

        issue_number = created.get("issue_number")
        child_status = _normalize_child_status(created.get("status") or child.get("status"))
        if child_status == "planned" and isinstance(issue_number, int):
            child_status = "created"
        effective_status_by_order[order] = child_status

    rollup_children: list[dict] = []
    counts = {status: 0 for status in DECOMPOSITION_CHILD_STATUSES}
    next_child: dict | None = None
    total_children = len(proposed_children)

    for child in proposed_children:
        order = child.get("order")
        if type(order) is not int:
            continue

        created = created_by_order.get(order)
        created = created if isinstance(created, dict) else {}

        issue_number = created.get("issue_number")
        if not isinstance(issue_number, int):
            issue_number = child.get("issue_number")
        issue_url = str(created.get("issue_url") or child.get("issue_url") or "").strip()
        child_status = _normalize_child_status(created.get("status") or child.get("status"))
        if child_status == "planned" and isinstance(issue_number, int):
            child_status = "created"

        rollup_child: dict = {
            "order": order,
            "title": str(child.get("title") or f"Child task {order}"),
            "status": child_status,
            "issue_number": issue_number if isinstance(issue_number, int) else None,
            "depends_on": list(child.get("depends_on") or []),
        }
        if issue_url:
            rollup_child["issue_url"] = issue_url
        rollup_children.append(rollup_child)

        counts[child_status] += 1

        dependencies = child.get("depends_on") if isinstance(child.get("depends_on"), list) else []
        dependency_orders: list[int] = []
        for dependency in dependencies:
            dependency_order = _as_positive_int(dependency)
            if dependency_order is not None:
                dependency_orders.append(dependency_order)
        dependencies_satisfied = all(
            effective_status_by_order.get(dependency_order) == "done"
            for dependency_order in dependency_orders
        )

        if (
            child_status in {"planned", "created", "in-progress"}
            and dependencies_satisfied
            and next_child is None
        ):
            next_child = {
                "order": order,
                "title": str(child.get("title") or f"Child task {order}"),
                "status": child_status,
                "depends_on": dependency_orders,
            }
            if isinstance(issue_number, int):
                next_child["issue_number"] = issue_number
            if issue_url:
                next_child["issue_url"] = issue_url

    completed_children = counts["done"]
    percent_done = 0 if total_children == 0 else int((completed_children / total_children) * 100)
    next_action_hint = str(payload.get("next_action") or "").strip() or (
        "execute_next_child" if next_child is not None else "all_children_complete"
    )
    resume_context = payload.get("resume_context")
    if not isinstance(resume_context, dict):
        resume_context = None

    rollup = {
        "parent_issue": plan_parent_issue,
        "counts": counts,
        "children": rollup_children,
        "total_children": total_children,
        "blockers": blockers,
        "progress": {
            "completed": completed_children,
            "total": total_children,
            "percent": percent_done,
        },
        "next_child": next_child,
        "next_action_hint": next_action_hint,
        "source": "plan_payload",
    }
    if resume_context is not None:
        rollup["resume_context"] = dict(resume_context)
    return rollup


def build_decomposition_rollup_from_recovered_state(
    recovered_state: dict | None,
    parent_issue: int | None,
) -> dict | None:
    if not isinstance(recovered_state, dict):
        return None

    state_payload = recovered_state.get("payload") if isinstance(recovered_state, dict) else None
    if not isinstance(state_payload, dict):
        return None

    decomposition = state_payload.get("decomposition")
    if not isinstance(decomposition, dict):
        decomposition = recovered_state.get("decomposition")
    if not isinstance(decomposition, dict):
        return None

    if "counts" in decomposition and isinstance(decomposition.get("children"), list):
        rollup = dict(decomposition)
    elif "proposed_children" in decomposition:
        rollup = build_decomposition_rollup_from_plan_payload(decomposition)
    else:
        return None

    if parent_issue is not None:
        rollup["parent_issue"] = parent_issue

    if "resume_context" not in rollup:
        rollup["resume_context"] = {
            "task_type": str(state_payload.get("task_type") or ""),
            "status": str(recovered_state.get("status") or state_payload.get("status") or ""),
            "stage": str(state_payload.get("stage") or ""),
            "branch": str(state_payload.get("branch") or ""),
        }
    resume_context = rollup.get("resume_context")
    if isinstance(resume_context, dict):
        if str(resume_context.get("branch") or "") == "":
            resume_context["branch"] = str(state_payload.get("branch") or "")
        if str(resume_context.get("base_branch") or "") == "":
            resume_context["base_branch"] = str(state_payload.get("base_branch") or "")
        if str(resume_context.get("task_type") or "") == "":
            resume_context["task_type"] = str(state_payload.get("task_type") or "")
        resolved_pr = state_payload.get("pr")
        if "pr" not in resume_context:
            resume_context["pr"] = resolved_pr if isinstance(resolved_pr, int) else None

    return rollup


def format_decomposition_rollup_context(decomposition: dict) -> str:
    if not isinstance(decomposition, dict):
        return ""

    parent_issue = decomposition.get("parent_issue")
    parent_text = str(parent_issue) if isinstance(parent_issue, int) else "?"

    counts = decomposition.get("counts")
    counts_dict = counts if isinstance(counts, dict) else {}
    total_children = 0
    try:
        total_children = int(decomposition.get("total_children") or 0)
    except (TypeError, ValueError):
        total_children = 0

    count_bits: list[str] = []
    for status in DECOMPOSITION_CHILD_STATUSES:
        try:
            count_bits.append(f"{status}={int(counts_dict.get(status) or 0)}")
        except (TypeError, ValueError):
            count_bits.append(f"{status}=0")

    next_child_payload = decomposition.get("next_child")
    next_child_text = "none"
    if isinstance(next_child_payload, dict):
        next_order = next_child_payload.get("order")
        next_title = str(next_child_payload.get("title") or "").strip()
        next_status = str(next_child_payload.get("status") or "").strip()
        issue_number = next_child_payload.get("issue_number")
        if isinstance(issue_number, int):
            next_child_text = f"{next_order}:{next_title} (#{issue_number}, {next_status})"
        else:
            next_child_text = f"{next_order}:{next_title} ({next_status})"

    blockers = decomposition.get("blockers")
    blockers_text = _safe_join_sorted(blockers) if blockers else ""
    blockers_suffix = f"; blockers={blockers_text}" if blockers_text else ""

    resume_context = decomposition.get("resume_context")
    resume_bits: list[str] = []
    if isinstance(resume_context, dict):
        branch = str(resume_context.get("branch") or "").strip()
        pr = resume_context.get("pr")
        if isinstance(pr, int) and pr > 0:
            resume_bits.append(f"pr={pr}")
        if branch:
            resume_bits.append(f"branch={branch}")
        base_branch = str(resume_context.get("base_branch") or "").strip()
        if base_branch:
            resume_bits.append(f"base={base_branch}")
    resume_suffix = f"; resume={'; '.join(resume_bits)}" if resume_bits else ""

    progress = decomposition.get("progress")
    completed = 0
    percent = 0
    if isinstance(progress, dict):
        completed = int(progress.get("completed") or 0)
        percent = int(progress.get("percent") or 0)

    return (
        "decomposition(" \
        f"parent=#{parent_text}, "
        f"children={total_children}, "
        f"counts=({', '.join(count_bits)}), "
        f"done={completed}/{total_children} ({percent}%), "
        f"next={next_child_text}" \
        f"{blockers_suffix}{resume_suffix}"
        ")"
    )


def _normalize_decomposition_plan_child(child: dict, fallback_order: int) -> dict | None:
    if not isinstance(child, dict):
        return None

    raw_order = child.get("order")
    try:
        order = int(raw_order)
    except (TypeError, ValueError):
        order = fallback_order
    if order <= 0:
        order = fallback_order

    title = str(child.get("title") or "").strip() or f"Child task {order}"
    depends_raw = child.get("depends_on")
    depends_on: list[int] = []
    if isinstance(depends_raw, list):
        for dep in depends_raw:
            try:
                dep_order = int(dep)
            except (TypeError, ValueError):
                continue
            if dep_order > 0:
                depends_on.append(dep_order)

    acceptance_raw = child.get("acceptance")
    acceptance: list[str] = []
    if isinstance(acceptance_raw, list):
        acceptance = [str(item).strip() for item in acceptance_raw if str(item).strip()]
    if not acceptance:
        acceptance = [f"{title} is completed and validated."]

    status = _normalize_child_status(child.get("status"))
    issue_number = _as_positive_int(child.get("issue_number"))
    if issue_number is None:
        issue_number = _as_positive_int(child.get("issue"))
    issue_url = str(child.get("issue_url") or "").strip()

    normalized_child = {
        "title": title,
        "order": order,
        "depends_on": sorted(set(depends_on)),
        "acceptance": acceptance,
        "status": status,
        "issue_number": issue_number,
    }
    if issue_url:
        normalized_child["issue_url"] = issue_url
    return normalized_child


def normalize_decomposition_proposed_children(plan_payload: dict) -> list[dict]:
    raw_children = plan_payload.get("proposed_children")
    if not isinstance(raw_children, list):
        return []

    normalized: list[dict] = []
    for index, child in enumerate(raw_children, start=1):
        normalized_child = _normalize_decomposition_plan_child(child, fallback_order=index)
        if normalized_child is None:
            continue
        normalized.append(normalized_child)

    return sorted(normalized, key=lambda item: int(item.get("order") or 0))


def _extract_ordered_linked_children(plan_payload: dict) -> dict[int, dict]:
    raw_created = plan_payload.get("created_children")
    if not isinstance(raw_created, list):
        return {}

    created_by_order: dict[int, dict] = {}
    for child in raw_created:
        if not isinstance(child, dict):
            continue
        try:
            order = int(child.get("order"))
        except (TypeError, ValueError):
            continue

        if order <= 0:
            continue
        child_copy = dict(child)
        child_copy["order"] = order
        created_by_order[order] = child_copy

    return created_by_order


def _normalize_created_children(raw_created_children: list[dict] | dict[int, dict]) -> list[dict]:
    created_children_values: list[dict] = []
    if isinstance(raw_created_children, dict):
        created_children_values = [
            child for child in raw_created_children.values() if isinstance(child, dict)
        ]
    elif isinstance(raw_created_children, list):
        created_children_values = [
            child for child in raw_created_children if isinstance(child, dict)
        ]
    else:
        return []

    normalized: list[dict] = []
    for child in created_children_values:
        child_copy = dict(child)
        try:
            order = int(child_copy.get("order"))
        except (TypeError, ValueError):
            continue

        if order <= 0:
            continue
        child_copy["order"] = order
        normalized.append(child_copy)

    normalized.sort(key=lambda item: int(item.get("order") or 0))
    return normalized


def _classify_decomposition_child_execution_status(
    child_issue: dict,
    recovered_state: dict | None,
) -> tuple[str, str | None]:
    issue_state = str(child_issue.get("state") or "").strip().lower()
    if issue_state == "closed":
        return "done", None

    recovered_status = str(recovered_state.get("status") or "") if isinstance(recovered_state, dict) else ""
    if recovered_status in {"in-progress", "ready-for-review", "waiting-for-ci", "ready-to-merge"}:
        return "in-progress", None

    if recovered_status in {"blocked", "failed", "waiting-for-author"}:
        payload = recovered_state.get("payload") if isinstance(recovered_state, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        blocker = str(
            payload.get("error")
            or payload.get("reason")
            or payload.get("summary")
            or recovered_status
        ).strip()
        return "blocked", blocker or recovered_status

    return "created", None


def refresh_decomposition_plan_payload_from_child_states(
    repo: str,
    plan_payload: dict,
) -> dict:
    tracker_provider = current_tracker_provider()
    refreshed_payload = dict(plan_payload)
    refreshed_created_children = _normalize_created_children(plan_payload.get("created_children") or [])
    blockers: list[str] = []

    for index, child in enumerate(refreshed_created_children):
        issue_number = _as_positive_int(child.get("issue_number"))
        if issue_number is None:
            continue

        child_issue = tracker_provider.get_issue(repo=repo, issue_id=issue_number)
        child_comments = tracker_provider.list_issue_comments(repo=repo, issue_id=issue_number)
        recovered_state, _warnings = select_latest_parseable_orchestration_state(
            comments=child_comments,
            source_label=f"issue #{issue_number}",
        )
        status, blocker = _classify_decomposition_child_execution_status(
            child_issue=child_issue,
            recovered_state=recovered_state,
        )

        updated_child = dict(child)
        updated_child["status"] = status
        child_url = str(updated_child.get("issue_url") or child_issue.get("url") or "").strip()
        if child_url:
            updated_child["issue_url"] = child_url
        child_title = str(updated_child.get("title") or child_issue.get("title") or "").strip()
        if child_title:
            updated_child["title"] = child_title
        refreshed_created_children[index] = updated_child

        if blocker:
            child_order = updated_child.get("order")
            child_prefix = f"step {child_order}" if isinstance(child_order, int) else f"issue #{issue_number}"
            blockers.append(f"{child_prefix}: {blocker}")

    refreshed_payload["created_children"] = refreshed_created_children
    refreshed_payload["blockers"] = sorted(set(blockers))
    refreshed_payload["timestamp"] = utc_now_iso()

    rollup = build_decomposition_rollup_from_plan_payload(refreshed_payload)
    next_child = rollup.get("next_child")
    if isinstance(next_child, dict):
        refreshed_payload["next_action"] = "execute_next_child"
    elif int(rollup.get("progress", {}).get("completed") or 0) >= int(rollup.get("total_children") or 0):
        refreshed_payload["next_action"] = "all_children_complete"
    elif blockers:
        refreshed_payload["next_action"] = "resolve_blocked_child"
    else:
        refreshed_payload["next_action"] = "await_child_dependencies"
    return refreshed_payload


def build_decomposition_resume_context(
    parent_issue: dict,
    parent_branch: str | None,
    base_branch: str | None,
    next_action: str,
    selected_child: dict | None = None,
) -> dict:
    context = {
        "task_type": "issue",
        "parent_issue": parent_issue.get("number"),
        "branch": str(parent_branch or "").strip(),
        "base_branch": str(base_branch or "").strip(),
        "next_action": str(next_action or "").strip(),
        "resume_issue": parent_issue.get("number"),
    }
    if isinstance(selected_child, dict):
        child_context = {
            "order": selected_child.get("order"),
            "title": str(selected_child.get("title") or "").strip(),
            "issue_number": _as_positive_int(selected_child.get("issue_number")),
        }
        context["selected_child"] = child_context
    return context


def attach_decomposition_resume_context(
    plan_payload: dict,
    parent_issue: dict,
    parent_branch: str | None,
    base_branch: str | None,
    next_action: str,
    selected_child: dict | None = None,
) -> dict:
    annotated_payload = dict(plan_payload)
    annotated_payload["resume_context"] = build_decomposition_resume_context(
        parent_issue=parent_issue,
        parent_branch=parent_branch,
        base_branch=base_branch,
        next_action=next_action,
        selected_child=selected_child,
    )
    return annotated_payload


def build_decomposition_child_execution_note(
    parent_issue: dict,
    decomposition_rollup: dict,
    selected_child: dict,
) -> str:
    parent_number = parent_issue.get("number")
    parent_title = str(parent_issue.get("title") or "").strip()
    child_order = selected_child.get("order")
    child_title = str(selected_child.get("title") or "").strip()
    rollup_context = format_decomposition_rollup_context(decomposition_rollup)
    return (
        "Parent decomposition context:\n"
        f"- Parent issue: #{parent_number} - {parent_title}\n"
        f"- Selected child step: {child_order}: {child_title}\n"
        f"- Current roll-up: {rollup_context}\n"
        "- Preserve dependency order and update the parent tracker state through normal orchestration outputs."
    )


def post_parent_decomposition_rollup_update(
    repo: str,
    parent_issue: dict,
    parent_branch: str,
    base_branch: str | None,
    runner: str,
    agent: str,
    model: str | None,
    plan_payload: dict,
    dry_run: bool,
) -> tuple[dict, dict]:
    refreshed_payload = refresh_decomposition_plan_payload_from_child_states(
        repo=repo,
        plan_payload=plan_payload,
    )
    rollup = build_decomposition_rollup_from_plan_payload(refreshed_payload)
    blockers = rollup.get("blockers") if isinstance(rollup.get("blockers"), list) else []
    next_child = rollup.get("next_child") if isinstance(rollup.get("next_child"), dict) else None
    progress = rollup.get("progress") if isinstance(rollup.get("progress"), dict) else {}
    completed = int(progress.get("completed") or 0)
    total = int(progress.get("total") or 0)

    if total > 0 and completed >= total:
        state_status = "ready-for-review"
        next_action = "review_completed_children"
        error = None
    elif blockers:
        state_status = "blocked"
        next_action = "resolve_blocked_child"
        error = short_error_text("; ".join(str(blocker) for blocker in blockers))
    else:
        state_status = "in-progress"
        next_action = str(refreshed_payload.get("next_action") or "execute_next_child")
        error = None

    refreshed_payload = attach_decomposition_resume_context(
        plan_payload=refreshed_payload,
        parent_issue=parent_issue,
        parent_branch=parent_branch,
        base_branch=base_branch,
        next_action=next_action,
        selected_child=next_child,
    )
    rollup = build_decomposition_rollup_from_plan_payload(refreshed_payload)

    post_decomposition_plan_comment(
        repo=repo,
        issue_number=parent_issue["number"],
        payload=refreshed_payload,
        dry_run=dry_run,
    )
    safe_post_orchestration_state_comment(
        repo=repo,
        target_type="issue",
        target_number=parent_issue["number"],
        dry_run=dry_run,
        state=build_orchestration_state(
            status=state_status,
            task_type="issue",
            issue_number=parent_issue["number"],
            pr_number=None,
            branch=parent_branch,
            base_branch=base_branch,
            runner=runner,
            agent=agent,
            model=model,
            attempt=1,
            stage="decomposition_execution",
            next_action=next_action,
            error=error,
            decomposition=rollup,
        ),
    )
    return refreshed_payload, rollup


def is_decomposition_plan_approved(plan_payload: dict) -> bool:
    status = str(plan_payload.get("status") or "").strip().lower()
    return status in {"approved", "children_created", "execution_plan"}


def _build_child_issue_body(
    parent_issue: dict,
    child: dict,
    created_dependencies: dict[int, dict],
    parent_branch: str | None = None,
    base_branch: str | None = None,
) -> str:
    parent_number = parent_issue.get("number")
    parent_title = str(parent_issue.get("title") or "(untitled)").strip()
    order = child.get("order")
    depends_on = child.get("depends_on")
    if isinstance(depends_on, list):
        dependency_lines: list[str] = []
        for dep in depends_on:
            try:
                dep_order = int(dep)
            except (TypeError, ValueError):
                continue

            dependency_child = created_dependencies.get(dep_order)
            dependency_ref = f"step {dep_order}"
            dep_issue_number = dependency_child.get("issue_number")
            dep_issue_url = str(dependency_child.get("issue_url") or "").strip()
            if isinstance(dep_issue_number, int):
                dependency_ref = f"[{DECOMPOSITION_CHILD_ORDER_PREFIX} {dep_order}: #{dep_issue_number}]({dep_issue_url})"
            elif dep_issue_url:
                dependency_ref = f"[{DECOMPOSITION_CHILD_ORDER_PREFIX} {dep_order}: {dep_issue_url}]"
            dependency_lines.append(dependency_ref)
    else:
        dependency_lines = []

    execution_order_text = str(order) if isinstance(order, int) else "unknown"
    dependency_text = (
        "\nDepends on: " + ", ".join(dependency_lines)
        if dependency_lines
        else "\nDepends on: none"
    )
    branch_context_lines: list[str] = []
    parent_branch_text = str(parent_branch or "").strip()
    base_branch_text = str(base_branch or "").strip()
    if parent_branch_text:
        branch_context_lines.append(f"- Parent orchestration branch: `{parent_branch_text}`")
    if base_branch_text:
        branch_context_lines.append(f"- Base branch: `{base_branch_text}`")
    branch_context_text = "\n".join(branch_context_lines) or "- Branch context will be selected by the parent orchestration run."
    parent_ref = format_issue_ref_from_issue(parent_issue)
    resume_lines = [
        f"- Preferred resume path: rerun the orchestrator for parent issue {parent_ref}; it will select the next unblocked child in dependency order.",
        f"- Parent issue branch context: `{parent_branch_text or 'resolved at runtime'}`.",
    ]
    if dependency_lines:
        resume_lines.append("- Do not start this task until the listed dependencies are completed.")

    acceptance = child.get("acceptance")
    acceptance_lines: list[str] = []
    if isinstance(acceptance, list):
        for criterion in acceptance:
            criterion_text = str(criterion or "").strip()
            if criterion_text:
                acceptance_lines.append(f"- {criterion_text}")

    if not acceptance_lines:
        acceptance_lines = ["- Implementation is complete and validated."]

    resume_text = "\n".join(resume_lines)

    return (
        "Child task generated from decomposition plan\n\n"
        f"Parent issue: {parent_ref} ({parent_title})\n"
        f"Execution order: {execution_order_text}\n"
        f"{dependency_text}\n\n"
        "Branch context:\n"
        f"{branch_context_text}\n\n"
        "Resume instructions:\n"
        f"{resume_text}\n\n"
        f"Suggested acceptance criteria:\n" + "\n".join(acceptance_lines)
    )


def create_decomposition_child_issue(
    repo: str,
    parent_issue: dict,
    child: dict,
    created_dependencies: dict[int, dict],
    dry_run: bool,
    parent_branch: str | None = None,
    base_branch: str | None = None,
) -> dict:
    child_title = str(child.get("title") or "")[:120]
    if not child_title:
        raise RuntimeError("Cannot create child issue with empty title")

    body = _build_child_issue_body(
        parent_issue,
        child,
        created_dependencies,
        parent_branch=parent_branch,
        base_branch=base_branch,
    )
    if dry_run:
        print(f"[dry-run] Would create child issue for order {child.get('order')} of parent #{parent_issue.get('number')}")
        return {
            "title": child_title,
            "order": child.get("order"),
            "issue_number": None,
            "issue_url": None,
            "created": False,
        }

    created = gh_issue_create(repo, child_title, body)
    issue_number = created.get("number")
    if type(issue_number) is not int:
        raise RuntimeError("Created child issue response missing integer number")

    return {
        "title": child_title,
        "order": child.get("order"),
        "issue_number": issue_number,
        "issue_url": str(created.get("url") or ""),
        "created": True,
    }


def _decomposition_plan_has_missing_children(plan_payload: dict) -> list[dict]:
    proposed_children = normalize_decomposition_proposed_children(plan_payload)
    created_children = _extract_ordered_linked_children(plan_payload)

    missing_children: list[dict] = []
    for child in proposed_children:
        order = child.get("order")
        if order not in created_children:
            missing_children.append(child)

    return missing_children


def merge_created_children_into_plan_payload(
    plan_payload: dict,
    created_children: list[dict],
) -> dict:
    merged_children: list[dict] = []
    existing_children = _extract_ordered_linked_children(plan_payload)
    for order in sorted(existing_children):
        merged_children.append(dict(existing_children[order]))

    for child in created_children:
        order = child.get("order")
        if type(order) is not int:
            continue
        if order < 1:
            continue
        child_copy = dict(child)
        child_copy["order"] = order
        merged_children = [
            existing
            for existing in merged_children
            if existing.get("order") != order
        ] + [child_copy]

    merged_children.sort(key=lambda item: int(item.get("order") or 0))

    merged_payload = dict(plan_payload)
    merged_payload["created_children"] = merged_children
    merged_payload["timestamp"] = utc_now_iso()
    return merged_payload


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
    state_payload = state.get("payload") if isinstance(state, dict) else None
    decomposition_payload = state.get("decomposition") if isinstance(state, dict) else None
    if decomposition_payload is None and isinstance(state_payload, dict):
        decomposition_payload = state_payload.get("decomposition")

    details = f"status={status}; source={source}; created_at={created_at}"

    if isinstance(decomposition_payload, dict):
        decomposition_context = format_decomposition_rollup_context(decomposition_payload)
        if decomposition_context:
            details += f"; {decomposition_context}"

    if url:
        details += f"; comment={url}"
    return details


def _humanize_status_token(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return text.replace("_", " ").replace("-", " ")


def _status_payload(state: dict | None) -> dict:
    payload = state.get("payload") if isinstance(state, dict) else None
    return payload if isinstance(payload, dict) else {}


def _collect_status_done_items(
    *,
    issue_number: int | str | None,
    pr_number: int | None,
    branch: str | None,
    required_file_validation: dict | None,
    ci_status: dict | None,
    merge_readiness: dict | None,
    decomposition: dict | None,
) -> list[str]:
    done: list[str] = []
    if is_trackable_issue_number(issue_number):
        done.append(f"issue {format_issue_ref(issue_number)}")
    if isinstance(pr_number, int) and pr_number > 0:
        done.append(f"pr #{pr_number}")
    if branch:
        done.append(f"branch {branch}")

    if isinstance(required_file_validation, dict) and str(required_file_validation.get("status") or "") == "passed":
        required_count = _as_positive_int(required_file_validation.get("required_file_count")) or 0
        if required_count > 0:
            done.append("required files matched")

    if isinstance(ci_status, dict) and str(ci_status.get("overall") or "") == "success":
        done.append("ci green")

    if isinstance(merge_readiness, dict) and str(merge_readiness.get("status") or "") == "ready-to-merge":
        done.append("merge ready")

    if isinstance(decomposition, dict):
        progress = decomposition.get("progress") if isinstance(decomposition.get("progress"), dict) else {}
        completed = _as_positive_int(progress.get("completed")) or 0
        total = _as_positive_int(progress.get("total")) or 0
        if total > 0:
            done.append(f"children {completed}/{total} done")

    return done


def _summarize_current_status(
    state: dict | None,
    *,
    ci_status: dict | None,
    merge_readiness: dict | None,
) -> str:
    if not isinstance(state, dict):
        return "no orchestration state recorded yet"

    payload = _status_payload(state)
    status = str(state.get("status") or payload.get("status") or "unknown").strip() or "unknown"
    stage = str(payload.get("stage") or "").strip()

    if isinstance(ci_status, dict) and status == "waiting-for-ci":
        pending = ci_status.get("pending_checks") if isinstance(ci_status.get("pending_checks"), list) else []
        failing = ci_status.get("failing_checks") if isinstance(ci_status.get("failing_checks"), list) else []
        if failing:
            return f"{status} at {stage or 'unknown stage'}; {format_failing_ci_checks_summary(failing)}"
        if pending:
            return f"{status} at {stage or 'unknown stage'}; waiting on {len(pending)} pending CI check(s)"
        if not ci_status.get("has_checks"):
            return f"{status} at {stage or 'unknown stage'}; waiting for CI checks to start"

    if isinstance(merge_readiness, dict) and status in {"ready-to-merge", "blocked"}:
        merge_readiness_state = _as_optional_string(merge_readiness.get("merge_readiness_state"))
        merge_state = str(merge_readiness.get("merge_state_status") or "").strip()
        verification = (
            merge_readiness.get("merge_result_verification")
            if isinstance(merge_readiness.get("merge_result_verification"), dict)
            else None
        )
        verification_status = str(verification.get("status") or "").strip() if verification else ""
        verification_text = (
            f"; merge-result verification {verification_status}"
            if verification_status in {"passed", "skipped", "dry-run"}
            else ""
        )
        detail_parts: list[str] = []
        if merge_readiness_state:
            detail_parts.append(f"merge readiness {merge_readiness_state}")
        if merge_state:
            detail_parts.append(f"merge state {merge_state}")
        if detail_parts:
            return f"{status} at {stage or 'unknown stage'}; {'; '.join(detail_parts)}{verification_text}"

    if stage:
        return f"{status} at {stage}"
    return status


def _collect_status_blockers(
    state: dict | None,
    *,
    required_file_validation: dict | None,
    ci_status: dict | None,
    merge_readiness: dict | None,
    decomposition: dict | None,
) -> list[str]:
    blockers: list[str] = []
    payload = _status_payload(state)
    state_error = _as_optional_string(payload.get("error"))
    if state_error:
        blockers.append(state_error)

    if isinstance(required_file_validation, dict) and str(required_file_validation.get("status") or "") == "blocked":
        missing_files = required_file_validation.get("missing_files")
        missing_summary = ", ".join(sorted(str(path) for path in missing_files or [] if str(path).strip()))
        if missing_summary:
            blockers.append(f"missing required files: {missing_summary}")

    if isinstance(ci_status, dict):
        failing_checks = ci_status.get("failing_checks") if isinstance(ci_status.get("failing_checks"), list) else []
        if failing_checks:
            blockers.append(format_failing_ci_checks_summary(failing_checks))

    if isinstance(merge_readiness, dict):
        readiness_status = str(merge_readiness.get("status") or "")
        readiness_error = _as_optional_string(merge_readiness.get("error"))
        if readiness_error and readiness_status in {"blocked", "waiting-for-author"}:
            blockers.append(readiness_error)

    if isinstance(decomposition, dict):
        decomposition_blockers = decomposition.get("blockers") if isinstance(decomposition.get("blockers"), list) else []
        blockers_text = _safe_join_sorted(decomposition_blockers)
        if blockers_text:
            blockers.append(f"decomposition blockers: {blockers_text}")

    deduped: list[str] = []
    seen: set[str] = set()
    for blocker in blockers:
        text = blocker.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def format_orchestration_status_summary(snapshot: dict) -> str:
    target_type = str(snapshot.get("target_type") or "unknown")
    target_number = snapshot.get("target_number")
    latest_state = snapshot.get("latest_state") if isinstance(snapshot.get("latest_state"), dict) else None
    payload = _status_payload(latest_state)
    state_source = _as_optional_string(latest_state.get("source") if isinstance(latest_state, dict) else None)
    issue_number = snapshot.get("issue_number")
    pr_number = snapshot.get("pr_number")
    branch = _as_optional_string(snapshot.get("branch"))
    base_branch = _as_optional_string(snapshot.get("base_branch"))
    linked_issue_numbers = snapshot.get("linked_issue_numbers") if isinstance(snapshot.get("linked_issue_numbers"), list) else []
    source_comment = _as_optional_string(snapshot.get("source_comment"))
    ci_status = snapshot.get("ci_status") if isinstance(snapshot.get("ci_status"), dict) else None
    merge_readiness = snapshot.get("merge_readiness") if isinstance(snapshot.get("merge_readiness"), dict) else None
    required_file_validation = (
        snapshot.get("required_file_validation")
        if isinstance(snapshot.get("required_file_validation"), dict)
        else None
    )
    decomposition = snapshot.get("decomposition") if isinstance(snapshot.get("decomposition"), dict) else None

    status = str(snapshot.get("latest_status") or payload.get("status") or "new").strip() or "new"
    done_items = _collect_status_done_items(
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        required_file_validation=required_file_validation,
        ci_status=ci_status,
        merge_readiness=merge_readiness,
        decomposition=decomposition,
    )
    blockers = _collect_status_blockers(
        latest_state,
        required_file_validation=required_file_validation,
        ci_status=ci_status,
        merge_readiness=merge_readiness,
        decomposition=decomposition,
    )
    next_action = _as_optional_string(payload.get("next_action")) or _as_optional_string(snapshot.get("next_action")) or "inspect tracker comments"
    current = _summarize_current_status(latest_state, ci_status=ci_status, merge_readiness=merge_readiness)

    if target_type == "issue":
        target_label = format_issue_ref(target_number)
    elif target_type == "pr":
        target_label = f"#{target_number}"
    else:
        target_label = str(target_number or "unknown")

    lines = [
        f"Target: {target_type} {target_label}",
        f"Latest state: {status}",
        f"Done: {', '.join(done_items) if done_items else 'none yet'}",
        f"Current: {current}",
        f"Next: {_humanize_status_token(next_action)}",
        f"Blockers: {'; '.join(blockers) if blockers else 'none'}",
    ]

    if is_trackable_issue_number(issue_number):
        lines.append(f"Issue: {format_issue_ref(issue_number)}")
    if isinstance(pr_number, int) and pr_number > 0:
        lines.append(f"PR: #{pr_number}")
    if linked_issue_numbers:
        lines.append(
            "Linked issues: " + ", ".join(format_issue_ref(number) for number in linked_issue_numbers if is_trackable_issue_number(number))
        )
    if branch:
        branch_line = f"Branch: {branch}"
        if base_branch:
            branch_line += f" (base {base_branch})"
        lines.append(branch_line)
    verification = (
        merge_readiness.get("merge_result_verification")
        if isinstance(merge_readiness, dict) and isinstance(merge_readiness.get("merge_result_verification"), dict)
        else None
    )
    verification_text = ""
    if verification is not None:
        verification_status = str(verification.get("status") or "unknown")
        verification_summary = _as_optional_string(verification.get("summary")) or verification_status
        verification_text = f"; merge-result verification={verification_summary}"

    merge_readiness_text = ""
    if isinstance(merge_readiness, dict):
        merge_readiness_state = _as_optional_string(merge_readiness.get("merge_readiness_state"))
        if merge_readiness_state:
            merge_readiness_text = f"merge={merge_readiness_state}, "

    if isinstance(ci_status, dict):
        ci_overall = str(ci_status.get("overall") or "unknown").strip() or "unknown"
        pending = ci_status.get("pending_checks") if isinstance(ci_status.get("pending_checks"), list) else []
        failing = ci_status.get("failing_checks") if isinstance(ci_status.get("failing_checks"), list) else []
        lines.append(
            f"PR readiness: {merge_readiness_text}ci={ci_overall}, pending={len(pending)}, failing={len(failing)}{verification_text}"
        )
    elif isinstance(merge_readiness, dict):
        merge_readiness_state = _as_optional_string(merge_readiness.get("merge_readiness_state"))
        readiness_line = "PR readiness: "
        if merge_readiness_state:
            readiness_line += f"merge={merge_readiness_state}, "
        readiness_line += f"status={str(merge_readiness.get('status') or 'unknown')}"
        if verification_text:
            readiness_line += verification_text
        lines.append(readiness_line)
    if state_source:
        lines.append(f"State source: {state_source}")
    if source_comment:
        lines.append(f"Source comment: {source_comment}")
    created_at = _as_optional_string(latest_state.get("created_at") if isinstance(latest_state, dict) else None)
    if created_at:
        lines.append(f"Updated: {created_at}")
    return "\n".join(lines)


def load_issue_status_snapshot(repo: str, issue: dict, merge_policy: dict) -> dict:
    issue_number = issue.get("number")
    issue_comments = current_tracker_provider().list_issue_comments(repo=repo, issue_id=issue_number)
    issue_state, issue_warnings = select_latest_parseable_orchestration_state(
        comments=issue_comments,
        source_label=format_issue_label_from_issue(issue),
    )

    linked_open_pr = current_codehost_provider().find_open_pr_for_issue(repo=repo, issue=issue)
    pull_request: dict | None = None
    pr_state: dict | None = None
    pr_warnings: list[str] = []
    if isinstance(linked_open_pr, dict):
        linked_pr_number = linked_open_pr.get("number")
        if type(linked_pr_number) is int:
            pull_request = current_codehost_provider().fetch_pull_request(repo=repo, number=linked_pr_number)
            pr_comments = current_codehost_provider().list_pr_comments(repo=repo, pr_number=linked_pr_number)
            pr_state, pr_warnings = select_latest_parseable_orchestration_state(
                comments=pr_comments,
                source_label=f"pr #{linked_pr_number}",
            )

    latest_state = merge_latest_recovered_state([issue_state, pr_state])
    payload = _status_payload(latest_state)
    issue_number_value = latest_state.get("payload", {}).get("issue") if isinstance(latest_state, dict) else None
    if not is_trackable_issue_number(issue_number_value):
        issue_number_value = issue_number

    pr_number = payload.get("pr") if type(payload.get("pr")) is int else None
    if pr_number is None and isinstance(pull_request, dict) and type(pull_request.get("number")) is int:
        pr_number = pull_request.get("number")

    branch = _as_optional_string(payload.get("branch")) or _as_optional_string(
        pull_request.get("headRefName") if isinstance(pull_request, dict) else None
    )
    base_branch = _as_optional_string(payload.get("base_branch")) or _as_optional_string(
        pull_request.get("baseRefName") if isinstance(pull_request, dict) else None
    )
    ci_status = (
        current_codehost_provider().read_pr_ci_status_for_pull_request(repo=repo, pull_request=pull_request)
        if isinstance(pull_request, dict)
        else None
    )
    required_file_validation = (
        validate_required_files_in_pr(pull_request=pull_request, linked_issues=[issue])
        if isinstance(pull_request, dict)
        else None
    )
    stored_merge_readiness = payload.get("merge_readiness") if isinstance(payload.get("merge_readiness"), dict) else None
    stored_merge_result_verification = None
    if isinstance(stored_merge_readiness, dict) and isinstance(
        stored_merge_readiness.get("merge_result_verification"), dict
    ):
        stored_merge_result_verification = stored_merge_readiness.get("merge_result_verification")
    merge_readiness = (
        evaluate_pr_merge_readiness(
            pull_request=pull_request,
            merge_policy=merge_policy,
            merge_result_verification=stored_merge_result_verification,
        )
        if isinstance(pull_request, dict)
        else None
    )
    decomposition = build_decomposition_rollup_from_recovered_state(
        recovered_state=latest_state,
        parent_issue=issue_number if type(issue_number) is int else None,
    )

    return {
        "target_type": "issue",
        "target_number": issue_number,
        "issue_number": issue_number_value,
        "pr_number": pr_number,
        "latest_state": latest_state,
        "latest_status": str(latest_state.get("status") or "new") if isinstance(latest_state, dict) else "new",
        "next_action": payload.get("next_action"),
        "branch": branch,
        "base_branch": base_branch,
        "source_comment": _as_optional_string(latest_state.get("url") if isinstance(latest_state, dict) else None),
        "ci_status": ci_status,
        "required_file_validation": required_file_validation,
        "merge_readiness": merge_readiness,
        "decomposition": decomposition,
        "warnings": issue_warnings + pr_warnings,
    }


def load_pr_status_snapshot(repo: str, pr_number: int, merge_policy: dict) -> dict:
    pull_request = current_codehost_provider().fetch_pull_request(repo=repo, number=pr_number)
    pr_comments = current_codehost_provider().list_pr_comments(repo=repo, pr_number=pr_number)
    pr_state, warnings = select_latest_parseable_orchestration_state(
        comments=pr_comments,
        source_label=f"pr #{pr_number}",
    )
    payload = _status_payload(pr_state)
    linked_issues = current_codehost_provider().load_pr_linked_issue_context(repo=repo, pull_request=pull_request)
    issue_numbers = [issue.get("number") for issue in linked_issues if is_trackable_issue_number(issue.get("number"))]
    issue_number = issue_numbers[0] if issue_numbers else payload.get("issue")
    ci_status = current_codehost_provider().read_pr_ci_status_for_pull_request(repo=repo, pull_request=pull_request)
    required_file_validation = validate_required_files_in_pr(pull_request=pull_request, linked_issues=linked_issues)
    stored_merge_readiness = payload.get("merge_readiness") if isinstance(payload.get("merge_readiness"), dict) else None
    stored_merge_result_verification = None
    if isinstance(stored_merge_readiness, dict) and isinstance(
        stored_merge_readiness.get("merge_result_verification"), dict
    ):
        stored_merge_result_verification = stored_merge_readiness.get("merge_result_verification")
    merge_readiness = evaluate_pr_merge_readiness(
        pull_request=pull_request,
        merge_policy=merge_policy,
        merge_result_verification=stored_merge_result_verification,
    )
    decomposition = build_decomposition_rollup_from_recovered_state(
        recovered_state=pr_state,
        parent_issue=issue_number if type(issue_number) is int else None,
    )

    return {
        "target_type": "pr",
        "target_number": pr_number,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "linked_issue_numbers": issue_numbers,
        "latest_state": pr_state,
        "latest_status": str(pr_state.get("status") or "new") if isinstance(pr_state, dict) else "new",
        "next_action": payload.get("next_action"),
        "branch": _as_optional_string(payload.get("branch")) or _as_optional_string(pull_request.get("headRefName")),
        "base_branch": _as_optional_string(payload.get("base_branch")) or _as_optional_string(pull_request.get("baseRefName")),
        "source_comment": _as_optional_string(pr_state.get("url") if isinstance(pr_state, dict) else None),
        "ci_status": ci_status,
        "required_file_validation": required_file_validation,
        "merge_readiness": merge_readiness,
        "decomposition": decomposition,
        "warnings": warnings,
    }


def run_status_command(*, args: argparse.Namespace, repo: str, merge_policy: dict) -> int:
    issue_number_arg = getattr(args, "issue", None)
    pr_number_arg = getattr(args, "pr", None)
    from_review_comments = bool(getattr(args, "from_review_comments", False))
    autonomous_session_file = _as_optional_string(getattr(args, "autonomous_session_file", None))
    if issue_number_arg is not None and pr_number_arg is not None:
        raise RuntimeError("Use either --issue or --pr with --status, not both.")
    if pr_number_arg is not None and from_review_comments:
        raise RuntimeError("--status does not use --from-review-comments.")
    if issue_number_arg is None and pr_number_arg is None:
        if not autonomous_session_file:
            raise RuntimeError("--status requires --issue <number>, --pr <number>, or --autonomous-session-file <path>.")
        print(format_autonomous_session_status_summary(load_autonomous_session_state(autonomous_session_file)))
        return 0

    if issue_number_arg is not None:
        issue = current_tracker_provider().get_issue(repo=repo, issue_id=issue_number_arg)
        snapshot = load_issue_status_snapshot(repo=repo, issue=issue, merge_policy=merge_policy)
    else:
        snapshot = load_pr_status_snapshot(repo=repo, pr_number=pr_number_arg, merge_policy=merge_policy)

    for warning in snapshot.get("warnings") or []:
        print(f"Warning: {warning}", file=sys.stderr)
    print(format_orchestration_status_summary(snapshot))
    return 0


def _comment_author_login(comment: dict) -> str:
    user = comment.get("user") if isinstance(comment, dict) else None
    if isinstance(user, dict):
        login = str(user.get("login") or "").strip().lower()
        if login:
            return login

    author = comment.get("author") if isinstance(comment, dict) else None
    if isinstance(author, dict):
        login = str(author.get("login") or "").strip().lower()
        if login:
            return login
    if isinstance(author, str):
        return author.strip().lower()
    return ""


def _is_orchestration_machine_comment(body: str) -> bool:
    return any(
        marker in body
        for marker in (
            ORCHESTRATION_STATE_MARKER,
            DECOMPOSITION_PLAN_MARKER,
            AGENT_FAILURE_REPORT_MARKER,
            SCOPE_DECISION_MARKER,
        )
    )


def find_waiting_for_author_answer(
    comments: list[dict],
    recovered_state: dict | None,
    author_login: str | None = None,
) -> dict | None:
    if not isinstance(recovered_state, dict):
        return None
    if str(recovered_state.get("status") or "") != "waiting-for-author":
        return None

    waiting_created_at = str(recovered_state.get("created_at") or "")
    waiting_comment_id = recovered_state.get("comment_id")
    normalized_author = str(author_login or "").strip().lower()

    latest_answer: dict | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").strip()
        if not body or _is_orchestration_machine_comment(body):
            continue

        created_at = str(comment.get("created_at") or "")
        comment_id = comment.get("id")
        if waiting_created_at and created_at and created_at < waiting_created_at:
            continue
        if waiting_comment_id is not None and comment_id is not None:
            try:
                if int(comment_id) <= int(waiting_comment_id):
                    continue
            except (TypeError, ValueError):
                pass

        comment_author = _comment_author_login(comment)
        if normalized_author and comment_author and comment_author != normalized_author:
            continue

        latest_answer = {
            "body": body,
            "created_at": created_at,
            "url": str(comment.get("html_url") or "").strip(),
            "author": comment_author or normalized_author or "unknown",
            "comment_id": comment_id,
        }

    return latest_answer


def build_clarification_context_note(state: dict, answer: dict | None = None) -> str:
    payload = state.get("payload") if isinstance(state, dict) else None
    if not isinstance(payload, dict):
        payload = {}

    question = _as_optional_string(payload.get("question")) or _as_optional_string(payload.get("reason")) or "Clarification requested"
    reason = _as_optional_string(payload.get("reason"))
    lines = [
        "Recovered clarification context:",
        f"- {format_recovered_state_context(state)}",
    ]
    if reason and reason != question:
        lines.append(f"- why clarification was needed: {reason}")
    lines.append(f"- question asked to the task author: {question}")
    if isinstance(answer, dict):
        answer_text = _as_optional_string(answer.get("body")) or ""
        answer_author = _as_optional_string(answer.get("author")) or "unknown"
        answer_url = _as_optional_string(answer.get("url"))
        answer_context = f" by {answer_author}"
        if answer_url:
            answer_context += f" ({answer_url})"
        lines.append(f"- answer received{answer_context}: {answer_text}")
        lines.append("- use this answer as authoritative context for the resumed run")
    else:
        lines.append("- no answer has been posted yet; do not guess beyond the current requirements")
    return "\n".join(lines)


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

    latest_review_by_author = latest_reviews_by_author(reviews)
    for review in reviews:
        if isinstance(review, dict):
            stats["reviews_total"] += 1
    stats["reviews_superseded"] = max(0, stats["reviews_total"] - len(latest_review_by_author))

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


def fetch_actionable_pr_review_feedback(
    repo: str,
    pr_number: int,
    pull_request: dict | None = None,
) -> tuple[dict, list[dict], dict[str, int]]:
    codehost_provider = current_codehost_provider()
    current_pull_request = pull_request or codehost_provider.fetch_pull_request(repo=repo, number=pr_number)
    threads = codehost_provider.fetch_pr_review_threads(repo=repo, number=pr_number)
    conversation_comments = codehost_provider.fetch_pr_conversation_comments(repo=repo, pr_number=pr_number)
    reviews = current_pull_request.get("reviews")
    if not isinstance(reviews, list):
        reviews = []

    pr_author_payload = current_pull_request.get("author")
    pr_author_login = ""
    if isinstance(pr_author_payload, dict):
        pr_author_login = str(pr_author_payload.get("login") or "")

    review_items, review_stats = normalize_review_items(
        threads=threads,
        reviews=reviews,
        conversation_comments=conversation_comments,
        pr_author_login=pr_author_login,
    )
    return current_pull_request, review_items, review_stats


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
            issue = current_tracker_provider().get_issue(repo=repo, issue_id=number)
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


def pr_links_issue(pr: dict, issue: dict) -> bool:
    return _github_lifecycle.pr_links_issue(
        pr,
        issue,
        issue_tracker=issue_tracker,
        tracker_github=TRACKER_GITHUB,
        format_issue_ref_from_issue=format_issue_ref_from_issue,
    )


def find_open_pr_for_issue(repo: str, issue: dict) -> dict | None:
    return _github_lifecycle.find_open_pr_for_issue(
        repo,
        issue,
        run_capture=run_capture,
        issue_tracker=issue_tracker,
        tracker_github=TRACKER_GITHUB,
        format_issue_ref_from_issue=format_issue_ref_from_issue,
    )


def fetch_pr_review_comments(repo: str, pr_number: int) -> list[dict]:
    return _github_lifecycle.fetch_pr_review_comments(repo, pr_number, run_capture=run_capture)


def fetch_issue_comments(repo: str, issue_number: int) -> list[dict]:
    return _github_lifecycle.fetch_issue_comments(repo, issue_number, run_capture=run_capture)


def fetch_commit_check_runs(repo: str, head_sha: str) -> list[dict]:
    output = run_capture(
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{head_sha}/check-runs",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            "-f",
            "per_page=100",
        ]
    )
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected response from gh api while fetching commit check runs")

    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise RuntimeError("Unexpected check_runs payload while fetching commit check runs")
    return check_runs


def fetch_commit_status_contexts(repo: str, head_sha: str) -> list[dict]:
    output = run_capture(
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{head_sha}/status",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
        ]
    )
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected response from gh api while fetching commit status")

    statuses = payload.get("statuses")
    if not isinstance(statuses, list):
        raise RuntimeError("Unexpected statuses payload while fetching commit status")
    return statuses


def read_pr_ci_status_for_head_sha(repo: str, head_sha: str) -> dict:
    normalized_checks: list[dict] = []
    check_runs = fetch_commit_check_runs(repo=repo, head_sha=head_sha)
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            continue
        status = str(check_run.get("status") or "").strip().lower()
        conclusion = str(check_run.get("conclusion") or "").strip().lower()
        name = str(check_run.get("name") or "check-run").strip() or "check-run"
        url = str(
            check_run.get("details_url") or check_run.get("html_url") or ""
        ).strip()

        if status in CI_PENDING_CHECK_RUN_STATUSES or status != "completed":
            normalized_state = "pending"
        elif conclusion in CI_FAILURE_CHECK_RUN_CONCLUSIONS:
            normalized_state = "failure"
        elif conclusion in CI_SUCCESS_CHECK_RUN_CONCLUSIONS:
            normalized_state = "success"
        else:
            normalized_state = "failure"

        normalized_checks.append(
            {
                "source": "check-run",
                "id": check_run.get("id"),
                "name": name,
                "url": url,
                "html_url": str(check_run.get("html_url") or "").strip(),
                "status": status,
                "conclusion": conclusion or None,
                "state": normalized_state,
            }
        )

    status_contexts = fetch_commit_status_contexts(repo=repo, head_sha=head_sha)
    for context in status_contexts:
        if not isinstance(context, dict):
            continue
        raw_state = str(context.get("state") or "").strip().lower()
        name = str(context.get("context") or "status-context").strip() or "status-context"
        url = str(context.get("target_url") or "").strip()

        if raw_state == "pending":
            normalized_state = "pending"
        elif raw_state in CI_FAILURE_COMMIT_STATES:
            normalized_state = "failure"
        elif raw_state == "success":
            normalized_state = "success"
        else:
            normalized_state = "pending"

        normalized_checks.append(
            {
                "source": "status-context",
                "name": name,
                "url": url,
                "status": raw_state,
                "conclusion": None,
                "state": normalized_state,
            }
        )

    pending_checks = [check for check in normalized_checks if check.get("state") == "pending"]
    failing_checks = [check for check in normalized_checks if check.get("state") == "failure"]

    if not normalized_checks:
        overall = "success"
    elif failing_checks:
        overall = "failure"
    elif pending_checks:
        overall = "pending"
    else:
        overall = "success"

    return {
        "head_sha": head_sha,
        "overall": overall,
        "has_checks": bool(normalized_checks),
        "checks": normalized_checks,
        "pending_checks": pending_checks,
        "failing_checks": failing_checks,
    }


def read_pr_ci_status_for_pull_request(repo: str, pull_request: dict) -> dict:
    head_sha = str(pull_request.get("headRefOid") or "").strip()
    if not head_sha:
        pr_number = pull_request.get("number")
        raise RuntimeError(
            f"Unable to read CI status for PR #{pr_number}: missing headRefOid in PR payload"
        )
    return read_pr_ci_status_for_head_sha(repo=repo, head_sha=head_sha)


def format_failing_ci_checks_summary(failing_checks: list[dict], max_items: int = 5) -> str:
    if not failing_checks:
        return "No failing CI checks reported"

    rendered_items: list[str] = []
    for check in failing_checks[:max_items]:
        name = str(check.get("name") or "unknown-check")
        url = str(check.get("url") or "").strip()
        rendered_items.append(f"{name} ({url})" if url else name)

    remaining = len(failing_checks) - len(rendered_items)
    if remaining > 0:
        rendered_items.append(f"and {remaining} more")

    return "CI failing checks: " + "; ".join(rendered_items)


def wait_for_pr_ci_status(
    repo: str,
    pull_request: dict,
    poll_interval_seconds: int = CI_WAIT_POLL_INTERVAL_SECONDS,
    max_polls: int = CI_WAIT_MAX_POLLS,
) -> dict:
    latest = current_codehost_provider().read_pr_ci_status_for_pull_request(
        repo=repo,
        pull_request=pull_request,
    )
    polls = 1
    while str(latest.get("overall") or "") == "pending" and polls < max_polls:
        pending_checks = latest.get("pending_checks")
        pending_count = len(pending_checks) if isinstance(pending_checks, list) else 0
        pr_number = pull_request.get("number")
        print(
            f"CI checks still pending for PR #{pr_number} "
            f"({pending_count} pending); waiting {poll_interval_seconds}s before retry {polls + 1}/{max_polls}."
        )
        time.sleep(poll_interval_seconds)
        latest = current_codehost_provider().read_pr_ci_status_for_pull_request(
            repo=repo,
            pull_request=pull_request,
        )
        polls += 1
    latest = dict(latest)
    latest["poll_count"] = polls
    latest["timed_out_waiting"] = str(latest.get("overall") or "") == "pending" and polls >= max_polls
    return latest


def _extract_github_actions_run_id(url: str) -> int | None:
    match = re.search(r"/runs/(\d+)", str(url or ""))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def fetch_failed_ci_log(repo: str, check: dict) -> str | None:
    run_id = _extract_github_actions_run_id(
        str(check.get("url") or "") or str(check.get("html_url") or "")
    )
    if run_id is None:
        return None
    try:
        output = run_capture(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
                "--log-failed",
            ]
        )
    except Exception:
        return None
    normalized = str(output or "").strip()
    return normalized or None


def classify_ci_failure(check: dict, log_text: str | None) -> dict:
    name = str(check.get("name") or "unknown-check")
    conclusion = str(check.get("conclusion") or check.get("status") or "").strip().lower()
    log_excerpt = str(log_text or "")

    if conclusion in {"cancelled", "startup_failure", "stale"}:
        return {
            "check": name,
            "kind": "transient",
            "reason": f"check concluded with {conclusion}",
        }

    for pattern, reason in CI_TRANSIENT_LOG_PATTERNS:
        if pattern.search(log_excerpt):
            return {
                "check": name,
                "kind": "transient",
                "reason": reason,
            }

    if conclusion == "timed_out" and not log_excerpt:
        return {
            "check": name,
            "kind": "transient",
            "reason": "check timed out without captured failing log evidence",
        }

    return {
        "check": name,
        "kind": "real",
        "reason": "failure log points to a repository or test issue",
    }


def collect_failing_ci_diagnostics(repo: str, failing_checks: list[dict]) -> dict:
    diagnostics: list[dict] = []
    classifications: list[str] = []
    for check in failing_checks[:CI_LOG_MAX_CHECKS]:
        log_text = fetch_failed_ci_log(repo=repo, check=check)
        if log_text and len(log_text) > CI_LOG_EXCERPT_MAX_CHARS:
            log_excerpt = log_text[-CI_LOG_EXCERPT_MAX_CHARS :]
        else:
            log_excerpt = log_text
        classification = classify_ci_failure(check=check, log_text=log_excerpt)
        classifications.append(str(classification.get("kind") or "real"))
        diagnostics.append(
            {
                "name": str(check.get("name") or "unknown-check"),
                "url": str(check.get("url") or "").strip(),
                "classification": classification,
                "log_excerpt": log_excerpt,
            }
        )

    if not diagnostics:
        overall = "real"
    elif all(kind == "transient" for kind in classifications):
        overall = "transient"
    else:
        overall = "real"

    return {
        "overall_classification": overall,
        "failing_checks": diagnostics,
    }


def format_ci_diagnostics_summary(ci_diagnostics: dict) -> str:
    failing_checks = ci_diagnostics.get("failing_checks")
    if not isinstance(failing_checks, list) or not failing_checks:
        return "No CI diagnostics available"

    summary_parts: list[str] = []
    for item in failing_checks:
        if not isinstance(item, dict):
            continue
        classification = item.get("classification") if isinstance(item.get("classification"), dict) else {}
        name = str(item.get("name") or "unknown-check")
        kind = str(classification.get("kind") or "real")
        reason = str(classification.get("reason") or "unspecified reason")
        summary_parts.append(f"{name}: {kind} ({reason})")
    return "; ".join(summary_parts) if summary_parts else "No CI diagnostics available"


def build_ci_failure_prompt(
    pull_request: dict,
    failing_checks: list[dict],
    ci_diagnostics: dict,
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
        issue_context_lines.append(
            (
                f"- Issue {format_issue_ref_from_issue(issue)}: {str(issue.get('title') or '')}\n"
                f"  URL: {str(issue.get('url') or '')}\n"
                f"  Body: {str(issue.get('body') or '').strip()}"
            ).strip()
        )
    if not issue_context_lines:
        issue_context_lines.append("- No linked issue context found.")

    failing_lines: list[str] = []
    for check in failing_checks:
        if not isinstance(check, dict):
            continue
        check_name = str(check.get("name") or "unknown-check")
        check_url = str(check.get("url") or "").strip()
        conclusion = str(check.get("conclusion") or check.get("status") or "unknown")
        failing_lines.append(
            f"- {check_name} [{conclusion}]" + (f"\n  URL: {check_url}" if check_url else "")
        )

    diagnostics_lines: list[str] = []
    for item in ci_diagnostics.get("failing_checks", []):
        if not isinstance(item, dict):
            continue
        classification = item.get("classification") if isinstance(item.get("classification"), dict) else {}
        diagnostics_lines.append(
            (
                f"Check: {str(item.get('name') or 'unknown-check')}\n"
                f"Classification: {str(classification.get('kind') or 'real')}\n"
                f"Reason: {str(classification.get('reason') or 'unspecified reason')}\n"
                "Failed log excerpt:\n"
                f"{str(item.get('log_excerpt') or 'No failed log excerpt available.') }"
            ).strip()
        )

    _issue_context = "\n".join(issue_context_lines)
    _failing = "\n".join(failing_lines) if failing_lines else "- No failing checks supplied."
    _diagnostics = "\n\n".join(diagnostics_lines) if diagnostics_lines else "No CI diagnostics available."
    return (
        "You are working on an existing GitHub pull request CI failure cycle in the current git branch.\n"
        "Diagnose the failing CI checks using the provided logs and implement the safest repository fix in files.\n"
        "Do not run git commands; git actions are handled by orchestration script.\n\n"
        f"Pull Request: #{pr_number} - {pr_title}\n"
        f"PR URL: {pr_url}\n\n"
        "PR description:\n"
        f"{pr_body}\n\n"
        "Linked issue context:\n"
        f"{_issue_context}\n\n"
        "Failing CI checks:\n"
        f"{_failing}\n\n"
        "CI diagnostics and failing logs:\n\n"
        f"{_diagnostics}\n"
    )


def orchestration_attempt_from_state(state: dict | None) -> int:
    if not isinstance(state, dict):
        return 1
    attempt = state.get("attempt")
    return attempt if type(attempt) is int and attempt > 0 else 1


def fetch_pr_conversation_comments(repo: str, pr_number: int) -> list[dict]:
    return _github_lifecycle.fetch_pr_conversation_comments(
        repo,
        pr_number,
        fetch_issue_comments=fetch_issue_comments,
    )


def current_branch() -> str:
    return run_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()


def current_repo_root() -> str:
    return os.path.abspath(run_capture(["git", "rev-parse", "--show-toplevel"]).strip())


def assert_expected_git_context(
    *,
    expected_branch: str | None,
    expected_repo_root: str | None,
    operation: str,
) -> None:
    normalized_branch = str(expected_branch or "").strip()
    normalized_repo_root = os.path.abspath(str(expected_repo_root or "").strip()) if expected_repo_root else ""
    if not normalized_branch and not normalized_repo_root:
        return

    actual_branch = current_branch()
    actual_repo_root = current_repo_root()
    if normalized_branch and actual_branch != normalized_branch:
        raise BranchContextMismatchError(
            operation=operation,
            expected_branch=normalized_branch,
            actual_branch=actual_branch,
            expected_repo_root=normalized_repo_root or actual_repo_root,
            actual_repo_root=actual_repo_root,
        )
    if normalized_repo_root and actual_repo_root != normalized_repo_root:
        raise BranchContextMismatchError(
            operation=operation,
            expected_branch=normalized_branch or actual_branch,
            actual_branch=actual_branch,
            expected_repo_root=normalized_repo_root,
            actual_repo_root=actual_repo_root,
        )


def ensure_clean_worktree() -> None:
    status = run_capture(["git", "status", "--porcelain"]).strip()
    if status:
        raise RuntimeError("Git working tree must be clean before running this script.")


def current_branch_stack_warnings() -> list[str]:
    warnings: list[str] = []

    if has_changes():
        warnings.append(
            "current branch has uncommitted changes; stacked mode expects a clean branch"
        )

    try:
        upstream_ref = run_capture(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
        ).strip()
    except RuntimeError:
        warnings.append(
            "current branch has no upstream tracking branch; stacked base may not be visible remotely"
        )
        return warnings

    if not upstream_ref:
        warnings.append(
            "current branch has empty upstream tracking ref; stacked base may not be visible remotely"
        )
        return warnings

    ahead_count_raw = run_capture(["git", "rev-list", "--count", f"{upstream_ref}..HEAD"]).strip()
    try:
        ahead_count = int(ahead_count_raw)
    except ValueError:
        ahead_count = 0

    if ahead_count > 0:
        warnings.append(
            f"current branch is ahead of {upstream_ref} by {ahead_count} commit(s); push it before stacked run"
        )

    return warnings


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:40] or "issue"


def branch_name_for_issue(issue: dict, prefix: str) -> str:
    return _github_lifecycle.branch_name_for_issue(
        issue,
        prefix,
        issue_tracker=issue_tracker,
        tracker_jira=TRACKER_JIRA,
        slugify=slugify,
    )


def has_changes() -> bool:
    return bool(run_capture(["git", "status", "--porcelain"]).strip())


def residual_untracked_files_after_baseline(
    pre_run_untracked_files: set[str] | None,
) -> list[str]:
    if pre_run_untracked_files is None:
        return []

    post_run_untracked_files = list_untracked_files()
    return sorted(post_run_untracked_files - set(pre_run_untracked_files))


def list_untracked_files() -> set[str]:
    status_output = run_capture(["git", "ls-files", "--others", "--exclude-standard"]).strip()
    if not status_output:
        return set()
    return {line for line in status_output.splitlines() if line.strip()}


def stage_worktree_changes(pre_run_untracked_files: set[str] | None = None) -> None:
    run_command(["git", "add", "-u"])

    if pre_run_untracked_files is None:
        return

    new_untracked_files = residual_untracked_files_after_baseline(pre_run_untracked_files)
    if new_untracked_files:
        run_command(["git", "add", "--", *new_untracked_files])


def sanitize_branch_for_path(branch_name: str) -> str:
    return _github_lifecycle.sanitize_branch_for_path(branch_name)


def checkout_pr_target_branch(branch_name: str, dry_run: bool) -> None:
    local_exists = local_branch_exists(branch_name)
    remote_exists = remote_branch_exists(branch_name)

    if dry_run:
        if local_exists:
            print(f"[dry-run] Would checkout local PR branch '{branch_name}'")
            return
        if remote_exists:
            print(
                f"[dry-run] Would create and checkout tracking branch '{branch_name}' "
                f"from 'origin/{branch_name}'"
            )
            return
        raise RuntimeError(
            f"Target PR branch '{branch_name}' not found locally or on origin "
            "(based on current refs)."
        )

    if local_exists:
        run_command(["git", "checkout", branch_name])
        return

    run_command(["git", "fetch", "origin", branch_name])
    run_command(["git", "checkout", "-b", branch_name, "--track", f"origin/{branch_name}"])


def create_isolated_worktree_for_branch(branch_name: str, dry_run: bool) -> str | None:
    safe_branch = sanitize_branch_for_path(branch_name)
    preview_path = os.path.join(tempfile.gettempdir(), f"opencode-pr-{safe_branch}-<random>")
    if dry_run:
        print(
            f"[dry-run] Would create isolated worktree for '{branch_name}' "
            f"at '{preview_path}'"
        )
        return None

    worktree_dir = tempfile.mkdtemp(prefix=f"opencode-pr-{safe_branch}-")
    try:
        local_exists = local_branch_exists(branch_name)
        if local_exists:
            run_command(["git", "worktree", "add", worktree_dir, branch_name])
            return worktree_dir

        run_command(["git", "fetch", "origin", branch_name])
        run_command(["git", "worktree", "add", "-b", branch_name, worktree_dir, f"origin/{branch_name}"])
        run_command(["git", "-C", worktree_dir, "branch", "--set-upstream-to", f"origin/{branch_name}", branch_name])
        return worktree_dir
    except Exception:
        shutil.rmtree(worktree_dir, ignore_errors=True)
        raise


def remove_isolated_worktree(path: str) -> None:
    run_command(["git", "worktree", "remove", "--force", path])


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def short_error_text(error: str, max_len: int = 280) -> str:
    compact = " ".join(str(error).split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[: max_len - 3].rstrip()}..."


def generate_run_id() -> str:
    return f"{int(time.time() * 1000)}-{os.getpid()}"


def build_issue_failure_report_comment(
    issue_number: int,
    run_id: str,
    stage: str,
    error: str,
    branch: str | None,
    base_branch: str | None,
    runner: str,
    agent: str,
    model: str | None,
    residual_untracked_files: list[str] | None = None,
    next_action: str | None = None,
) -> str:
    payload = {
        "status": "failed",
        "issue": issue_number,
        "run_id": run_id,
        "stage": stage,
        "error": short_error_text(error),
        "branch": branch,
        "base_branch": base_branch,
        "runner": runner,
        "agent": agent,
        "model": model,
        "timestamp": utc_now_iso(),
    }
    if residual_untracked_files:
        payload["residual_untracked_files"] = sorted(residual_untracked_files)
        payload["residual_untracked_count"] = len(residual_untracked_files)
        payload["residual_validation_stage"] = stage

    next_actions = next_action or (
        "Next actions: rerun with --dry-run for preview, rerun with --force-reprocess "
        "to override skip guards, or take over manually from the branch above."
    )

    evidence = ""
    if payload.get("residual_untracked_files"):
        files = payload["residual_untracked_files"]
        file_lines = "\n".join(f"  - `{item}`" for item in files)
        evidence = (
            f"\nFailure evidence:\n"
            f"- stage: `{payload.get('residual_validation_stage') or 'unknown'}`\n"
            f"- residual untracked files:\n{file_lines}"
        )

    return (
        "Automation failure report\n\n"
        f"- status: `{payload['status']}`\n"
        f"- stage: `{payload['stage']}`\n"
        f"- error: `{payload['error']}`\n"
        f"- branch: `{payload['branch'] or 'n/a'}`\n"
        f"- base branch: `{payload['base_branch'] or 'n/a'}`\n"
        f"- runner: `{payload['runner']}`\n"
        f"- agent: `{payload['agent']}`\n"
        f"- model: `{payload['model'] or 'default'}`\n"
        f"- run id: `{payload['run_id']}`\n"
        f"- timestamp: `{payload['timestamp']}`\n\n"
        f"{evidence}\n"
        f"{next_actions}\n\n"
        f"{AGENT_FAILURE_REPORT_MARKER}\n"
        f"```json\n{json.dumps(payload, ensure_ascii=True, indent=2)}\n```"
    )


def ensure_agent_failure_label(repo: str, dry_run: bool) -> None:
    label_view_command = ["gh", "label", "view", AGENT_FAILURE_LABEL_NAME, "--repo", repo]
    if command_succeeds(label_view_command):
        return

    if dry_run:
        print(
            f"[dry-run] Would create missing label '{AGENT_FAILURE_LABEL_NAME}' "
            f"(color={AGENT_FAILURE_LABEL_COLOR}, description='{AGENT_FAILURE_LABEL_DESCRIPTION}')"
        )
        return

    create_command = [
        "gh",
        "label",
        "create",
        AGENT_FAILURE_LABEL_NAME,
        "--repo",
        repo,
        "--color",
        AGENT_FAILURE_LABEL_COLOR,
        "--description",
        AGENT_FAILURE_LABEL_DESCRIPTION,
    ]
    created, _stdout, stderr, _exit_code = run_check_command(create_command)
    if created:
        return

    if _label_already_exists_error(stderr) and command_succeeds(label_view_command):
        return

    raise RuntimeError(
        f"Failed to create missing failure label '{AGENT_FAILURE_LABEL_NAME}': {stderr or 'unknown error'}"
    )


def add_agent_failure_label_to_issue(repo: str, issue_number: int, dry_run: bool) -> None:
    tracker_provider = current_tracker_provider()
    if not tracker_provider.supports_issue_labels:
        return
    tracker_provider.add_issue_label(
        repo=repo,
        issue_id=issue_number,
        label_name=AGENT_FAILURE_LABEL_NAME,
        dry_run=dry_run,
    )


def issue_has_label(repo: str, issue_number: int, label_name: str) -> bool:
    return current_tracker_provider().issue_has_label(
        repo=repo,
        issue_id=issue_number,
        label_name=label_name,
    )


def remove_agent_failure_label_from_issue(repo: str, issue_number: int, dry_run: bool) -> None:
    try:
        tracker_provider = current_tracker_provider()
        if not tracker_provider.supports_issue_labels:
            return
        if dry_run:
            print(
                f"[dry-run] Would remove label '{AGENT_FAILURE_LABEL_NAME}' from issue "
                f"{format_issue_ref(issue_number, tracker=tracker_provider.name)} if present"
            )
            return
        if not tracker_provider.issue_has_label(
            repo=repo,
            issue_id=issue_number,
            label_name=AGENT_FAILURE_LABEL_NAME,
        ):
            return

        tracker_provider.remove_issue_label(
            repo=repo,
            issue_id=issue_number,
            label_name=AGENT_FAILURE_LABEL_NAME,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to remove label '{AGENT_FAILURE_LABEL_NAME}' from issue #{issue_number}: {exc}",
            file=sys.stderr,
        )


def safe_report_issue_automation_failure(
    repo: str,
    issue_number: int | str,
    run_id: str,
    stage: str,
    error: str,
    branch: str | None,
    base_branch: str | None,
    runner: str,
    agent: str,
    model: str | None,
    dry_run: bool,
    already_reported_issue_numbers: set[int],
    residual_untracked_files: list[str] | None = None,
    next_action: str | None = None,
) -> None:
    if issue_number in already_reported_issue_numbers:
        return

    try:
        body = build_issue_failure_report_comment(
            issue_number=issue_number,
            run_id=run_id,
            stage=stage,
            error=error,
            branch=branch,
            base_branch=base_branch,
            runner=runner,
            agent=agent,
            model=model,
            residual_untracked_files=residual_untracked_files,
            next_action=next_action,
        )
        if dry_run:
            print(
                f"[dry-run] Would post agent failure report comment to issue {format_issue_ref(issue_number, tracker=current_tracker_provider().name)}: "
                f"stage={stage} run_id={run_id}"
            )
        else:
            current_tracker_provider().post_issue_comment(
                repo=repo,
                issue_id=issue_number,
                body=body,
            )
        add_agent_failure_label_to_issue(
            repo=repo,
            issue_number=issue_number,
            dry_run=dry_run,
        )
        already_reported_issue_numbers.add(issue_number)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to report automation failure for issue #{issue_number}: {exc}",
            file=sys.stderr,
        )


def build_issue_scope_skip_comment(issue_number: int | str, reason: str, forced: bool) -> str:
    payload = {
        "status": "forced-in-scope" if forced else "out-of-scope",
        "issue": issue_number,
        "reason": short_error_text(reason),
        "forced": forced,
        "timestamp": utc_now_iso(),
    }

    if forced:
        headline = "Autonomous scope check: forced override applied"
        next_action = "Proceeding because --force-reprocess is set."
    else:
        headline = "Autonomous scope check: out of scope"
        next_action = (
            "No agent run started. Update issue labels/author scope rules or rerun with "
            "--force-reprocess to override explicitly."
        )

    return (
        f"{headline}\n\n"
        f"- decision: `{payload['status']}`\n"
        f"- reason: `{payload['reason']}`\n"
        f"- forced: `{'yes' if forced else 'no'}`\n"
        f"- timestamp: `{payload['timestamp']}`\n\n"
        f"{next_action}\n\n"
        f"{SCOPE_DECISION_MARKER}\n"
        f"```json\n{json.dumps(payload, ensure_ascii=True, indent=2)}\n```"
    )


DECOMPOSITION_KEYWORDS = {
    "epic",
    "roadmap",
    "architecture",
    "daemon",
    "multi-provider",
    "decomposition",
    "linked subtask",
    "subtask",
    "multiple pr",
    "multi-step",
    "large",
}

DECOMPOSITION_SOURCE_HEADINGS = {
    "scope",
    "implementation",
    "implementation plan",
    "work items",
    "tasks",
    "plan",
}

DECOMPOSITION_EXCLUDED_HEADINGS = {
    "acceptance criteria",
    "success criteria",
    "validation",
    "done when",
    "non-goals",
    "out of scope",
}

AUTO_DECOMPOSITION_HARD_TITLE_PREFIXES = (
    "epic:",
    "roadmap:",
)

CONCRETE_IMPLEMENTATION_TITLE_PREFIXES = (
    "fix",
    "add",
    "implement",
    "update",
    "refine",
    "improve",
    "roll out",
    "support",
    "allow",
    "track",
    "record",
)

CONCRETE_IMPLEMENTATION_HEADINGS = {
    "feature request",
    "proposed behavior",
    "proposed behaviour",
    "implementation notes",
    "acceptance criteria",
    "success criteria",
}


def _issue_body_lines(issue: dict) -> list[str]:
    body = str(issue.get("body") or "")
    return [line.strip() for line in body.splitlines() if line.strip()]


def _issue_headings(issue: dict) -> list[str]:
    headings: list[str] = []
    for line in _issue_body_lines(issue):
        heading = _normalize_heading(line)
        if heading is not None:
            headings.append(heading)
    return headings


def _normalize_heading(line: str) -> str | None:
    match = re.match(r"^#{1,6}\s+(.+)$", line.strip())
    if not match:
        return None
    heading = re.sub(r"[^a-z0-9]+", " ", match.group(1).strip().lower()).strip()
    return heading or None


def _issue_sectioned_bullets(issue: dict) -> list[tuple[str | None, str]]:
    bullets: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    for line in _issue_body_lines(issue):
        heading = _normalize_heading(line)
        if heading is not None:
            current_heading = heading
            continue
        normalized = line.strip()
        if normalized.startswith(("-", "*")):
            item = normalized[1:].strip()
            if item:
                bullets.append((current_heading, item))
    return bullets


def _issue_scope_bullets(issue: dict) -> list[str]:
    return [item for _, item in _issue_sectioned_bullets(issue)]


def _issue_decomposition_source_bullets(issue: dict) -> list[str]:
    sectioned_bullets = _issue_sectioned_bullets(issue)
    preferred = [
        item
        for heading, item in sectioned_bullets
        if heading in DECOMPOSITION_SOURCE_HEADINGS
    ]
    if preferred:
        return preferred
    return [
        item
        for heading, item in sectioned_bullets
        if heading not in DECOMPOSITION_EXCLUDED_HEADINGS
    ]


def _child_acceptance_criteria(title: str) -> list[str]:
    return [
        f"Required changes for '{title}' are implemented.",
        f"Relevant validation or follow-up checks for '{title}' are recorded.",
    ]


def _looks_like_concrete_implementation_issue(issue: dict) -> bool:
    title = str(issue.get("title") or "").strip().lower()
    if not any(title.startswith(prefix) for prefix in CONCRETE_IMPLEMENTATION_TITLE_PREFIXES):
        return False

    headings = set(_issue_headings(issue))
    body = str(issue.get("body") or "")
    return bool(headings & CONCRETE_IMPLEMENTATION_HEADINGS) or "`" in body


def assess_issue_decomposition_need(issue: dict) -> dict:
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    combined = f"{title}\n{body}".lower()
    bullets = _issue_scope_bullets(issue)
    decomposition_source_bullets = _issue_decomposition_source_bullets(issue)
    title_lower = title.strip().lower()
    hard_reasons: list[str] = []
    soft_hints: list[str] = []

    if title_lower.startswith(AUTO_DECOMPOSITION_HARD_TITLE_PREFIXES):
        if title_lower.startswith("epic:"):
            hard_reasons.append("explicit_epic_title")
        else:
            hard_reasons.append("explicit_roadmap_title")

    if len(body) >= 1200:
        soft_hints.append("long_body")
    if len(decomposition_source_bullets) >= 5:
        soft_hints.append("many_implementation_areas")

    matched_keywords = sorted(keyword for keyword in DECOMPOSITION_KEYWORDS if keyword in combined)

    acceptance_like = sum(
        1
        for line in _issue_body_lines(issue)
        if any(token in line.lower() for token in ["acceptance", "success criteria", "scope", "goal"])
    )
    if acceptance_like >= 3 and (hard_reasons or len(body) >= 900 or len(decomposition_source_bullets) >= 4):
        soft_hints.append("multiple_planning_sections")

    concrete_implementation = _looks_like_concrete_implementation_issue(issue)
    soft_hint_threshold = 3 if concrete_implementation else 2
    needs_decomposition = bool(hard_reasons) or len(soft_hints) >= soft_hint_threshold
    reasons = hard_reasons + (soft_hints if needs_decomposition else [])

    return {
        "needs_decomposition": needs_decomposition,
        "reasons": reasons,
        "hard_reasons": hard_reasons,
        "soft_hints": soft_hints,
        "concrete_implementation": concrete_implementation,
        "matched_keywords": matched_keywords,
        "body_length": len(body),
        "bullet_count": len(bullets),
        "implementation_area_count": len(decomposition_source_bullets),
    }


def should_issue_decompose(issue: dict, decompose_mode: str) -> tuple[bool, dict]:
    assessment = assess_issue_decomposition_need(issue)
    return decompose_mode == "always" or bool(assessment.get("needs_decomposition")), assessment


def should_check_existing_decomposition_plan(issue: dict, assessment: dict) -> bool:
    title = str(issue.get("title") or "").lower()
    body = str(issue.get("body") or "").lower()
    matched_keywords = assessment.get("matched_keywords")
    matched = matched_keywords if isinstance(matched_keywords, list) else []
    if "decomposition" in matched:
        return True
    if "decomposition" in title or "decomposition" in body:
        return True
    if "parent epic:" in body:
        return True
    return False


def build_decomposition_plan_payload(issue: dict, assessment: dict) -> dict:
    bullets = _issue_decomposition_source_bullets(issue)
    proposed_children: list[dict] = []
    source_items = bullets[:5]
    if not source_items:
        source_items = [
            "Clarify scope and acceptance criteria",
            "Implement the smallest safe slice",
            "Validate behavior and update tracker state",
        ]

    for index, item in enumerate(source_items, start=1):
        title = item.rstrip(".")
        proposed_children.append(
            {
                "title": title[:120],
                "order": index,
                "depends_on": [] if index == 1 else [index - 1],
                "acceptance": _child_acceptance_criteria(title),
            }
        )

    return {
        "status": "proposed",
        "parent_issue": issue.get("number"),
        "reason": assessment.get("reasons", []),
        "matched_keywords": assessment.get("matched_keywords", []),
        "proposed_children": proposed_children,
        "created_children": [],
        "next_action": "approve_plan_or_rerun_with_decompose_never",
        "timestamp": utc_now_iso(),
    }


def format_decomposition_plan_comment(payload: dict) -> str:
    status = str(payload.get("status") or "proposed").strip().lower() or "proposed"
    reasons = payload.get("reason") if isinstance(payload.get("reason"), list) else []
    children = payload.get("proposed_children")
    if not isinstance(children, list):
        children = []
    created_children = payload.get("created_children")
    if not isinstance(created_children, list):
        created_children = []
    resume_context = payload.get("resume_context")
    if not isinstance(resume_context, dict):
        resume_context = {}

    reason_lines = "\n".join(f"- `{reason}`" for reason in reasons) or "- `manual`"
    child_lines: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        order = child.get("order")
        title = str(child.get("title") or "Untitled child task")
        deps = child.get("depends_on") if isinstance(child.get("depends_on"), list) else []
        deps_text = f"; depends on: {', '.join(str(dep) for dep in deps)}" if deps else ""
        child_lines.append(f"{order}. {title}{deps_text}")
    children_text = "\n".join(child_lines) or "No child tasks proposed."

    created_lines: list[str] = []
    for child in created_children:
        if not isinstance(child, dict):
            continue
        order = child.get("order")
        created_issue_number = child.get("issue_number")
        issue_url = str(child.get("issue_url") or "").strip()
        title = str(child.get("title") or f"Child task {order}")

        if isinstance(created_issue_number, int):
            created_entry = f"- {order}. #{created_issue_number} {title}"
        elif issue_url:
            created_entry = f"- {order}. {title} ({issue_url})"
        else:
            created_entry = f"- {order}. {title}"
        created_lines.append(created_entry)
    created_text = "\n".join(created_lines) or "No child issues created yet."
    resume_lines: list[str] = []
    resume_branch = str(resume_context.get("branch") or "").strip()
    resume_base = str(resume_context.get("base_branch") or "").strip()
    resume_action = str(resume_context.get("next_action") or payload.get("next_action") or "").strip()
    resume_issue = resume_context.get("resume_issue")
    if resume_branch:
        resume_lines.append(f"- branch: `{resume_branch}`")
    if resume_base:
        resume_lines.append(f"- base branch: `{resume_base}`")
    if is_trackable_issue_number(resume_issue):
        resume_lines.append(f"- resume parent issue: `{resume_issue}`")
    if resume_action:
        resume_lines.append(f"- next action: `{resume_action}`")
    selected_child = resume_context.get("selected_child") if isinstance(resume_context.get("selected_child"), dict) else None
    if isinstance(selected_child, dict):
        child_order = selected_child.get("order")
        child_title = str(selected_child.get("title") or "").strip()
        child_issue_number = _as_positive_int(selected_child.get("issue_number"))
        child_text = f"{child_order}: {child_title}" if child_title else str(child_order)
        if child_issue_number is not None:
            child_text = f"{child_text} (#{child_issue_number})"
        resume_lines.append(f"- selected child: `{child_text}`")
    resume_text = "\n".join(resume_lines) or "- Resume context will be filled in when execution starts."

    if status == "children_created":
        status_note = (
            "Status: `children-created`; suggested execution sequence is preserved and "
            "child links are recorded in this plan payload."
        )
        next_action = "Execute child issues in listed order."
    elif status == "approved":
        status_note = "Status: `approved`; ready to create child issues."
        next_action = "Run with --create-child-issues to create linked child issues."
    else:
        status_note = "Status: `needs-decomposition`; child tasks were proposed for planning review."
        next_action = (
            "approve/edit this plan, then rerun with --create-child-issues, or rerun with "
            "--decompose never to intentionally bypass planning-only decomposition."
        )

    if status != "children_created":
        created_text = f"Created child issues (tracked):\n{created_text}"

    return (
        "Decomposition plan\n\n"
        f"{status_note}\n\n"
        "Reason:\n"
        f"{reason_lines}\n\n"
        "Proposed child tasks:\n"
        f"{children_text}\n\n"
        "Execution context:\n"
        f"{resume_text}\n\n"
        f"Created child issues:\n{created_text}\n\n"
        f"Recommended next action: {next_action}\n\n"
        f"{DECOMPOSITION_PLAN_MARKER}\n"
        f"```json\n{json.dumps(payload, ensure_ascii=True, indent=2)}\n```"
    )


def post_decomposition_plan_comment(
    repo: str,
    issue_number: int | str,
    payload: dict,
    dry_run: bool,
) -> None:
    if dry_run:
        print(
            f"[dry-run] Would post decomposition plan to issue #{issue_number}: "
            f"children={len(payload.get('proposed_children') or [])}"
        )
        return

    current_tracker_provider().post_issue_comment(
        repo=repo,
        issue_id=issue_number,
        body=format_decomposition_plan_comment(payload),
    )


def safe_post_issue_scope_skip_comment(
    repo: str,
    issue_number: int | str,
    reason: str,
    forced: bool,
    dry_run: bool,
) -> None:
    try:
        body = build_issue_scope_skip_comment(
            issue_number=issue_number,
            reason=reason,
            forced=forced,
        )
        if dry_run:
            print(
                f"[dry-run] Would post scope decision comment to issue {format_issue_ref(issue_number, tracker=current_tracker_provider().name)}: "
                f"decision={'forced-in-scope' if forced else 'out-of-scope'}"
            )
            return

        current_tracker_provider().post_issue_comment(
            repo=repo,
            issue_id=issue_number,
            body=body,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to post scope decision comment for issue #{issue_number}: {exc}",
            file=sys.stderr,
        )


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
    workflow_checks: list[dict] | None = None,
    ci_checks: list[dict] | None = None,
    ci_diagnostics: dict[str, object] | None = None,
    residual_untracked_files: list[str] | None = None,
    decomposition: dict | None = None,
    stats: dict[str, object] | None = None,
    required_file_validation: dict[str, object] | None = None,
    merge_readiness: dict[str, object] | None = None,
    merge_policy: dict[str, object] | None = None,
) -> dict:
    if status not in ORCHESTRATION_STATE_STATUSES:
        raise RuntimeError(f"Unsupported orchestration state status: {status}")
    if task_type not in {"issue", "pr"}:
        raise RuntimeError(f"Unsupported orchestration task type: {task_type}")

    state = {
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
    if workflow_checks is not None:
        state["workflow_checks"] = workflow_checks
    if ci_checks is not None:
        state["ci_checks"] = ci_checks
    if ci_diagnostics is not None:
        state["ci_diagnostics"] = ci_diagnostics
    if residual_untracked_files is not None:
        sorted_residual = sorted(residual_untracked_files)
        state["residual_untracked_files"] = sorted_residual
        state["residual_untracked_count"] = len(sorted_residual)
    if stats is not None:
        state["stats"] = stats
    if decomposition is not None:
        state["decomposition"] = decomposition
    if required_file_validation is not None:
        state["required_file_validation"] = required_file_validation
    if merge_readiness is not None:
        state["merge_readiness"] = merge_readiness
    if merge_policy is not None:
        state["merge_policy"] = merge_policy
    return state


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


def format_clarification_request_comment(question: str, reason: str | None = None) -> str:
    lines = [
        "Automation needs clarification before it can continue safely.",
        "",
        f"Question: {question}",
    ]
    if reason and reason.strip() and reason.strip() != question.strip():
        lines.extend(["", f"Why this is blocked: {reason.strip()}"])
    lines.extend(["", "Next action: reply here and rerun the orchestrator."])
    return "\n".join(lines)


def post_clarification_request_comment(
    repo: str,
    target_type: str,
    target_number: int,
    question: str,
    reason: str | None,
    dry_run: bool,
) -> None:
    if target_type not in {"issue", "pr"}:
        raise RuntimeError(f"Unsupported clarification comment target type: {target_type}")

    body = format_clarification_request_comment(question=question, reason=reason)
    if dry_run:
        print(
            f"[dry-run] Would post clarification request to {target_type} #{target_number}: "
            f"question={question}"
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


def safe_post_clarification_request_comment(
    repo: str,
    target_type: str,
    target_number: int,
    question: str,
    reason: str | None,
    dry_run: bool,
) -> None:
    try:
        post_clarification_request_comment(
            repo=repo,
            target_type=target_type,
            target_number=target_number,
            question=question,
            reason=reason,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to post clarification request to {target_type} #{target_number}: {exc}",
            file=sys.stderr,
        )


def post_orchestration_state_comment(
    repo: str,
    target_type: str,
    target_number: int | str,
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

    if target_type == "issue":
        current_tracker_provider().post_issue_comment(
            repo=repo,
            issue_id=target_number,
            body=body,
        )
        return

    current_codehost_provider().post_pr_comment(
        repo=repo,
        pr_number=int(target_number),
        body=body,
    )


def safe_post_orchestration_state_comment(
    repo: str,
    target_type: str,
    target_number: int | str,
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


def format_orchestration_claim_comment(claim: dict) -> str:
    status = str(claim.get("status") or "unknown")
    issue_number = claim.get("issue")
    tracker_name = current_tracker_provider().name
    return (
        f"Orchestration claim update: {status} for issue {format_issue_ref(issue_number, tracker=tracker_name)}.\n\n"
        f"{ORCHESTRATION_CLAIM_MARKER}\n"
        f"```json\n{json.dumps(claim, ensure_ascii=True, indent=2)}\n```"
    )


def post_orchestration_claim_comment(repo: str, issue_number: int | str, claim: dict, dry_run: bool) -> None:
    body = format_orchestration_claim_comment(claim)
    issue_ref = format_issue_ref(issue_number, tracker=current_tracker_provider().name)
    if dry_run:
        print(
            f"[dry-run] Would post orchestration claim to issue {issue_ref}: "
            f"status={claim.get('status')}"
        )
        return

    current_tracker_provider().post_issue_comment(
        repo=repo,
        issue_id=issue_number,
        body=body,
    )


def safe_post_orchestration_claim_comment(
    repo: str,
    issue_number: int | str,
    claim: dict,
    dry_run: bool,
) -> None:
    try:
        post_orchestration_claim_comment(
            repo=repo,
            issue_number=issue_number,
            claim=claim,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to post orchestration claim to issue {format_issue_ref(issue_number, tracker=current_tracker_provider().name)}: {exc}",
            file=sys.stderr,
        )


def build_prompt(issue: dict, image_paths: list[str] | None = None) -> str:
    attached_images = image_paths if image_paths else []
    image_section = ""
    if attached_images:
        image_filenames = ", ".join(
            sorted(os.path.basename(path) for path in attached_images if isinstance(path, str) and path.strip())
        )
        if image_filenames:
            image_section = f"\nImage attachments provided with this prompt: {image_filenames}\n"

    return (
        "You are working on an issue in the current git branch.\n"
        "Implement the fix for the issue in the repository files.\n"
        "Do not run git commands; git actions are handled by orchestration script.\n\n"
        "If the task is ambiguous, unsafe, or needs product/business judgment, do not guess and do not wait for interactive approval. "
        f"Instead, stop and print {CLARIFICATION_REQUEST_MARKER} followed by a JSON object like "
        '{"question":"<focused question>","reason":"<why clarification is required>"}.\n\n'
        f"Issue: {format_issue_ref_from_issue(issue)} - {issue['title']}\n"
        f"URL: {issue['url']}\n\n"
        "Issue body:\n"
        f"{issue.get('body', '').strip()}\n"
        f"{image_section}"
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
        "If the requested change is ambiguous, unsafe, or needs product/business judgment, do not guess and do not wait for interactive approval. "
        f"Instead, stop and print {CLARIFICATION_REQUEST_MARKER} followed by a JSON object like "
        '{"question":"<focused question>","reason":"<why clarification is required>"}.\n\n'
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
    clarification_answer: dict | None = None,
) -> tuple[str, str]:
    recovered_status = ""
    if isinstance(recovered_state, dict):
        recovered_status = str(recovered_state.get("status") or "")

    if recovered_status == "waiting-for-author" and clarification_answer is not None:
        recovered_payload = recovered_state.get("payload") if isinstance(recovered_state, dict) else None
        recovered_task_type = ""
        if isinstance(recovered_payload, dict):
            recovered_task_type = str(recovered_payload.get("task_type") or "")
        if linked_open_pr is not None and recovered_task_type == "pr":
            pr_number = linked_open_pr.get("number")
            return "pr-review", f"recovered waiting-for-author state has a newer author answer for linked PR #{pr_number}"
        return "issue-flow", "recovered waiting-for-author state has a newer author answer"

    if recovered_status in {"waiting-for-author", "blocked"}:
        return (
            "skip",
            f"recovered orchestration state is {recovered_status}; skipping until explicitly resumed",
        )

    if force_issue_flow:
        return "issue-flow", "--force-issue-flow is set"

    if recovered_status in {"waiting-for-ci", "ready-to-merge"} and linked_open_pr is not None:
        pr_number = linked_open_pr.get("number")
        return (
            "pr-review",
            f"recovered orchestration state is {recovered_status} and linked open PR #{pr_number} exists",
        )

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


_TOKEN_LINE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\binput(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)"), "tokens_in"),
    (re.compile(r"(?i)\bin(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)"), "tokens_in"),
    (re.compile(r"(?i)\boutput(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)"), "tokens_out"),
    (re.compile(r"(?i)\bout(?:\s+tokens?)\b[:\s=]*([0-9][0-9, _]*)"), "tokens_out"),
)


_COMBINED_TOKEN_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)~?\s*([0-9][0-9, _]*)\s+in\s*/\s*~?\s*([0-9][0-9, _]*)\s+out"),
)


def _parse_int_value(value: str) -> int | None:
    normalized = re.sub(r"[ ,_]", "", value)
    try:
        return int(normalized)
    except ValueError:
        return None


def _parse_cost_value(value: str) -> float | None:
    normalized = value.replace(",", "").strip()
    normalized = normalized.lstrip("~$ ")
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _update_agent_run_stats(
    line: str,
    track_tokens: bool,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: float | None,
) -> tuple[int | None, int | None, float | None]:
    if not track_tokens:
        return tokens_in, tokens_out, cost_usd

    for pattern in _COMBINED_TOKEN_LINE_PATTERNS:
        combined_match = pattern.search(line)
        if combined_match:
            if combined_match.group(1):
                parsed = _parse_int_value(combined_match.group(1))
                if parsed is not None:
                    tokens_in = parsed
            if combined_match.group(2):
                parsed = _parse_int_value(combined_match.group(2))
                if parsed is not None:
                    tokens_out = parsed
            return tokens_in, tokens_out, cost_usd

    for pattern, metric in _TOKEN_LINE_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue

        parsed = _parse_int_value(match.group(1))
        if parsed is None:
            continue

        if metric == "tokens_in":
            tokens_in = parsed
        else:
            tokens_out = parsed

    cost_match = re.search(r"\$([0-9]+(?:\.[0-9]{1,4})?)", line)
    if cost_match:
        parsed_cost = _parse_cost_value(cost_match.group(0))
        if parsed_cost is not None:
            cost_usd = parsed_cost

    return tokens_in, tokens_out, cost_usd


def _total_tracked_tokens(tokens_in: int | None, tokens_out: int | None) -> int | None:
    if tokens_in is None and tokens_out is None:
        return None
    return (tokens_in or 0) + (tokens_out or 0)


def _build_agent_run_stats(
    elapsed_seconds: float,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: float | None,
) -> dict[str, object]:
    elapsed = format_elapsed_duration(elapsed_seconds)
    stats: dict[str, object] = {
        "elapsed_seconds": int(elapsed_seconds),
        "elapsed": elapsed,
    }
    if tokens_in is not None:
        stats["tokens_in"] = tokens_in
    if tokens_out is not None:
        stats["tokens_out"] = tokens_out
    total_tokens = _total_tracked_tokens(tokens_in=tokens_in, tokens_out=tokens_out)
    if total_tokens is not None:
        stats["tokens_total"] = total_tokens
    if cost_usd is not None:
        stats["cost_usd"] = cost_usd
    return stats


def _format_token_count(value: int | str | None) -> str | None:
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return f"{value:,}"


def _format_budget_message_count(value: int | str | None) -> str | None:
    formatted = _format_token_count(value)
    if formatted is None:
        return None
    return formatted.replace(",", " ")


def format_elapsed_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def print_agent_run_summary(item_label: str, stats: dict[str, object]) -> None:
    elapsed = str(stats.get("elapsed") or "unknown")
    if not isinstance(elapsed, str):
        elapsed = "unknown"

    tokens_in = _format_token_count(stats.get("tokens_in"))
    tokens_out = _format_token_count(stats.get("tokens_out"))
    if tokens_in is not None and tokens_out is not None:
        token_text = f"tokens: ~{tokens_in} in / ~{tokens_out} out"
    else:
        token_text = "tokens: unavailable"

    cost_text = "cost: unavailable"
    cost = stats.get("cost_usd")
    if isinstance(cost, int | float):
        cost_text = f"cost: ~${cost:.2f}"

    print(f"[{item_label}] elapsed: {elapsed} | {token_text} | {cost_text}")


def _record_agent_run_stats(
    run_stats: dict[str, object] | None,
    start: float,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: float | None,
) -> dict[str, object] | None:
    if run_stats is None:
        return None

    run_stats.clear()
    run_stats.update(
        _build_agent_run_stats(
            elapsed_seconds=(time.monotonic() - start),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
    )
    return run_stats


def run_agent(
    issue: dict,
    runner: str,
    agent: str,
    model: str | None,
    dry_run: bool,
    timeout_seconds: int,
    idle_timeout_seconds: int | None,
    opencode_auto_approve: bool,
    image_paths: list[str] | None = None,
    prompt_override: str | None = None,
    track_tokens: bool = False,
    token_budget: int | None = None,
    cost_budget_usd: float | None = None,
    run_stats: dict[str, object] | None = None,
    agent_result: dict[str, object] | None = None,
    expected_branch: str | None = None,
    expected_repo_root: str | None = None,
) -> int:
    prompt = prompt_override if prompt_override is not None else build_prompt(
        issue=issue,
        image_paths=image_paths,
    )
    return run_agent_with_prompt(
        prompt=prompt,
        item_label=format_issue_label_from_issue(issue),
        runner=runner,
        agent=agent,
        model=model,
        image_paths=image_paths,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        opencode_auto_approve=opencode_auto_approve,
        track_tokens=track_tokens,
        token_budget=token_budget,
        cost_budget_usd=cost_budget_usd,
        run_stats=run_stats,
        agent_result=agent_result,
        cwd=expected_repo_root,
        expected_branch=expected_branch,
        expected_repo_root=expected_repo_root,
    )


def build_agent_command(
    runner: str,
    prompt: str,
    agent: str,
    model: str | None,
    image_paths: list[str] | None = None,
    opencode_auto_approve: bool = False,
) -> list[str]:
    if runner == "claude":
        command = ["claude", "--dangerously-skip-permissions"]
        for image_path in image_paths or []:
            command.extend(["--image", image_path])
        command.extend(["-p", prompt])
        if model:
            command.extend(["--model", model])
        return command

    command = ["opencode", "run", "--agent", agent]
    if model:
        command.extend(["--model", model])
    if opencode_auto_approve:
        command.append("--dangerously-skip-permissions")
    command.append(prompt)
    return command


def should_skip_issue_for_empty_body(
    mode: str,
    include_empty: bool,
    has_issue_text: bool,
    issue_image_urls: list[str] | None = None,
) -> bool:
    if mode != "issue-flow":
        return False

    return (
        not has_issue_text
        and not include_empty
        and not issue_image_urls
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
    image_paths: list[str] | None = None,
    track_tokens: bool = False,
    token_budget: int | None = None,
    cost_budget_usd: float | None = None,
    run_stats: dict[str, object] | None = None,
    agent_result: dict[str, object] | None = None,
    cwd: str | None = None,
    expected_branch: str | None = None,
    expected_repo_root: str | None = None,
) -> int:
    command = build_agent_command(
        runner=runner,
        prompt=prompt,
        agent=agent,
        model=model,
        image_paths=image_paths,
        opencode_auto_approve=opencode_auto_approve,
    )

    if dry_run:
        image_count = len(image_paths or [])
        if image_count:
            print(
                f"[dry-run] Would run: {' '.join(command[:4])} ... for {item_label} "
                f"with {image_count} image attachment(s)"
            )
        else:
            print(f"[dry-run] Would run: {' '.join(command[:4])} ... for {item_label}")
        return 0

    validate_opencode_model_backend(runner=runner, model=model)
    assert_expected_git_context(
        expected_branch=expected_branch,
        expected_repo_root=expected_repo_root,
        operation=f"start agent for {item_label}",
    )

    start = time.monotonic()
    print(f"Running agent for {item_label}")
    last_output = start
    track_tokens = bool(track_tokens or token_budget is not None)
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    captured_output: list[str] = []

    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
    )

    line_queue: _queue.Queue[tuple[str, str]] = _queue.Queue()

    def _raise_if_token_budget_exceeded() -> None:
        total_tokens = _total_tracked_tokens(tokens_in=tokens_in, tokens_out=tokens_out)
        if token_budget is None or total_tokens is None or total_tokens <= token_budget:
            return

        _record_agent_run_stats(
            run_stats=run_stats,
            start=start,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
        budget_error = TokenBudgetExceededError(
            budget=token_budget,
            reached=total_tokens,
            item_label=item_label,
        )
        print(str(budget_error))
        raise budget_error

    def _raise_if_cost_budget_exceeded() -> None:
        if cost_budget_usd is None or cost_usd is None or cost_usd <= cost_budget_usd:
            return

        _record_agent_run_stats(
            run_stats=run_stats,
            start=start,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
        budget_error = CostBudgetExceededError(
            budget=cost_budget_usd,
            reached=cost_usd,
            item_label=item_label,
        )
        print(str(budget_error))
        raise budget_error

    def _pipe_reader(stream, tag: str) -> None:
        try:
            for line in stream:
                line_queue.put((tag, line))
        finally:
            line_queue.put((tag, ""))

    stdout_thread = threading.Thread(
        target=_pipe_reader, args=(process.stdout, "stdout"), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pipe_reader, args=(process.stderr, "stderr"), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    while True:
        now = time.monotonic()
        elapsed = now - start
        idle_elapsed = now - last_output

        if timeout_seconds > 0 and elapsed > timeout_seconds:
            process.kill()
            process.wait(timeout=10)
            _record_agent_run_stats(
                run_stats=run_stats,
                start=start,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )
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
            _record_agent_run_stats(
                run_stats=run_stats,
                start=start,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )
            raise RuntimeError(
                f"Agent produced no output for {idle_timeout_seconds}s on {item_label}; "
                "aborting to avoid indefinite hang. Possible causes: waiting for "
                "interactive approval or a stuck process. Try --opencode-auto-approve "
                "(if safe) or a larger --agent-idle-timeout-seconds."
            )

        try:
            tag, line = line_queue.get(timeout=1.0)
            if line:
                last_output = time.monotonic()
                if track_tokens:
                    tokens_in, tokens_out, cost_usd = _update_agent_run_stats(
                        line=line,
                        track_tokens=track_tokens,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=cost_usd,
                    )
                    total_tokens = _total_tracked_tokens(tokens_in=tokens_in, tokens_out=tokens_out)
                    if token_budget is not None and total_tokens is not None and total_tokens > token_budget:
                        process.kill()
                        process.wait(timeout=10)
                        _raise_if_token_budget_exceeded()
                    if cost_budget_usd is not None and cost_usd is not None and cost_usd > cost_budget_usd:
                        process.kill()
                        process.wait(timeout=10)
                        _raise_if_cost_budget_exceeded()
                if tag == "stderr":
                    captured_output.append(line)
                    print(line, end="", file=sys.stderr)
                else:
                    captured_output.append(line)
                    print(line, end="")
        except _queue.Empty:
            pass

        if process.poll() is not None:
            stdout_thread.join()
            stderr_thread.join()
            while not line_queue.empty():
                tag, line = line_queue.get_nowait()
                if line:
                    if track_tokens:
                        tokens_in, tokens_out, cost_usd = _update_agent_run_stats(
                            line=line,
                            track_tokens=track_tokens,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            cost_usd=cost_usd,
                        )
                        total_tokens = _total_tracked_tokens(tokens_in=tokens_in, tokens_out=tokens_out)
                        if token_budget is not None and total_tokens is not None and total_tokens > token_budget:
                            _raise_if_token_budget_exceeded()
                        if cost_budget_usd is not None and cost_usd is not None and cost_usd > cost_budget_usd:
                            _raise_if_cost_budget_exceeded()
                    if tag == "stderr":
                        captured_output.append(line)
                        print(line, end="", file=sys.stderr)
                    else:
                        captured_output.append(line)
                        print(line, end="")

            recorded_stats = _record_agent_run_stats(
                run_stats=run_stats,
                start=start,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )
            if agent_result is not None:
                agent_result.clear()
                agent_result["output"] = "".join(captured_output)
                clarification_request = latest_clarification_request_from_agent_output(
                    agent_result["output"]
                )
                if clarification_request is not None:
                    agent_result["clarification_request"] = clarification_request
            if recorded_stats is not None:
                print_agent_run_summary(item_label=item_label, stats=recorded_stats)
            return process.returncode


def create_branch(base_branch: str, branch_name: str, dry_run: bool) -> None:
    prepare_issue_branch(
        base_branch=base_branch,
        branch_name=branch_name,
        dry_run=dry_run,
        fail_on_existing=False,
    )


def create_followup_branch(current_branch_name: str, branch_name: str, dry_run: bool) -> None:
    if dry_run:
        print(
            f"[dry-run] Would create follow-up branch '{branch_name}' "
            f"from '{current_branch_name}'"
        )
        return

    if local_branch_exists(branch_name):
        run_command(["git", "checkout", branch_name])
        print(f"Reusing existing follow-up branch: {branch_name}")
        return

    run_command(["git", "checkout", "-b", branch_name])
    print(f"Created follow-up branch: {branch_name}")


def local_branch_exists(branch_name: str) -> bool:
    return _branch_recovery.local_branch_exists(
        branch_name,
        command_succeeds=command_succeeds,
    )


def remote_branch_exists(branch_name: str) -> bool:
    return _branch_recovery.remote_branch_exists(
        branch_name,
        command_succeeds=command_succeeds,
    )


def list_conflicted_paths() -> list[str]:
    return _branch_recovery.list_conflicted_paths(run_capture=run_capture)


def build_branch_sync_result(
    *,
    branch_name: str,
    remote_base_ref: str,
    requested_strategy: str,
    applied_strategy: str,
    status: str,
    changed: bool,
    auto_resolved: bool,
) -> dict[str, object]:
    return _branch_recovery.build_branch_sync_result(
        branch_name=branch_name,
        remote_base_ref=remote_base_ref,
        requested_strategy=requested_strategy,
        applied_strategy=applied_strategy,
        status=status,
        changed=changed,
        auto_resolved=auto_resolved,
    )


def print_branch_sync_result(result: dict[str, object], *, dry_run: bool = False) -> None:
    _branch_recovery.print_branch_sync_result(result, dry_run=dry_run)


def push_recovered_branch(
    branch_name: str,
    result: dict[str, object],
    dry_run: bool,
    expected_repo_root: str | None = None,
) -> None:
    _branch_recovery.push_recovered_branch(
        branch_name,
        result,
        dry_run,
        push_branch=push_branch,
        expected_repo_root=expected_repo_root,
    )


def auto_resolve_merge_conflicts_with_base() -> int:
    return _branch_recovery.auto_resolve_merge_conflicts_with_base(
        list_conflicted_paths=list_conflicted_paths,
        run_command=run_command,
    )


def merge_sync_with_auto_resolution(
    remote_base_ref: str,
    branch_name: str,
    requested_strategy: str,
) -> dict[str, object]:
    return _branch_recovery.merge_sync_with_auto_resolution(
        remote_base_ref,
        branch_name,
        requested_strategy,
        run_command=run_command,
        command_succeeds=command_succeeds,
        current_head_sha=current_head_sha,
        auto_resolve_merge_conflicts_with_base=auto_resolve_merge_conflicts_with_base,
        build_branch_sync_result=build_branch_sync_result,
    )


def prepare_issue_branch(
    base_branch: str,
    branch_name: str,
    dry_run: bool,
    fail_on_existing: bool,
) -> str:
    return _branch_recovery.prepare_issue_branch(
        base_branch,
        branch_name,
        dry_run,
        fail_on_existing,
        local_branch_exists=local_branch_exists,
        remote_branch_exists=remote_branch_exists,
        run_command=run_command,
    )


def sync_reused_branch_with_base(
    base_branch: str,
    branch_name: str,
    strategy: str,
    dry_run: bool,
) -> dict[str, object]:
    return _branch_recovery.sync_reused_branch_with_base(
        base_branch,
        branch_name,
        strategy,
        dry_run,
        run_command=run_command,
        command_succeeds=command_succeeds,
        current_head_sha=current_head_sha,
        merge_sync_with_auto_resolution=merge_sync_with_auto_resolution,
        build_branch_sync_result=build_branch_sync_result,
    )


def run_conflict_recovery_for_branch(
    *,
    branch_name: str,
    base_branch: str,
    strategy: str,
    dry_run: bool,
    verify_recovered_branch: Callable[[dict[str, object]], None] | None = None,
    expected_repo_root: str | None = None,
) -> dict[str, object]:
    return _branch_recovery.run_conflict_recovery_for_branch(
        branch_name=branch_name,
        base_branch=base_branch,
        strategy=strategy,
        dry_run=dry_run,
        sync_reused_branch_with_base=sync_reused_branch_with_base,
        print_branch_sync_result=print_branch_sync_result,
        verify_recovered_branch=verify_recovered_branch,
        push_recovered_branch=push_recovered_branch,
        verify_git_context=lambda: assert_expected_git_context(
            expected_branch=branch_name,
            expected_repo_root=expected_repo_root,
            operation=f"run conflict recovery for branch '{branch_name}'",
        ),
        expected_repo_root=expected_repo_root,
    )


def commit_changes(
    issue: dict,
    dry_run: bool,
    pre_run_untracked_files: set[str] | None = None,
    expected_branch: str | None = None,
    expected_repo_root: str | None = None,
) -> str:
    message = issue_commit_title(issue)
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
    assert_expected_git_context(
        expected_branch=expected_branch,
        expected_repo_root=expected_repo_root,
        operation="commit issue changes",
    )
    stage_worktree_changes(pre_run_untracked_files)
    run_command(["git", "commit", "-m", message])

    residual_untracked_files = residual_untracked_files_after_baseline(pre_run_untracked_files)
    if residual_untracked_files:
        raise ResidualUntrackedFilesError(
            files=residual_untracked_files,
            stage="issue_commit_validation",
        )

    return message


def push_branch(
    branch_name: str,
    dry_run: bool,
    force_with_lease: bool = False,
    expected_repo_root: str | None = None,
) -> None:
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
    assert_expected_git_context(
        expected_branch=branch_name,
        expected_repo_root=expected_repo_root,
        operation="push branch",
    )
    run_command(command)


def push_current_branch(dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] Would push current branch to origin")
        return
    run_command(["git", "push"])


def commit_pr_review_changes(
    pull_request: dict,
    dry_run: bool,
    pre_run_untracked_files: set[str] | None = None,
    expected_branch: str | None = None,
    expected_repo_root: str | None = None,
) -> str:
    message = f"Address review comments for PR #{pull_request['number']}"
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
    assert_expected_git_context(
        expected_branch=expected_branch,
        expected_repo_root=expected_repo_root,
        operation="commit PR review changes",
    )
    stage_worktree_changes(pre_run_untracked_files)
    run_command(["git", "commit", "-m", message])

    residual_untracked_files = residual_untracked_files_after_baseline(pre_run_untracked_files)
    if residual_untracked_files:
        raise ResidualUntrackedFilesError(
            files=residual_untracked_files,
            stage="pr_review_commit_validation",
        )

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


def _first_failed_workflow_result(results: list[dict[str, object]] | object) -> dict[str, object] | None:
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        if str(result.get("status") or "").strip().lower() == "failed":
            return result
    return None


def derive_recovery_verification_next_hypothesis(
    *,
    verification: dict[str, object],
    recovery_result: dict[str, object] | None,
) -> str:
    failed = _first_failed_workflow_result(verification.get("commands"))
    failed_name = str(failed.get("name") or "verification command") if isinstance(failed, dict) else "verification command"
    auto_resolved = bool(recovery_result and recovery_result.get("auto_resolved"))
    if auto_resolved:
        return (
            f"The auto-resolved recovery likely preserved mergeability but left '{failed_name}' failing; "
            "the branch needs a follow-up code fix before PR review can continue."
        )
    return (
        f"The sync fixed mergeability, but '{failed_name}' still fails on the recovered branch; "
        "the branch likely needs a focused follow-up fix before PR review can continue."
    )


def format_recovery_verification_follow_up_comment(
    *,
    branch_name: str,
    verification: dict[str, object],
    recovery_result: dict[str, object] | None,
    next_action: str,
) -> str:
    summary = _as_optional_string(verification.get("summary")) or "failed"
    error = _as_optional_string(verification.get("error")) or summary
    commands = verification.get("commands") if isinstance(verification.get("commands"), list) else []
    failed = _first_failed_workflow_result(commands)
    next_hypothesis = derive_recovery_verification_next_hypothesis(
        verification=verification,
        recovery_result=recovery_result,
    )
    recovery_status = str((recovery_result or {}).get("status") or "synced")
    applied_strategy = _as_optional_string((recovery_result or {}).get("applied_strategy")) or "unknown"

    lines = [
        "Recovery follow-up: mergeability sync passed, but verification still failed.",
        "",
        f"- Branch: `{branch_name}`",
        f"- Recovery result: `{recovery_status}` via `{applied_strategy}`",
        f"- Verification summary: `{summary}`",
        f"- Error: `{error}`",
    ]

    if isinstance(failed, dict):
        failed_name = str(failed.get("name") or "verification")
        exit_code = failed.get("exit_code")
        evidence = (
            _as_optional_string(failed.get("stderr_excerpt"))
            or _as_optional_string(failed.get("stdout_excerpt"))
            or _as_optional_string(failed.get("error"))
        )
        detail = f"- Failed check: `{failed_name}`"
        if exit_code is not None:
            detail += f" (exit code `{exit_code}`)"
        lines.append(detail)
        if evidence:
            lines.append(f"- Evidence: `{evidence}`")

    lines.extend(
        [
            "",
            f"Next hypothesis: {next_hypothesis}",
            f"Next action: {_humanize_status_token(next_action)}.",
        ]
    )
    return "\n".join(lines)


def post_recovery_verification_follow_up_comment(
    *,
    repo: str,
    pr_number: int,
    branch_name: str,
    verification: dict[str, object],
    recovery_result: dict[str, object] | None,
    next_action: str,
    dry_run: bool,
) -> None:
    body = format_recovery_verification_follow_up_comment(
        branch_name=branch_name,
        verification=verification,
        recovery_result=recovery_result,
        next_action=next_action,
    )
    if dry_run:
        print(
            f"[dry-run] Would post recovery follow-up comment to PR #{pr_number}: "
            f"status={verification.get('status')}"
        )
        return

    current_codehost_provider().post_pr_comment(
        repo=repo,
        pr_number=pr_number,
        body=body,
    )


def safe_post_recovery_verification_follow_up_comment(
    *,
    repo: str,
    pr_number: int,
    branch_name: str,
    verification: dict[str, object],
    recovery_result: dict[str, object] | None,
    next_action: str,
    dry_run: bool,
) -> None:
    try:
        post_recovery_verification_follow_up_comment(
            repo=repo,
            pr_number=pr_number,
            branch_name=branch_name,
            verification=verification,
            recovery_result=recovery_result,
            next_action=next_action,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to post recovery follow-up comment to PR #{pr_number}: {exc}",
            file=sys.stderr,
        )


def open_pr(
    repo: str,
    base_branch: str,
    branch_name: str,
    issue: dict,
    dry_run: bool,
    stacked_base_context: str | None = None,
) -> str:
    return _github_lifecycle.open_pr(
        repo,
        base_branch,
        branch_name,
        issue,
        dry_run,
        run_capture=run_capture,
        format_issue_ref_from_issue=format_issue_ref_from_issue,
        issue_commit_title=issue_commit_title,
        issue_tracker=issue_tracker,
        tracker_github=TRACKER_GITHUB,
        stacked_base_context=stacked_base_context,
    )


def find_existing_pr(repo: str, base_branch: str, branch_name: str) -> dict | None:
    return _github_lifecycle.find_existing_pr(
        repo,
        base_branch,
        branch_name,
        run_capture=run_capture,
    )


def ensure_pr(
    repo: str,
    base_branch: str,
    branch_name: str,
    issue: dict,
    dry_run: bool,
    fail_on_existing: bool,
    stacked_base_context: str | None = None,
) -> tuple[str, str]:
    return _github_lifecycle.ensure_pr(
        repo,
        base_branch,
        branch_name,
        issue,
        dry_run,
        fail_on_existing,
        find_existing_pr=lambda repo_arg, base_arg, branch_arg: find_existing_pr(
            repo=repo_arg,
            base_branch=base_arg,
            branch_name=branch_arg,
        ),
        open_pr=lambda repo_arg, base_arg, branch_arg, issue_arg, dry_run_arg, stacked_base_context_arg: open_pr(
            repo=repo_arg,
            base_branch=base_arg,
            branch_name=branch_arg,
            issue=issue_arg,
            dry_run=dry_run_arg,
            stacked_base_context=stacked_base_context_arg,
        ),
        stacked_base_context=stacked_base_context,
    )


def resolve_local_config_path(raw_path: str | None, target_dir: str) -> str:
    config_path = raw_path or LOCAL_CONFIG_RELATIVE_PATH
    if not os.path.isabs(config_path):
        config_path = os.path.join(target_dir, config_path)
    return os.path.abspath(config_path)


def resolve_project_config_path(raw_path: str | None, target_dir: str) -> str:
    config_path = raw_path or PROJECT_CONFIG_RELATIVE_PATH
    if not os.path.isabs(config_path):
        config_path = os.path.join(target_dir, config_path)
    return os.path.abspath(config_path)


def validate_local_config(config: dict, config_path: str) -> dict:
    supported_keys = {
        "tracker",
        "codehost",
        "state",
        "limit",
        "runner",
        "agent",
        "model",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "token_budget",
        "opencode_auto_approve",
        "branch_prefix",
        "include_empty",
        "stop_on_error",
        "fail_on_existing",
        "force_issue_flow",
        "skip_if_pr_exists",
        "skip_if_branch_exists",
        "force_reprocess",
        "sync_reused_branch",
        "sync_strategy",
        "base_branch",
        "decompose",
        "create_child_issues",
        "track_tokens",
        "preset",
        "max_attempts",
    }

    unsupported = sorted(set(config) - supported_keys)
    if unsupported:
        unsupported_text = ", ".join(unsupported)
        raise RuntimeError(
            f"Unsupported key(s) in local config {config_path}: {unsupported_text}"
        )

    validated: dict = {}

    if "tracker" in config:
        validated["tracker"] = _parse_tracker(config["tracker"])

    if "codehost" in config:
        validated["codehost"] = _parse_codehost(config["codehost"])

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

    if "preset" in config:
        if not isinstance(config["preset"], str) or not config["preset"].strip():
            raise RuntimeError("Local config key 'preset' must be a non-empty string")
        validated["preset"] = config["preset"]

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

    if "token_budget" in config:
        value = config["token_budget"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                "Local config key 'token_budget' must be a positive integer or null"
            )
        validated["token_budget"] = value

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError("Local config key 'max_attempts' must be a positive integer")
        validated["max_attempts"] = value

    for key in [
        "opencode_auto_approve",
        "include_empty",
        "stop_on_error",
        "fail_on_existing",
        "force_issue_flow",
        "skip_if_pr_exists",
        "skip_if_branch_exists",
        "force_reprocess",
        "sync_reused_branch",
        "create_child_issues",
    ]:
        if key in config:
            if not isinstance(config[key], bool):
                raise RuntimeError(f"Local config key '{key}' must be a boolean")
            validated[key] = config[key]

    if "track_tokens" in config and "track_tokens" not in validated:
        if not isinstance(config["track_tokens"], bool):
            raise RuntimeError("Local config key 'track_tokens' must be a boolean")
        validated["track_tokens"] = config["track_tokens"]

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

    if "base_branch" in config:
        if config["base_branch"] not in {"default", "current"}:
            raise RuntimeError("Local config key 'base_branch' must be one of: default, current")
        validated["base_branch"] = config["base_branch"]

    if "decompose" in config:
        if config["decompose"] not in {"auto", "never", "always"}:
            raise RuntimeError("Local config key 'decompose' must be one of: auto, never, always")
        validated["decompose"] = config["decompose"]

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


def preset_cli_defaults(project_config: dict, preset_name: str | None) -> dict:
    normalized_name = _as_optional_string(preset_name)
    if normalized_name is None:
        return {}

    presets = project_config.get("presets")
    if not isinstance(presets, dict) or normalized_name not in presets:
        raise RuntimeError(f"Unknown preset '{normalized_name}' in project config")

    preset_config = presets.get(normalized_name)
    if not isinstance(preset_config, dict):
        raise RuntimeError(f"Project config key 'presets.{normalized_name}' must be an object")

    cli_defaults: dict = {"preset": normalized_name}
    for key in [
        "runner",
        "agent",
        "model",
        "track_tokens",
        "token_budget",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "max_attempts",
        "escalate_to_preset",
    ]:
        if key in preset_config:
            cli_defaults[key] = preset_config[key]
    return cli_defaults


def _argv_has_flag(argv: list[str], *flags: str) -> bool:
    for flag in flags:
        if any(arg == flag or arg.startswith(f"{flag}=") for arg in argv):
            return True
    return False


def _preset_tier(preset_name: str | None) -> str | None:
    normalized = _as_optional_string(preset_name)
    if normalized in PRESET_TIER_ORDER:
        return normalized
    return None


def _tier_rank(tier: str | None) -> int | None:
    if tier is None:
        return None
    try:
        return PRESET_TIER_ORDER.index(tier)
    except ValueError:
        return None


def _cap_preset_to_budget_tier(project_config: dict, preset_name: str | None, max_model_tier: str | None) -> str | None:
    normalized_preset = _as_optional_string(preset_name)
    normalized_tier = _as_optional_string(max_model_tier)
    if normalized_preset is None or normalized_tier is None:
        return normalized_preset

    preset_rank = _tier_rank(_preset_tier(normalized_preset))
    budget_rank = _tier_rank(normalized_tier)
    if preset_rank is None or budget_rank is None or preset_rank <= budget_rank:
        return normalized_preset

    presets = project_config.get("presets")
    if not isinstance(presets, dict):
        return normalized_preset

    for candidate in reversed(PRESET_TIER_ORDER[: budget_rank + 1]):
        if candidate in presets:
            return candidate
    return normalized_preset


def _matches_routing_rule(
    rule_when: dict,
    issue: dict,
    task_type: str,
    scope_eligible: bool,
    needs_decomposition: bool,
) -> bool:
    labels = _normalize_match_list(rule_when.get("labels"))
    if labels:
        issue_labels = set(_issue_label_names(issue))
        if not any(label in issue_labels for label in labels):
            return False

    task_types = _normalize_match_list(rule_when.get("task_types"))
    if task_types and task_type.strip().lower() not in task_types:
        return False

    scope = _as_optional_string(rule_when.get("scope"))
    if scope == "in" and not scope_eligible:
        return False
    if scope == "out" and scope_eligible:
        return False

    if "needs_decomposition" in rule_when and bool(rule_when.get("needs_decomposition")) != needs_decomposition:
        return False

    return True


def choose_routed_preset(
    project_config: dict,
    issue: dict,
    task_type: str,
    scope_eligible: bool,
    needs_decomposition: bool,
) -> str | None:
    routing = project_config.get("routing")
    if isinstance(routing, dict):
        rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            when = rule.get("when")
            if not isinstance(when, dict):
                continue
            if _matches_routing_rule(when, issue, task_type, scope_eligible, needs_decomposition):
                routed_preset = _as_optional_string(rule.get("preset"))
                if routed_preset is not None:
                    return routed_preset

        default_preset = _as_optional_string(routing.get("default_preset"))
        if default_preset is not None:
            return default_preset

    presets = project_config.get("presets")
    if not isinstance(presets, dict) or not presets:
        return None
    if needs_decomposition and "hard" in presets:
        return "hard"
    if "cheap" in presets:
        return "cheap"
    if "default" in presets:
        return "default"
    if "hard" in presets:
        return "hard"
    return None


def resolve_task_execution_settings(
    args: argparse.Namespace,
    argv: list[str],
    project_config: dict,
    issue: dict,
    task_type: str,
    scope_eligible: bool,
    needs_decomposition: bool,
) -> dict[str, object]:
    settings: dict[str, object] = {
        "preset": _as_optional_string(getattr(args, "preset", None)),
        "runner": getattr(args, "runner", BUILTIN_DEFAULTS["runner"]),
        "agent": getattr(args, "agent", BUILTIN_DEFAULTS["agent"]),
        "model": getattr(args, "model", BUILTIN_DEFAULTS["model"]),
        "track_tokens": bool(getattr(args, "track_tokens", BUILTIN_DEFAULTS["track_tokens"])),
        "token_budget": getattr(args, "token_budget", BUILTIN_DEFAULTS["token_budget"]),
        "agent_timeout_seconds": getattr(
            args,
            "agent_timeout_seconds",
            BUILTIN_DEFAULTS["agent_timeout_seconds"],
        ),
        "agent_idle_timeout_seconds": getattr(
            args,
            "agent_idle_timeout_seconds",
            BUILTIN_DEFAULTS["agent_idle_timeout_seconds"],
        ),
        "max_attempts": getattr(args, "max_attempts", BUILTIN_DEFAULTS["max_attempts"]),
        "escalate_to_preset": _as_optional_string(
            getattr(args, "escalate_to_preset", BUILTIN_DEFAULTS["escalate_to_preset"])
        ),
    }

    explicit_preset = _argv_has_flag(argv, "--preset")
    selected_preset = settings["preset"] if explicit_preset else choose_routed_preset(
        project_config=project_config,
        issue=issue,
        task_type=task_type,
        scope_eligible=scope_eligible,
        needs_decomposition=needs_decomposition,
    )
    selected_preset = _as_optional_string(selected_preset)

    if selected_preset is not None:
        settings["preset"] = selected_preset
        preset_defaults = preset_cli_defaults(project_config, selected_preset)
        explicit_overrides = {
            "runner": _argv_has_flag(argv, "--runner"),
            "agent": _argv_has_flag(argv, "--agent"),
            "model": _argv_has_flag(argv, "--model"),
            "track_tokens": _argv_has_flag(argv, "--track-tokens"),
            "token_budget": _argv_has_flag(argv, "--token-budget", "--max-tokens"),
            "agent_timeout_seconds": _argv_has_flag(argv, "--agent-timeout-seconds"),
            "agent_idle_timeout_seconds": _argv_has_flag(argv, "--agent-idle-timeout-seconds"),
            "max_attempts": _argv_has_flag(argv, "--max-attempts"),
            "escalate_to_preset": _argv_has_flag(argv, "--escalate-to-preset"),
        }
        for key, value in preset_defaults.items():
            if key == "preset" or explicit_overrides.get(key, False):
                continue
            settings[key] = value

    budgets = project_config.get("budgets") if isinstance(project_config.get("budgets"), dict) else {}
    max_model_tier = _as_optional_string(budgets.get("max_model_tier"))
    if max_model_tier is not None:
        capped_preset = _cap_preset_to_budget_tier(project_config, _as_optional_string(settings.get("preset")), max_model_tier)
        if capped_preset != settings.get("preset"):
            settings["preset"] = capped_preset
            capped_defaults = preset_cli_defaults(project_config, capped_preset)
            for key, value in capped_defaults.items():
                if key != "preset":
                    settings[key] = value
        settings["max_model_tier"] = max_model_tier

    max_attempts_cap = _as_positive_int(budgets.get("max_attempts_per_task"))
    if max_attempts_cap is not None:
        current_attempts = _as_positive_int(settings.get("max_attempts")) or BUILTIN_DEFAULTS["max_attempts"]
        settings["max_attempts"] = min(current_attempts, max_attempts_cap)

    runtime_cap_minutes = _as_positive_int(budgets.get("max_runtime_minutes"))
    if runtime_cap_minutes is not None:
        runtime_cap_seconds = runtime_cap_minutes * 60
        current_timeout = _as_positive_int(settings.get("agent_timeout_seconds")) or runtime_cap_seconds
        settings["agent_timeout_seconds"] = min(current_timeout, runtime_cap_seconds)

    cost_budget_usd = budgets.get("max_cost_usd")
    if isinstance(cost_budget_usd, (int, float)):
        settings["cost_budget_usd"] = float(cost_budget_usd)

    return settings


def build_attempt_execution_plan(project_config: dict, initial_settings: dict[str, object]) -> list[dict[str, object]]:
    max_attempts = _as_positive_int(initial_settings.get("max_attempts")) or 1
    max_model_tier = _as_optional_string(initial_settings.get("max_model_tier"))
    plan: list[dict[str, object]] = []
    current_settings = dict(initial_settings)

    for attempt in range(1, max_attempts + 1):
        attempt_settings = dict(current_settings)
        attempt_settings["attempt"] = attempt
        plan.append(attempt_settings)

        next_preset = _as_optional_string(current_settings.get("escalate_to_preset"))
        if next_preset is None:
            continue
        next_preset = _cap_preset_to_budget_tier(project_config, next_preset, max_model_tier)
        if next_preset is None:
            continue
        next_defaults = preset_cli_defaults(project_config, next_preset)
        current_settings = dict(current_settings)
        current_settings.update(next_defaults)
        current_settings["preset"] = next_preset
        if max_model_tier is not None:
            current_settings["max_model_tier"] = max_model_tier

    return plan


def _attempt_settings_summary(attempt_settings: dict[str, object]) -> str:
    runner = str(attempt_settings.get("runner") or BUILTIN_DEFAULTS["runner"])
    model = attempt_settings.get("model") or "default"
    preset = _as_optional_string(attempt_settings.get("preset"))
    attempt = _as_positive_int(attempt_settings.get("attempt")) or 1
    max_attempts = _as_positive_int(attempt_settings.get("max_attempts")) or 1
    if preset is None:
        return f"attempt {attempt}/{max_attempts} (runner={runner}, model={model})"
    return f"attempt {attempt}/{max_attempts} using preset {preset} (runner={runner}, model={model})"


def without_keys(config: dict, *keys: str) -> dict:
    return {key: value for key, value in config.items() if key not in keys}


def _doctor_record(checks: list[dict[str, str]], status: str, name: str, detail: str) -> None:
    checks.append({"status": status, "name": name, "detail": detail})


def _doctor_has_flag(argv: list[str], flag: str) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in argv)


def run_doctor(args: argparse.Namespace, raw_argv: list[str] | None = None) -> int:
    argv = raw_argv or []
    checks: list[dict[str, str]] = []

    target_dir = os.path.abspath(getattr(args, "dir", BUILTIN_DEFAULTS["dir"]))
    repo_arg = getattr(args, "repo", None)
    runner = str(getattr(args, "runner", BUILTIN_DEFAULTS["runner"]))
    model = getattr(args, "model", None)
    agent = str(getattr(args, "agent", BUILTIN_DEFAULTS["agent"]))
    base_branch_mode = str(getattr(args, "base_branch", BUILTIN_DEFAULTS["base_branch"]))
    smoke_enabled = bool(getattr(args, "doctor_smoke_check", False))
    explicit_local_config = _doctor_has_flag(argv, "--local-config")
    explicit_project_config = _doctor_has_flag(argv, "--project-config")

    print("Doctor diagnostics")
    print(f"- Directory: {target_dir}")
    print(f"- Runner: {runner}")

    if not os.path.isdir(target_dir):
        _doctor_record(
            checks,
            "FAIL",
            "Repository directory",
            f"directory does not exist: {target_dir}",
        )
    elif not os.path.isdir(os.path.join(target_dir, ".git")):
        _doctor_record(
            checks,
            "FAIL",
            "Repository directory",
            f"not a git repository: {target_dir}",
        )
    else:
        ok_repo, stdout_repo, stderr_repo, _ = run_check_command(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_dir,
        )
        if ok_repo and stdout_repo == "true":
            _doctor_record(checks, "PASS", "Git repository", "inside a git work tree")
        else:
            detail = stderr_repo or stdout_repo or "unable to verify git work tree"
            _doctor_record(checks, "FAIL", "Git repository", detail)

        ok_clean, stdout_clean, stderr_clean, _ = run_check_command(
            ["git", "status", "--porcelain"],
            cwd=target_dir,
        )
        if not ok_clean:
            _doctor_record(
                checks,
                "FAIL",
                "Clean worktree",
                stderr_clean or "failed to query git status",
            )
        elif stdout_clean:
            _doctor_record(
                checks,
                "FAIL",
                "Clean worktree",
                "working tree has uncommitted changes",
            )
        else:
            _doctor_record(checks, "PASS", "Clean worktree", "working tree is clean")

    gh_path = shutil.which("gh")
    if gh_path:
        _doctor_record(checks, "PASS", "GitHub CLI", f"found at {gh_path}")
    else:
        _doctor_record(checks, "FAIL", "GitHub CLI", "gh is not installed or not in PATH")

    if gh_path:
        ok_auth, _stdout_auth, stderr_auth, _ = run_check_command(
            ["gh", "auth", "status"],
            cwd=target_dir,
        )
        if ok_auth:
            _doctor_record(checks, "PASS", "gh auth", "authenticated")
        else:
            _doctor_record(
                checks,
                "FAIL",
                "gh auth",
                stderr_auth or "not authenticated (run gh auth login)",
            )

    resolved_repo = ""
    if gh_path:
        repo_command = ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        if repo_arg:
            repo_command = [
                "gh",
                "repo",
                "view",
                repo_arg,
                "--json",
                "nameWithOwner",
                "--jq",
                ".nameWithOwner",
            ]
        ok_repo_access, stdout_repo_access, stderr_repo_access, _ = run_check_command(
            repo_command,
            cwd=target_dir,
        )
        resolved_repo = stdout_repo_access.strip()
        if ok_repo_access and resolved_repo:
            _doctor_record(
                checks,
                "PASS",
                "Repository access",
                f"resolved repository: {resolved_repo}",
            )
        else:
            _doctor_record(
                checks,
                "FAIL",
                "Repository access",
                stderr_repo_access
                or "unable to detect/access repository (pass --repo owner/name if needed)",
            )

    if gh_path and resolved_repo:
        ok_default, stdout_default, stderr_default, _ = run_check_command(
            [
                "gh",
                "repo",
                "view",
                resolved_repo,
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ],
            cwd=target_dir,
        )
        if ok_default and stdout_default:
            _doctor_record(
                checks,
                "PASS",
                "Default branch",
                f"default branch is '{stdout_default}'",
            )
        else:
            _doctor_record(
                checks,
                "FAIL",
                "Default branch",
                stderr_default or "unable to read repository default branch",
            )

    claude_path = shutil.which("claude")
    opencode_path = shutil.which("opencode")

    if runner == "claude":
        if claude_path:
            _doctor_record(checks, "PASS", "Selected runner (claude)", f"found at {claude_path}")
        else:
            _doctor_record(checks, "FAIL", "Selected runner (claude)", "claude CLI not found")
    else:
        if opencode_path:
            _doctor_record(checks, "PASS", "Selected runner (opencode)", f"found at {opencode_path}")
        else:
            _doctor_record(checks, "FAIL", "Selected runner (opencode)", "opencode CLI not found")

    if claude_path:
        _doctor_record(checks, "PASS", "Runner availability (claude)", "installed")
    else:
        _doctor_record(checks, "WARN", "Runner availability (claude)", "not installed")

    if opencode_path:
        _doctor_record(checks, "PASS", "Runner availability (opencode)", "installed")
    else:
        _doctor_record(checks, "WARN", "Runner availability (opencode)", "not installed")

    if runner == "opencode":
        try:
            local_ollama_model = _ollama_model_name(model)
        except RuntimeError as exc:
            _doctor_record(checks, "FAIL", "Ollama model availability", str(exc))
        else:
            if local_ollama_model is not None:
                try:
                    validate_opencode_model_backend(runner=runner, model=model)
                    _doctor_record(
                        checks,
                        "PASS",
                        "Ollama model availability",
                        f"validated local model '{local_ollama_model}'",
                    )
                except RuntimeError as exc:
                    _doctor_record(checks, "FAIL", "Ollama model availability", str(exc))

    if smoke_enabled:
        smoke_command: list[str]
        if runner == "claude":
            smoke_command = ["claude"]
            if model:
                smoke_command.extend(["--model", str(model)])
            smoke_command.append("--help")
        else:
            smoke_command = ["opencode", "run", "--agent", agent]
            if model:
                smoke_command.extend(["--model", str(model)])
            smoke_command.append("--help")

        ok_smoke, _stdout_smoke, stderr_smoke, _ = run_check_command(smoke_command, cwd=target_dir)
        if ok_smoke:
            _doctor_record(checks, "PASS", "Runner smoke check", "CLI invocation succeeded")
        else:
            _doctor_record(
                checks,
                "FAIL",
                "Runner smoke check",
                stderr_smoke or "runner smoke check failed",
            )
    else:
        _doctor_record(
            checks,
            "WARN",
            "Runner smoke check",
            "skipped (use --doctor-smoke-check to enable)",
        )

    project_config_path = resolve_project_config_path(getattr(args, "project_config", None), target_dir)
    if os.path.exists(project_config_path):
        try:
            project_config = load_project_config(project_config_path)
            workflow_commands = []
            setup_command = configured_setup_command(project_config)
            if setup_command is not None:
                workflow_commands.append("setup")
            workflow_commands.extend(name for name, _ in configured_workflow_commands(project_config))
            configured_hooks = sorted(workflow_hooks(project_config))
            readiness = workflow_readiness_policy(project_config)
            merge_policy = workflow_merge_policy(project_config)
            summary_parts = [f"valid: {project_config_path} ({len(project_config)} top-level key(s))"]
            if workflow_commands:
                summary_parts.append("commands=" + ", ".join(workflow_commands))
            if configured_hooks:
                summary_parts.append("hooks=" + ", ".join(configured_hooks))
            required_checks = readiness.get("required_checks")
            if isinstance(required_checks, list) and required_checks:
                summary_parts.append("required_checks=" + ", ".join(required_checks))
            if "required_approvals" in readiness:
                summary_parts.append(
                    f"required_approvals={int(readiness.get('required_approvals') or 0)}"
                )
            if readiness.get("require_mergeable"):
                summary_parts.append("require_mergeable=true")
            if merge_policy:
                merge_parts = []
                if "method" in merge_policy:
                    merge_parts.append(f"method={merge_policy['method']}")
                if "auto" in merge_policy:
                    merge_parts.append(f"auto={str(bool(merge_policy['auto'])).lower()}")
                if merge_parts:
                    summary_parts.append("merge=" + ", ".join(merge_parts))
            _doctor_record(checks, "PASS", "Project config", "; ".join(summary_parts))
        except Exception as exc:  # noqa: BLE001
            _doctor_record(checks, "FAIL", "Project config", str(exc))
    else:
        if explicit_project_config:
            _doctor_record(
                checks,
                "FAIL",
                "Project config",
                f"configured path does not exist: {project_config_path}",
            )
        else:
            _doctor_record(
                checks,
                "WARN",
                "Project config",
                f"optional config not found: {project_config_path}",
            )

    local_config_path = resolve_local_config_path(getattr(args, "local_config", None), target_dir)
    project_config_path = resolve_project_config_path(getattr(args, "project_config", None), target_dir)
    project_config: dict = {}

    if os.path.exists(project_config_path):
        try:
            project_config = load_project_config(project_config_path)
            workflow_commands = configured_setup_commands(project_config) + configured_workflow_commands(project_config)
            configured_hook_groups = configured_workflow_hooks(project_config)
            readiness_policy = workflow_readiness_policy(project_config)
            merge_policy = workflow_merge_policy(project_config)
            command_names = ", ".join(name for name, _ in workflow_commands) or "none"
            hook_names = ", ".join(
                sorted(name for name, values in configured_hook_groups.items() if values)
            ) or "none"
            readiness_parts = []
            required_checks = list(readiness_policy.get("required_checks") or [])
            if required_checks:
                readiness_parts.append("required_checks=" + ", ".join(required_checks))
            required_approvals = int(readiness_policy.get("required_approvals") or 0)
            if required_approvals > 0:
                readiness_parts.append(f"required_approvals={required_approvals}")
            if bool(readiness_policy.get("require_required_file_evidence")):
                readiness_parts.append("require_required_file_evidence=true")
            readiness_text = "; ".join(readiness_parts) if readiness_parts else "defaults"
            _doctor_record(
                checks,
                "PASS",
                "Project config",
                f"valid: {project_config_path} ({len(project_config)} top-level key(s))",
            )
            _doctor_record(
                checks,
                "PASS",
                "Workflow config",
                f"commands={command_names}; hooks={hook_names}; readiness={readiness_text}; auto_merge={merge_policy.get('auto')}; merge_method={merge_policy.get('method')}",
            )
        except Exception as exc:  # noqa: BLE001
            _doctor_record(checks, "FAIL", "Project config", str(exc))
    else:
        if explicit_project_config:
            _doctor_record(
                checks,
                "FAIL",
                "Project config",
                f"configured path does not exist: {project_config_path}",
            )
        else:
            _doctor_record(
                checks,
                "WARN",
                "Project config",
                f"optional config not found: {project_config_path}",
            )

    if os.path.exists(local_config_path):
        try:
            config = load_local_config(local_config_path)
            _doctor_record(
                checks,
                "PASS",
                "Local config",
                f"valid: {local_config_path} ({len(config)} key(s))",
            )
        except Exception as exc:  # noqa: BLE001
            _doctor_record(checks, "FAIL", "Local config", str(exc))
    else:
        if explicit_local_config:
            _doctor_record(
                checks,
                "FAIL",
                "Local config",
                f"configured path does not exist: {local_config_path}",
            )
        else:
            _doctor_record(
                checks,
                "WARN",
                "Local config",
                f"optional config not found: {local_config_path}",
            )

    if base_branch_mode == "current":
        try:
            stack_warnings = current_branch_stack_warnings()
        except Exception as exc:  # noqa: BLE001
            _doctor_record(checks, "FAIL", "Stacked branch sanity", str(exc))
        else:
            if stack_warnings:
                _doctor_record(
                    checks,
                    "WARN",
                    "Stacked branch sanity",
                    "; ".join(stack_warnings),
                )
            else:
                _doctor_record(
                    checks,
                    "PASS",
                    "Stacked branch sanity",
                    "upstream tracking looks healthy for --base current",
                )
    else:
        _doctor_record(
            checks,
            "PASS",
            "Stacked branch sanity",
            "not required (base mode is default)",
        )

    print()
    for check in checks:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")

    fail_count = sum(1 for check in checks if check["status"] == "FAIL")
    warn_count = sum(1 for check in checks if check["status"] == "WARN")
    pass_count = sum(1 for check in checks if check["status"] == "PASS")
    print()
    print(f"Doctor summary: {pass_count} pass, {warn_count} warn, {fail_count} fail")

    return 1 if fail_count > 0 else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch tracker items, coordinate code-host operations, and run an AI agent for each task body."
    )
    parser.add_argument(
        "--repo", help="Repository slug for the active code host. Defaults to the current authenticated repo context."
    )
    parser.add_argument(
        "--tracker",
        default=BUILTIN_DEFAULTS["tracker"],
        choices=sorted(TRACKER_CHOICES),
        help="Issue tracker to fetch from (default: github).",
    )
    parser.add_argument(
        "--codehost",
        default=BUILTIN_DEFAULTS["codehost"],
        choices=sorted(CODEHOST_CHOICES),
        help="Code host provider for PR/MR operations (default: github).",
    )
    parser.add_argument(
        "--issue",
        type=str,
        help="Process a single issue by number or key, ignoring --limit and --state.",
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
        "--preset",
        help=(
            "Named preset from project config. Presets can set runner/model/agent and "
            "basic retry or limit defaults before explicit CLI overrides are applied."
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
        "--token-budget",
        "--max-tokens",
        dest="token_budget",
        type=int,
        default=BUILTIN_DEFAULTS["token_budget"],
        help="Abort when cumulative tracked token usage exceeds this limit.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=BUILTIN_DEFAULTS["max_attempts"],
        help="Retry policy placeholder: maximum attempts before escalation or failure.",
    )
    parser.add_argument(
        "--escalate-to-preset",
        dest="escalate_to_preset",
        help="Preset to switch to on later attempts after failures.",
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
        "--track-tokens",
        action="store_true",
        help="Track token usage from runner output and include it in orchestration state.",
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
            "changes are committed to the target PR branch."
        ),
    )
    parser.add_argument(
        "--allow-pr-branch-switch",
        action="store_true",
        help=(
            "In --pr --from-review-comments mode, allow switching the current worktree "
            "to the target PR branch when it differs from the current branch."
        ),
    )
    parser.add_argument(
        "--isolate-worktree",
        action="store_true",
        help=(
            "Run PR review mode in a temporary git worktree bound to the target PR branch "
            "without switching your current branch."
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
        "--skip-if-pr-exists",
        dest="skip_if_pr_exists",
        action="store_true",
        default=BUILTIN_DEFAULTS["skip_if_pr_exists"],
        help=(
            "Skip issue processing when a linked open PR already exists. Enabled by default."
        ),
    )
    parser.add_argument(
        "--no-skip-if-pr-exists",
        dest="skip_if_pr_exists",
        action="store_false",
        help="Do not skip issue processing when a linked open PR exists.",
    )
    parser.add_argument(
        "--skip-if-branch-exists",
        dest="skip_if_branch_exists",
        action="store_true",
        default=BUILTIN_DEFAULTS["skip_if_branch_exists"],
        help=(
            "Skip issue processing when deterministic issue branch already exists on origin. "
            "Enabled by default."
        ),
    )
    parser.add_argument(
        "--no-skip-if-branch-exists",
        dest="skip_if_branch_exists",
        action="store_false",
        help="Do not skip issue processing when deterministic issue branch exists on origin.",
    )
    parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help=(
            "Override skip guards for existing linked PR and existing remote branch. "
            "Useful for intentional reruns."
        ),
    )
    parser.add_argument(
        "--conflict-recovery-only",
        action="store_true",
        help=(
            "Run reused-branch conflict recovery only: sync an existing issue or PR branch "
            "with base, push the result, and skip any agent work."
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
        "--base",
        "--base-branch",
        dest="base_branch",
        default=BUILTIN_DEFAULTS["base_branch"],
        choices=["default", "current"],
        help=(
            "Issue-flow base branch mode: 'default' uses repository default branch, "
            "'current' stacks new issue branches on the currently checked out branch."
        ),
    )
    parser.add_argument(
        "--decompose",
        default=BUILTIN_DEFAULTS["decompose"],
        choices=["auto", "never", "always"],
        help=(
            "Planning-only decomposition preflight for issue-flow: 'auto' proposes a plan "
            "for large tasks before agent execution, 'never' disables it, and 'always' "
            "forces a plan-only run."
        ),
    )
    parser.add_argument(
        "--create-child-issues",
        action="store_true",
        help=(
            "When a decomposed plan is approved in comments, create child issues "
            "from the plan (idempotent on re-runs)."
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
        "--project-config",
        help=(
            "Path to repository project JSON config. "
            "Defaults to project-config.json under --dir."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without running the agent."
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help=(
            "Enable autonomous batch selection behavior: recover state, respect claims, "
            "and continue linked PR tasks instead of treating batch mode as one-shot issue intake."
        ),
    )
    parser.add_argument(
        "--autonomous-session-file",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run environment diagnostics and exit without running any agent.",
    )
    parser.add_argument(
        "--doctor-smoke-check",
        action="store_true",
        help=(
            "In doctor mode, run a lightweight runner CLI smoke check "
            "(still does not start an agent run)."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "Print a concise orchestration status summary for --issue, --pr, or "
            "--autonomous-session-file and exit."
        ),
    )
    parser.add_argument(
        "--post-batch-verify",
        action="store_true",
        help=(
            "Run the repository post-batch verification path and exit, or run it automatically "
            "after an autonomous batch loop completes."
        ),
    )
    parser.add_argument(
        "--create-followup-issue",
        action="store_true",
        help="When post-batch verification fails, create a GitHub follow-up issue instead of only recommending one.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    bootstrap_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap_parser.add_argument("--dir", default=BUILTIN_DEFAULTS["dir"])
    bootstrap_parser.add_argument("--local-config")
    bootstrap_parser.add_argument("--project-config")
    bootstrap_parser.add_argument("--preset")
    bootstrap_parser.add_argument("--doctor", action="store_true")
    bootstrap_args, _ = bootstrap_parser.parse_known_args(argv)

    target_dir = os.path.abspath(bootstrap_args.dir)
    project_config_path = resolve_project_config_path(bootstrap_args.project_config, target_dir)
    local_config_path = resolve_local_config_path(bootstrap_args.local_config, target_dir)

    parser = build_parser()
    if not bootstrap_args.doctor:
        project_config = load_project_config(project_config_path)
        local_defaults = load_local_config(local_config_path)
        project_defaults = project_cli_defaults(project_config)
        project_preset = _as_optional_string(project_defaults.get("preset"))
        local_preset = _as_optional_string(local_defaults.get("preset"))
        cli_preset = _as_optional_string(getattr(bootstrap_args, "preset", None))

        parser.set_defaults(**without_keys(project_defaults, "preset"))
        parser.set_defaults(**preset_cli_defaults(project_config, project_preset))
        parser.set_defaults(**preset_cli_defaults(project_config, local_preset))
        parser.set_defaults(**without_keys(local_defaults, "preset"))
        parser.set_defaults(**preset_cli_defaults(project_config, cli_preset))
    parser.set_defaults(project_config=project_config_path)
    parser.set_defaults(local_config=local_config_path)
    return parser.parse_args(argv)


def _finish_main(exit_code: int, original_process_cwd: str) -> int:
    try:
        os.chdir(original_process_cwd)
    except FileNotFoundError:
        pass
    return exit_code


def main() -> int:
    raw_argv = sys.argv[1:]
    args = parse_args(raw_argv)
    original_process_cwd = os.getcwd()

    if bool(getattr(args, "doctor", False)):
        return _finish_main(run_doctor(args=args, raw_argv=raw_argv), original_process_cwd)

    issue_number_arg = getattr(args, "issue", None)
    tracker = _parse_tracker(getattr(args, "tracker", BUILTIN_DEFAULTS["tracker"]))
    pr_number_arg = getattr(args, "pr", None)
    status_mode = bool(getattr(args, "status", False))
    post_batch_verify_mode = bool(getattr(args, "post_batch_verify", False))
    create_followup_issue = bool(getattr(args, "create_followup_issue", False))
    from_review_comments = bool(getattr(args, "from_review_comments", False))
    force_issue_flow = bool(getattr(args, "force_issue_flow", False))
    conflict_recovery_only = bool(getattr(args, "conflict_recovery_only", False))
    skip_if_pr_exists = bool(getattr(args, "skip_if_pr_exists", False))
    skip_if_branch_exists = bool(getattr(args, "skip_if_branch_exists", False))
    force_reprocess = bool(getattr(args, "force_reprocess", False))
    pr_followup_branch_prefix = getattr(args, "pr_followup_branch_prefix", None)
    allow_pr_branch_switch = bool(getattr(args, "allow_pr_branch_switch", False))
    isolate_worktree = bool(getattr(args, "isolate_worktree", False))
    post_pr_summary = bool(getattr(args, "post_pr_summary", False))
    track_tokens = bool(getattr(args, "track_tokens", False))
    autonomous_mode = bool(getattr(args, "autonomous", False))
    autonomous_session_file = _as_optional_string(getattr(args, "autonomous_session_file", None))
    token_budget = getattr(args, "token_budget", BUILTIN_DEFAULTS["token_budget"])
    if token_budget is not None and (type(token_budget) is not int or token_budget <= 0):
        print("Error: --token-budget must be a positive integer", file=sys.stderr)
        return 1
    selected_preset = _as_optional_string(getattr(args, "preset", None))
    max_attempts = getattr(args, "max_attempts", BUILTIN_DEFAULTS["max_attempts"])
    if type(max_attempts) is not int or max_attempts <= 0:
        print("Error: --max-attempts must be a positive integer", file=sys.stderr)
        return 1
    escalate_to_preset = _as_optional_string(
        getattr(args, "escalate_to_preset", BUILTIN_DEFAULTS["escalate_to_preset"])
    )
    base_branch_mode = str(getattr(args, "base_branch", BUILTIN_DEFAULTS["base_branch"]))
    decompose_mode = str(getattr(args, "decompose", BUILTIN_DEFAULTS["decompose"]))
    create_child_issues = bool(getattr(args, "create_child_issues", BUILTIN_DEFAULTS["create_child_issues"]))

    if force_reprocess:
        skip_if_pr_exists = False
        skip_if_branch_exists = False

    try:
        target_dir = os.path.abspath(args.dir)
        if not os.path.isdir(target_dir):
            raise RuntimeError(f"--dir path does not exist or is not a directory: {target_dir}")
        if not os.path.isdir(os.path.join(target_dir, ".git")):
            raise RuntimeError(f"--dir path is not a git repository: {target_dir}")
        os.chdir(target_dir)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _finish_main(1, original_process_cwd)

    try:
        try:
            project_config_path = str(
                getattr(
                    args,
                    "project_config",
                    resolve_project_config_path(None, os.path.abspath(getattr(args, "dir", "."))),
                )
            )
            project_config = load_project_config(project_config_path)
            scope_defaults = project_scope_defaults(project_config)
            setup_command = configured_setup_command(project_config)
            workflow_checks = configured_workflow_commands(project_config)
            configured_hooks = configured_workflow_hooks(project_config)
            readiness_policy = workflow_readiness_policy(project_config)
            merge_policy = workflow_merge_policy(project_config)

            configured_command_names: list[str] = []
            if setup_command is not None:
                configured_command_names.append("setup")
            configured_command_names.extend(name for name, _ in workflow_checks)
            if configured_command_names:
                prefix = "[dry-run] " if args.dry_run else ""
                print(
                    f"{prefix}Configured workflow commands: "
                    + ", ".join(configured_command_names)
                )
            if configured_hooks:
                prefix = "[dry-run] " if args.dry_run else ""
                print(f"{prefix}Configured workflow hooks: {', '.join(sorted(configured_hooks))}")
            if readiness_policy:
                readiness_parts: list[str] = []
                required_checks = readiness_policy.get("required_checks")
                if isinstance(required_checks, list) and required_checks:
                    readiness_parts.append("required_checks=" + ", ".join(required_checks))
                if "required_approvals" in readiness_policy:
                    readiness_parts.append(
                        f"required_approvals={int(readiness_policy.get('required_approvals') or 0)}"
                    )
                if readiness_policy.get("require_review"):
                    readiness_parts.append("require_review=true")
                if readiness_policy.get("require_mergeable"):
                    readiness_parts.append("require_mergeable=true")
                if readiness_parts:
                    print("Readiness policy: " + "; ".join(readiness_parts))
            if merge_policy:
                merge_parts: list[str] = []
                if "method" in merge_policy:
                    merge_parts.append(f"method={merge_policy['method']}")
                if "auto" in merge_policy:
                    merge_parts.append(f"auto={str(bool(merge_policy['auto'])).lower()}")
                if merge_parts:
                    print("Merge policy: " + "; ".join(merge_parts))
            if selected_preset is not None:
                print(f"Selected preset: {selected_preset}")
            if max_attempts != BUILTIN_DEFAULTS["max_attempts"] or escalate_to_preset is not None:
                policy_text = f"Retry policy: max_attempts={max_attempts}"
                if escalate_to_preset is not None:
                    policy_text += f", escalate_to_preset={escalate_to_preset}"
                print(policy_text)

            if issue_number_arg is not None and pr_number_arg is not None:
                raise RuntimeError("Use either --issue or --pr, not both.")
            pr_mode_requested = pr_number_arg is not None or from_review_comments
            if post_batch_verify_mode and (issue_number_arg is not None or pr_mode_requested) and not autonomous_mode:
                raise RuntimeError("--post-batch-verify cannot be combined with --issue, --pr, or --from-review-comments.")
            if conflict_recovery_only and issue_number_arg is None and pr_number_arg is None:
                raise RuntimeError("--conflict-recovery-only requires --issue or --pr.")
            if from_review_comments and pr_number_arg is None:
                raise RuntimeError("--from-review-comments requires --pr <number>.")
            if pr_number_arg is not None and not from_review_comments:
                raise RuntimeError("--pr requires --from-review-comments.")
            codehost = _parse_codehost(getattr(args, "codehost", BUILTIN_DEFAULTS["codehost"]))
            validate_provider_requirements(
                tracker=tracker,
                codehost=codehost,
                pr_mode_requested=pr_mode_requested,
            )
            tracker_provider = resolve_tracker_provider(tracker)
            codehost_provider = resolve_codehost_provider(codehost)
            configure_active_providers(tracker_provider, codehost_provider)
            if issue_number_arg is not None:
                issue_number_arg = normalize_issue_number(issue_number_arg, tracker=tracker)
                setattr(args, "issue", issue_number_arg)

            if status_mode and pr_number_arg is not None and type(pr_number_arg) is not int:
                raise RuntimeError("--pr must be an integer pull request number")

            repo = args.repo or codehost_provider.detect_repo()
            if status_mode:
                return _finish_main(
                    run_status_command(args=args, repo=repo, merge_policy=merge_policy),
                    original_process_cwd,
                )
            if post_batch_verify_mode and not autonomous_mode:
                verification = run_post_batch_verification(
                    repo=repo,
                    tracker=tracker,
                    cwd=os.getcwd(),
                    dry_run=args.dry_run,
                    create_followup_issue=create_followup_issue,
                )
                print(f"Post-batch verification: {verification.get('summary')}")
                follow_up_issue = (
                    verification.get("follow_up_issue")
                    if isinstance(verification.get("follow_up_issue"), dict)
                    else None
                )
                if isinstance(follow_up_issue, dict) and str(follow_up_issue.get("status") or "") == "recommended":
                    print(f"Recommended follow-up issue: {follow_up_issue.get('title')}")
                if isinstance(follow_up_issue, dict) and str(follow_up_issue.get("status") or "") == "created":
                    issue_ref = _format_stored_issue_ref(follow_up_issue.get("issue_number")) or "issue"
                    print(
                        "Created follow-up issue: "
                        f"{issue_ref} {follow_up_issue.get('issue_url') or ''}".rstrip()
                    )
                exit_code = 1 if str(verification.get("status") or "") == "failed" else 0
                return _finish_main(exit_code, original_process_cwd)

            if not pr_mode_requested and base_branch_mode == "current":
                for warning in current_branch_stack_warnings():
                    print(f"Warning: {warning}", file=sys.stderr)

            ensure_clean_worktree()
            if setup_command is not None:
                failure_stage = "workflow_setup"
                _run_workflow_shell_command(
                    kind="command",
                    name="setup",
                    command_text=setup_command,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    env=os.environ.copy(),
                )
            if pr_mode_requested:
                base_branch = ""
                issues = []
            else:
                if base_branch_mode == "current":
                    base_branch = current_branch()
                else:
                    base_branch = codehost_provider.detect_default_branch(repo)
                mode_label = "[dry-run]" if args.dry_run else ""
                if base_branch_mode == "current":
                    if mode_label:
                        print(f"{mode_label} Selected current base branch: {base_branch}")
                        print(f"{mode_label} Base mode: current (stack on current branch: yes)")
                    else:
                        print(f"Selected current base branch: {base_branch}")
                        print("Base mode: current (stack on current branch: yes)")
                else:
                    if mode_label:
                        print(f"{mode_label} Selected stable base branch: {base_branch}")
                        print(f"{mode_label} Base mode: default (stack on current branch: no)")
                    else:
                        print(f"Selected stable base branch: {base_branch}")
                        print("Base mode: default (stack on current branch: no)")

                if issue_number_arg is not None:
                    issues = [tracker_provider.get_issue(repo=repo, issue_id=issue_number_arg)]
                else:
                    issues = tracker_provider.list_issues(repo=repo, state=args.state, limit=args.limit)
        except Exception:
            raise
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return _finish_main(1, original_process_cwd)

    if pr_mode_requested:
        try:
            failure_stage = "pr_review_init"
            pr_recovered_decomposition_rollup: dict | None = None
            pr_agent_run_stats: dict[str, object] | None = None
            workflow_check_results: list[dict] | None = None
            pr_runner = args.runner
            pr_agent = args.agent
            pr_model = args.model
            pr_attempt = 1
            original_cwd = os.getcwd()
            original_branch = current_branch()
            switched_branch = False
            isolated_worktree_path: str | None = None
            pr_state_context: dict[str, int | str | None] = {
                "issue": None,
                "pr": None,
                "branch": None,
                "base_branch": None,
            }
            pr_repo_root: str | None = None
            pr_attempt = 1
            if type(pr_number_arg) is not int:
                raise RuntimeError("--pr must be an integer pull request number")

            failure_stage = "fetch_pr"
            pull_request = current_codehost_provider().fetch_pull_request(repo=repo, number=pr_number_arg)
            pr_state_context["pr"] = pr_number_arg
            pr_state = str(pull_request.get("state") or "").strip().upper()
            if pr_state != "OPEN":
                print(f"PR #{pr_number_arg} is not open (state: {pr_state}); skipping.")
                return _finish_main(0, original_process_cwd)

            target_pr_branch = str(pull_request.get("headRefName") or "").strip()
            if not target_pr_branch:
                raise RuntimeError(
                    f"PR #{pr_number_arg} has empty headRefName; cannot select target branch"
                )

            prefix = "[dry-run] " if args.dry_run else ""
            print(f"{prefix}PR mode target branch: {target_pr_branch}")

            if isolate_worktree:
                print(
                    f"{prefix}PR mode execution: isolated worktree "
                    "(current branch will not be switched)"
                )
                isolated_worktree_path = create_isolated_worktree_for_branch(
                    branch_name=target_pr_branch,
                    dry_run=args.dry_run,
                )
                if isolated_worktree_path is not None:
                    os.chdir(isolated_worktree_path)
                    print(f"Using isolated worktree: {isolated_worktree_path}")
            else:
                if original_branch == target_pr_branch:
                    print(f"{prefix}PR mode execution: current branch already matches target")
                else:
                    print(
                        f"Warning: current branch '{original_branch}' differs from target PR "
                        f"branch '{target_pr_branch}'",
                        file=sys.stderr,
                    )
                    if not allow_pr_branch_switch:
                        raise RuntimeError(
                            "Refusing to modify another PR branch from current branch. "
                            "Use --allow-pr-branch-switch to switch branches in this worktree "
                            "or --isolate-worktree to run in a temporary worktree."
                        )
                    print(
                        f"{prefix}PR mode execution: switching worktree branch "
                        f"to '{target_pr_branch}'"
                    )

                checkout_pr_target_branch(branch_name=target_pr_branch, dry_run=args.dry_run)
                switched_branch = (not args.dry_run) and original_branch != target_pr_branch
            if not args.dry_run:
                pr_repo_root = current_repo_root()

            if conflict_recovery_only:
                target_base_branch = str(pull_request.get("baseRefName") or "").strip()
                if not target_base_branch:
                    raise RuntimeError(
                        f"PR #{pr_number_arg} has empty baseRefName; cannot run conflict recovery"
                    )
                if args.dry_run:
                    print(
                        f"[dry-run] Selected mode: conflict-recovery-only (PR #{pr_number_arg}; "
                        f"branch '{target_pr_branch}' -> base '{target_base_branch}')"
                    )
                else:
                    print(
                        f"Selected mode: conflict-recovery-only (PR #{pr_number_arg}; "
                        f"branch '{target_pr_branch}' -> base '{target_base_branch}')"
                    )
                try:
                    run_conflict_recovery_for_branch(
                        branch_name=target_pr_branch,
                        base_branch=target_base_branch,
                        strategy=args.sync_strategy,
                        dry_run=args.dry_run,
                        verify_recovered_branch=lambda _result: run_forced_recovery_verification(
                            branch_name=target_pr_branch,
                            project_config=project_config,
                            repo_dir=os.getcwd(),
                            dry_run=args.dry_run,
                        ),
                        expected_repo_root=pr_repo_root,
                    )
                except RuntimeError as exc:
                    print(
                        f"Conflict recovery result for branch '{target_pr_branch}': needs manual intervention ({exc})"
                    )
                    raise
                return _finish_main(0, original_process_cwd)

            recovered_pr_state: dict | None = None
            pr_clarification_answer: dict | None = None
            try:
                pr_comments = current_codehost_provider().list_pr_comments(
                    repo=repo,
                    pr_number=pr_number_arg,
                )
                recovered_pr_state, pr_state_warnings = select_latest_parseable_orchestration_state(
                    comments=pr_comments,
                    source_label=f"pr #{pr_number_arg}",
                )
                pr_clarification_answer = find_waiting_for_author_answer(
                    comments=pr_comments,
                    recovered_state=recovered_pr_state,
                    author_login=pr_author_login if "pr_author_login" in locals() else None,
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
                pr_recovered_decomposition_rollup = build_decomposition_rollup_from_recovered_state(
                    recovered_state=recovered_pr_state,
                    parent_issue=None,
                )

            failure_stage = "fetch_review_feedback"
            pull_request, review_items, review_stats = fetch_actionable_pr_review_feedback(
                repo=repo,
                pr_number=pr_number_arg,
                pull_request=pull_request,
            )
            reviews = pull_request.get("reviews")
            if not isinstance(reviews, list):
                reviews = []

            pr_author_payload = pull_request.get("author")
            pr_author_login = ""
            if isinstance(pr_author_payload, dict):
                pr_author_login = str(pr_author_payload.get("login") or "")
            if pr_clarification_answer is None:
                pr_clarification_answer = find_waiting_for_author_answer(
                    comments=pr_comments if "pr_comments" in locals() else [],
                    recovered_state=recovered_pr_state,
                    author_login=pr_author_login,
                )

            print(
                "Review prompt sources: "
                f"{format_review_filtering_stats(review_stats)}"
            )

            base_branch_for_run = target_pr_branch
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
            attempt = pr_attempt
            while True:
                pr_attempt = attempt
                ci_prompt_override: str | None = None
                linked_issues = current_codehost_provider().load_pr_linked_issue_context(
                    repo=repo,
                    pull_request=pull_request,
                )
                recovered_pr_status = ""
                if isinstance(recovered_pr_state, dict):
                    recovered_pr_status = str(recovered_pr_state.get("status") or "")

                if not review_items:
                    if recovered_pr_status in {"waiting-for-ci", "ready-to-merge"}:
                        current_attempt = orchestration_attempt_from_state(recovered_pr_state)
                        ci_status = wait_for_pr_ci_status(
                            repo=repo,
                            pull_request=pull_request,
                        )
                        ci_overall = str(ci_status.get("overall") or "")
                        failing_checks = ci_status.get("failing_checks")
                        failing_checks_list = (
                            failing_checks if isinstance(failing_checks, list) else []
                        )
                        pending_checks = ci_status.get("pending_checks")
                        pending_checks_count = (
                            len(pending_checks) if isinstance(pending_checks, list) else 0
                        )
                        ci_checks_payload = (
                            ci_status.get("checks") if isinstance(ci_status.get("checks"), list) else []
                        )

                        if ci_overall == "pending":
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
                                    runner=pr_runner,
                                    agent=pr_agent,
                                    model=pr_model,
                                    attempt=current_attempt,
                                    stage="ci_checks",
                                    next_action="wait_for_ci",
                                    error=None,
                                    ci_checks=ci_checks_payload,
                                    decomposition=pr_recovered_decomposition_rollup,
                                ),
                            )
                            print(
                                f"CI checks are still pending for PR #{pr_number_arg} "
                                f"({pending_checks_count} pending); keeping waiting-for-ci state."
                            )
                            return _finish_main(0, original_process_cwd)

                        if ci_overall == "failure":
                            failing_summary = format_failing_ci_checks_summary(failing_checks_list)
                            ci_diagnostics = collect_failing_ci_diagnostics(
                                repo=repo,
                                failing_checks=failing_checks_list,
                            )
                            diagnostics_summary = format_ci_diagnostics_summary(ci_diagnostics)
                            if str(ci_diagnostics.get("overall_classification") or "") == "transient":
                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="pr",
                                    target_number=pr_number_arg,
                                    dry_run=args.dry_run,
                                    state=build_orchestration_state(
                                        status="blocked",
                                        task_type="pr",
                                        issue_number=None,
                                        pr_number=pr_number_arg,
                                        branch=active_branch,
                                        base_branch=str(pr_state_context["base_branch"] or "") or None,
                                        runner=args.runner,
                                        agent=args.agent,
                                        model=args.model,
                                        attempt=current_attempt,
                                        stage="ci_checks",
                                        next_action="retry_ci_after_transient_failure",
                                        error=short_error_text(f"{failing_summary}; {diagnostics_summary}"),
                                        ci_checks=ci_checks_payload,
                                        ci_diagnostics=ci_diagnostics,
                                        decomposition=pr_recovered_decomposition_rollup,
                                    ),
                                )
                                print(
                                    f"PR #{pr_number_arg} CI failure looks transient: {diagnostics_summary}. "
                                    "Stopping without a code change retry."
                                )
                                return _finish_main(0, original_process_cwd)

                            if current_attempt >= max_attempts:
                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="pr",
                                    target_number=pr_number_arg,
                                    dry_run=args.dry_run,
                                    state=build_orchestration_state(
                                        status="blocked",
                                        task_type="pr",
                                        issue_number=None,
                                        pr_number=pr_number_arg,
                                        branch=active_branch,
                                        base_branch=str(pr_state_context["base_branch"] or "") or None,
                                        runner=args.runner,
                                        agent=args.agent,
                                        model=args.model,
                                        attempt=current_attempt,
                                        stage="ci_checks",
                                        next_action="manual_ci_fix_required",
                                        error=short_error_text(
                                            f"{failing_summary}; retry limit reached at attempt {current_attempt}/{max_attempts}"
                                        ),
                                        ci_checks=ci_checks_payload,
                                        ci_diagnostics=ci_diagnostics,
                                        decomposition=pr_recovered_decomposition_rollup,
                                    ),
                                )
                                print(
                                    f"PR #{pr_number_arg} has failing CI checks after {current_attempt}/{max_attempts} "
                                    f"attempts: {diagnostics_summary}"
                                )
                                return _finish_main(0, original_process_cwd)

                            retry_attempt = current_attempt + 1
                            attempt = retry_attempt
                            ci_prompt_override = build_ci_failure_prompt(
                                pull_request=pull_request,
                                failing_checks=failing_checks_list,
                                ci_diagnostics=ci_diagnostics,
                                linked_issues=linked_issues,
                            )
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
                                    attempt=retry_attempt,
                                    stage="ci_checks",
                                    next_action="run_ci_fix_agent",
                                    error=short_error_text(f"{failing_summary}; {diagnostics_summary}"),
                                    ci_checks=ci_checks_payload,
                                    ci_diagnostics=ci_diagnostics,
                                    decomposition=pr_recovered_decomposition_rollup,
                                ),
                            )
                            print(
                                f"PR #{pr_number_arg} has failing CI checks: {diagnostics_summary}. "
                                f"Running CI fix attempt {retry_attempt}/{max_attempts}."
                            )
                        elif ci_overall == "success":
                            finalize_pr_after_ci_success(
                                repo=repo,
                                pr_number=pr_number_arg,
                                linked_issues=linked_issues,
                                merge_policy=merge_policy,
                                target_type="pr",
                                target_number=pr_number_arg,
                                issue_number=None,
                                branch=active_branch,
                                base_branch=str(pr_state_context["base_branch"] or "") or None,
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=current_attempt,
                                ci_checks=ci_checks_payload,
                                decomposition=pr_recovered_decomposition_rollup,
                                project_config=project_config,
                                repo_dir=os.getcwd(),
                                dry_run=args.dry_run,
                            )
                            return _finish_main(0, original_process_cwd)
                    elif recovered_pr_status == "ready-for-review":
                        print(
                            f"No actionable review comments found for PR #{pr_number_arg}; "
                            f"keeping recovered state '{recovered_pr_status}'."
                        )
                        return _finish_main(0, original_process_cwd)
                    else:
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
                                attempt=attempt,
                                stage="review_feedback",
                                next_action="await_new_review_comments",
                                error="No actionable review comments found",
                                decomposition=pr_recovered_decomposition_rollup,
                            ),
                        )
                        print(
                            "No actionable review comments found "
                            f"for PR #{pr_number_arg}; nothing to do."
                        )
                        return _finish_main(0, original_process_cwd)

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
                        attempt=attempt,
                        stage="agent_run",
                        next_action="wait_for_agent_result",
                        error=None,
                        decomposition=pr_recovered_decomposition_rollup,
                    ),
                )

                prompt = (
                    ci_prompt_override
                    if ci_prompt_override is not None
                    else build_pr_review_prompt(
                        pull_request=pull_request,
                        review_items=review_items,
                        linked_issues=linked_issues,
                    )
                )
                if (
                    recovered_pr_state is not None
                    and str(recovered_pr_state.get("status") or "") == "failed"
                ):
                    prompt = append_recovered_context_to_prompt(
                        prompt,
                        build_recovered_failure_context_note(recovered_pr_state),
                    )
                elif (
                    recovered_pr_state is not None
                    and str(recovered_pr_state.get("status") or "") == "waiting-for-author"
                    and pr_clarification_answer is not None
                ):
                    prompt = append_recovered_context_to_prompt(
                        prompt,
                        build_clarification_context_note(recovered_pr_state, pr_clarification_answer),
                    )

                failure_stage = "agent_run"
                pre_run_untracked_files: set[str] | None = None
                if not args.dry_run:
                    pre_run_untracked_files = list_untracked_files()
                pr_hook_env = build_workflow_hook_env(
                    repo=repo,
                    mode="pr-review",
                    issue_number=None,
                    pr_number=pr_number_arg,
                    branch=active_branch,
                    base_branch=str(pr_state_context["base_branch"] or "") or None,
                )
                failure_stage = "workflow_hooks"
                run_configured_workflow_hooks(
                    hook_name="pre_agent",
                    configured_hooks=configured_hooks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    env=pr_hook_env,
                )
                failure_stage = "agent_run"
                pr_agent_run_stats = {}
                pr_agent_result: dict[str, object] = {}
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
                    track_tokens=track_tokens,
                    token_budget=token_budget,
                    run_stats=pr_agent_run_stats,
                    agent_result=pr_agent_result,
                    cwd=pr_repo_root,
                    expected_branch=active_branch,
                    expected_repo_root=pr_repo_root,
                )
                if exit_code != 0:
                    exit_summary = describe_exit_code(exit_code)
                    diagnosis = (
                        classify_opencode_failure(return_code=exit_code, model=args.model)
                        if args.runner == "opencode"
                        else None
                    )
                    message = (
                        f"Agent failed for PR #{pr_number_arg} with {exit_summary}"
                        + (f" ({diagnosis})" if diagnosis else "")
                    )
                    raise RuntimeError(message)

                failure_stage = "workflow_hooks"
                run_configured_workflow_hooks(
                    hook_name="post_agent",
                    configured_hooks=configured_hooks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    env=pr_hook_env,
                )

                clarification_request = pr_agent_result.get("clarification_request")
                if isinstance(clarification_request, dict):
                    question = str(clarification_request.get("question") or "").strip()
                    reason = _as_optional_string(clarification_request.get("reason"))
                    if question:
                        safe_post_clarification_request_comment(
                            repo=repo,
                            target_type="pr",
                            target_number=pr_number_arg,
                            question=question,
                            reason=reason,
                            dry_run=args.dry_run,
                        )
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
                                attempt=attempt,
                                stage="agent_run",
                                next_action="await_author_reply",
                                error=reason or question,
                                stats=pr_agent_run_stats,
                                decomposition=pr_recovered_decomposition_rollup,
                            )
                            | {"question": question, "reason": reason},
                        )
                        print(f"Paused PR #{pr_number_arg} for author clarification: {question}")
                        return _finish_main(0, original_process_cwd)

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
                            attempt=attempt,
                            stage="post_agent_check",
                            next_action="await_more_feedback_or_manual_changes",
                            error="Agent produced no repository changes",
                            stats=pr_agent_run_stats,
                            decomposition=pr_recovered_decomposition_rollup,
                        ),
                    )
                    print(f"No changes detected for PR #{pr_number_arg}; skipping commit and push")
                    return _finish_main(0, original_process_cwd)

                failure_stage = "commit_push"
                commit_pr_review_changes(
                    pull_request=pull_request,
                    dry_run=args.dry_run,
                    pre_run_untracked_files=pre_run_untracked_files,
                    expected_branch=active_branch,
                    expected_repo_root=pr_repo_root,
                )

                failure_stage = "workflow_checks"
                workflow_check_results = run_configured_workflow_checks(
                    checks=workflow_checks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                )

                failure_stage = "workflow_hooks"
                run_configured_workflow_hooks(
                    hook_name="pre_pr_update",
                    configured_hooks=configured_hooks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    env=pr_hook_env,
                )

                failure_stage = "commit_push"
                push_branch(
                    branch_name=active_branch,
                    dry_run=args.dry_run,
                    expected_repo_root=pr_repo_root,
                )
                if pr_followup_branch_prefix:
                    print(f"Pushed follow-up branch for PR #{pr_number_arg}: {active_branch}")

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
                        attempt=attempt,
                        stage="pr_update",
                        next_action="wait_for_ci",
                        error=None,
                        stats=pr_agent_run_stats,
                        decomposition=pr_recovered_decomposition_rollup,
                    ),
                )

                failure_stage = "workflow_hooks"
                run_configured_workflow_hooks(
                    hook_name="post_pr_update",
                    configured_hooks=configured_hooks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    env=pr_hook_env,
                )

                if post_pr_summary:
                    leave_pr_summary_comment(
                        repo=repo,
                        pr_number=pr_number_arg,
                        review_items_count=len(review_items),
                        dry_run=args.dry_run,
                    )

                failure_stage = "fetch_review_feedback"
                pull_request, review_items, review_stats = fetch_actionable_pr_review_feedback(
                    repo=repo,
                    pr_number=pr_number_arg,
                )
                print(
                    "Review prompt sources: "
                    f"{format_review_filtering_stats(review_stats)}"
                )

                if not review_items:
                    print(
                        f"Done. Processed PR #{pr_number_arg} with no remaining actionable review items after attempt {attempt}."
                    )
                    return _finish_main(0, original_process_cwd)

                if attempt >= max_attempts:
                    safe_post_orchestration_state_comment(
                        repo=repo,
                        target_type="pr",
                        target_number=pr_number_arg,
                        dry_run=args.dry_run,
                        state=build_orchestration_state(
                            status="blocked",
                            task_type="pr",
                            issue_number=None,
                            pr_number=pr_number_arg,
                            branch=active_branch,
                            base_branch=str(pr_state_context["base_branch"] or "") or None,
                            runner=args.runner,
                            agent=args.agent,
                            model=args.model,
                            attempt=attempt,
                            stage="review_feedback",
                            next_action="manual_review_follow_up_required",
                            error=short_error_text(
                                f"{len(review_items)} actionable review items remain after {attempt}/{max_attempts} attempts"
                            ),
                            stats=pr_agent_run_stats,
                            decomposition=pr_recovered_decomposition_rollup,
                        ),
                    )
                    print(
                        f"PR #{pr_number_arg} still has {len(review_items)} actionable review items "
                        f"after {attempt}/{max_attempts} attempts; blocking for manual follow-up."
                    )
                    return _finish_main(0, original_process_cwd)

                print(
                    f"PR #{pr_number_arg} still has {len(review_items)} actionable review items after attempt {attempt}; "
                    f"continuing review feedback loop ({attempt + 1}/{max_attempts})."
                )
                attempt += 1
                recovered_pr_state = None
                pr_clarification_answer = None
        except Exception as exc:  # noqa: BLE001
            if pr_number_arg is not None:
                failed_pr_number = pr_state_context.get("pr")
                if type(failed_pr_number) is int:
                    if isinstance(exc, ResidualUntrackedFilesError):
                        failure_stage = "residual_untracked_validation"
                    elif isinstance(exc, BranchContextMismatchError):
                        failure_stage = "branch_context_validation"
                    elif isinstance(exc, TokenBudgetExceededError):
                        failure_stage = "token_budget"
                    elif isinstance(exc, CostBudgetExceededError):
                        failure_stage = "cost_budget"

                    failure_status = failure_state_for_stage(failure_stage)
                    next_action = failure_next_action_for_stage(failure_stage)
                    workflow_results = exc.checks if isinstance(exc, WorkflowCheckFailure) else None
                    if workflow_results is None and isinstance(exc, WorkflowHookFailure):
                        workflow_results = exc.hooks
                    residual_untracked_files = (
                        exc.files if isinstance(exc, ResidualUntrackedFilesError) else None
                    )
                    safe_post_orchestration_state_comment(
                        repo=repo,
                        target_type="pr",
                        target_number=failed_pr_number,
                        dry_run=args.dry_run,
                        state=build_orchestration_state(
                            status=failure_status,
                            task_type="pr",
                            issue_number=None,
                            pr_number=failed_pr_number,
                            branch=str(pr_state_context.get("branch") or "") or None,
                            base_branch=str(pr_state_context.get("base_branch") or "") or None,
                            runner=locals().get("pr_runner", args.runner),
                            agent=locals().get("pr_agent", args.agent),
                            model=locals().get("pr_model", args.model),
                            attempt=pr_attempt,
                            stage=failure_stage,
                            next_action=next_action,
                            error=short_error_text(str(exc)),
                            workflow_checks=workflow_results,
                            residual_untracked_files=residual_untracked_files,
                            stats=pr_agent_run_stats,
                            decomposition=pr_recovered_decomposition_rollup,
                        ),
                    )
            if configured_hooks:
                failure_stage = "workflow_hooks"
                run_configured_workflow_hooks(
                    hook_name="post_pr_update",
                    configured_hooks=configured_hooks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    context=hook_context,
                )
                failure_stage = "commit_push"
            print(f"Error: {exc}", file=sys.stderr)
            return _finish_main(1, original_process_cwd)
        finally:
            if isolated_worktree_path is not None:
                os.chdir(original_cwd)
                if not args.dry_run:
                    try:
                        remove_isolated_worktree(isolated_worktree_path)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        print(
                            f"Warning: failed to remove isolated worktree '{isolated_worktree_path}': "
                            f"{cleanup_exc}",
                            file=sys.stderr,
                        )
            elif switched_branch:
                try:
                    run_command(["git", "checkout", original_branch])
                except Exception as restore_exc:  # noqa: BLE001
                    print(
                        f"Warning: failed to restore original branch '{original_branch}': "
                        f"{restore_exc}",
                        file=sys.stderr,
                    )

    if not issues:
        print("No issues found.")
        return _finish_main(0, original_process_cwd)

    if autonomous_mode and not pr_mode_requested and issue_number_arg is None:
        issues = sort_autonomous_issues(issues=issues, scope_defaults=scope_defaults, repo=repo)

    autonomous_session_state = load_autonomous_session_state(autonomous_session_file)
    blocked_dependency_entries: list[dict] = []
    if autonomous_mode and issue_number_arg is None:
        issues, skipped_session_issues = filter_autonomous_issues_for_single_pass(
            issues=issues,
            session_state=autonomous_session_state,
        )
        if skipped_session_issues:
            skipped_labels = ", ".join(f"#{issue_number}" for issue_number in skipped_session_issues)
            print(
                "Skipping previously processed issues for this daemon invocation: "
                f"{skipped_labels}"
            )
        issues, blocked_dependency_entries = split_autonomous_issues_by_dependency_state(
            repo=repo,
            issues=issues,
        )
        if blocked_dependency_entries:
            print("Skipping blocked issues for this daemon invocation:")
            for blocked_entry in blocked_dependency_entries:
                print(f"- {format_autonomous_dependency_blocker(blocked_entry)}")

    run_id = generate_run_id()
    failures = 0
    processed = 0
    skipped_existing_pr = 0
    skipped_existing_branch = 0
    skipped_blocked_dependencies = len(blocked_dependency_entries)
    skipped_out_of_scope = 0
    touched_prs: list[str] = []
    reported_issue_failures: set[int] = set()
    blocked_dependency_summaries = [
        format_autonomous_dependency_blocker(blocked_entry) for blocked_entry in blocked_dependency_entries
    ]

    post_batch_verification: dict[str, object] | None = None
    if autonomous_mode and issue_number_arg is None and post_batch_verify_mode:
        post_batch_verification = run_post_batch_verification(
            repo=repo,
            tracker=tracker,
            cwd=os.getcwd(),
            dry_run=args.dry_run,
            create_followup_issue=create_followup_issue,
            touched_prs=touched_prs,
        )
        print(f"Post-batch verification: {post_batch_verification.get('summary')}")
        follow_up_issue = (
            post_batch_verification.get("follow_up_issue")
            if isinstance(post_batch_verification.get("follow_up_issue"), dict)
            else None
        )
        if isinstance(follow_up_issue, dict) and str(follow_up_issue.get("status") or "") == "recommended":
            print(f"Recommended follow-up issue: {follow_up_issue.get('title')}")
        if isinstance(follow_up_issue, dict) and str(follow_up_issue.get("status") or "") == "created":
            issue_ref = _format_stored_issue_ref(follow_up_issue.get("issue_number")) or "issue"
            print(
                "Created follow-up issue: "
                f"{issue_ref} {follow_up_issue.get('issue_url') or ''}".rstrip()
            )

    if autonomous_mode and issue_number_arg is None:
        final_issue_pr_actions = (
            [f"Touched {len(touched_prs)} PR(s)"] if touched_prs else ["No PR updates were needed in the last batch"]
        )
        final_blockers = [f"{failures} batch failure(s) need follow-up"] if failures > 0 else []
        if isinstance(post_batch_verification, dict):
            verification_status = str(post_batch_verification.get("status") or "")
            if verification_status == "passed":
                final_issue_pr_actions.append("Post-batch verification passed")
            elif verification_status == "failed":
                final_blockers.append(
                    _as_optional_string(post_batch_verification.get("summary")) or "post-batch verification failed"
                )
                follow_up_issue = (
                    post_batch_verification.get("follow_up_issue")
                    if isinstance(post_batch_verification.get("follow_up_issue"), dict)
                    else None
                )
                if isinstance(follow_up_issue, dict):
                    follow_up_status = str(follow_up_issue.get("status") or "")
                    if follow_up_status == "created":
                        final_issue_pr_actions.append(
                            f"Created verification follow-up issue #{follow_up_issue.get('issue_number')}"
                        )
                    elif follow_up_status == "recommended":
                        final_issue_pr_actions.append("Recommended a verification follow-up issue")
        update_autonomous_session_checkpoint(
            autonomous_session_state,
            run_id=run_id,
            phase="running",
            batch_index=0,
            total_batches=len(issues),
            counts={
                "processed": processed,
                "failures": failures,
                "skipped_existing_pr": skipped_existing_pr,
                "skipped_existing_branch": skipped_existing_branch,
                "skipped_blocked_dependencies": skipped_blocked_dependencies,
                "skipped_out_of_scope": skipped_out_of_scope,
            },
            done=[
                f"Loaded autonomous queue with {len(issues)} runnable issue(s)",
                *(
                    [f"Skipped {skipped_blocked_dependencies} dependency-blocked issue(s)"]
                    if skipped_blocked_dependencies > 0
                    else []
                ),
            ],
            current="Idle between autonomous batches",
            next_items=preview_autonomous_issue_queue(issues, start_index=0),
            issue_pr_actions=[],
            in_progress=[],
            blockers=blocked_dependency_summaries,
            next_checkpoint="when batch 1 starts",
        )
        save_autonomous_session_state(autonomous_session_file, autonomous_session_state)
        print(format_autonomous_session_status_summary(autonomous_session_state))

    for batch_index, issue in enumerate(issues, start=1):
        try:
            failure_stage = "issue_setup"
            batch_issue_number = issue["number"]
            claim_acquired = False
            workflow_check_results: list[dict] | None = None
            linked_open_pr: dict | None = None
            recovered_state: dict | None = None
            recovered_status = ""
            mode = "issue-flow"
            mode_reason = "batch issue processing"
            force_override_applied = False
            skip_agent_run = False
            supports_issue_tracker_ops = True
            issue_label = format_issue_label_from_issue(issue)
            issue_branch = branch_name_for_issue(issue=issue, prefix=args.branch_prefix)
            state_target_type = "issue"
            state_target_number = issue["number"]
            state_pr_number: int | None = None
            decomposition_rollup: dict | None = None
            decomposition_parent_issue: dict | None = None
            decomposition_parent_branch: str | None = None
            decomposition_parent_payload: dict | None = None
            decomposition_child_note: str | None = None
            selected_decomposition_child = False
            issue_agent_run_stats: dict[str, object] | None = None
            issue_repo_root: str | None = None
            state_attempt = 1
            orchestration_attempt = 1
            active_attempt = state_attempt
            active_runner = str(args.runner)
            active_agent = str(args.agent)
            active_model = args.model
            supports_github_issue_ops = issue_tracker(issue) == TRACKER_GITHUB and type(issue["number"]) is int
            decomposition_assessment = assess_issue_decomposition_need(issue)
            batch_done_summary = f"Started batch {batch_index}/{len(issues)} for {issue_label}"
            batch_current_summary = f"Batch {batch_index}/{len(issues)} running for {issue_label}"
            batch_action_items = [f"Inspect {issue_label} and choose issue-flow or PR-review path"]
            batch_in_progress_items = [f"autonomous batch {batch_index}/{len(issues)} for {issue_label}"]
            batch_blockers: list[str] = []

            if autonomous_mode and issue_number_arg is None:
                update_autonomous_session_checkpoint(
                    autonomous_session_state,
                    run_id=run_id,
                    phase="running",
                    batch_index=batch_index,
                    total_batches=len(issues),
                    counts={
                        "processed": processed,
                        "failures": failures,
                        "skipped_existing_pr": skipped_existing_pr,
                        "skipped_existing_branch": skipped_existing_branch,
                        "skipped_blocked_dependencies": skipped_blocked_dependencies,
                        "skipped_out_of_scope": skipped_out_of_scope,
                    },
                    done=[batch_done_summary],
                    current=batch_current_summary,
                    next_items=preview_autonomous_issue_queue(issues, start_index=batch_index),
                    issue_pr_actions=batch_action_items,
                    in_progress=batch_in_progress_items,
                    blockers=blocked_dependency_summaries + batch_blockers,
                    next_checkpoint=f"after batch {batch_index}/{len(issues)} finishes",
                )
                save_autonomous_session_state(autonomous_session_file, autonomous_session_state)

            scope_decision = evaluate_issue_scope(issue=issue, scope_defaults=scope_defaults)
            scope_eligible = bool(scope_decision.get("eligible", True))
            scope_reason = str(scope_decision.get("reason") or "scope rules passed")
            scope_prefix = "[dry-run] " if args.dry_run else ""
            print(
                f"{scope_prefix}Scope decision for {issue_label}: "
                f"{'eligible' if scope_eligible else 'out-of-scope'} ({scope_reason})"
            )

            if not scope_eligible:
                if force_reprocess:
                    print(
                        f"Continuing {issue_label} despite out-of-scope decision "
                        "because --force-reprocess is set."
                    )
                    if supports_issue_tracker_ops:
                        safe_post_issue_scope_skip_comment(
                            repo=repo,
                            issue_number=issue["number"],
                            reason=scope_reason,
                            forced=True,
                            dry_run=args.dry_run,
                        )
                else:
                    skipped_out_of_scope += 1
                    batch_done_summary = f"Skipped {issue_label} as out-of-scope"
                    batch_current_summary = f"Batch {batch_index}/{len(issues)} paused on scope gating for {issue_label}"
                    batch_action_items = [f"Posted blocked state and scope decision for {issue_label}"]
                    batch_blockers = [scope_reason]
                    if supports_issue_tracker_ops:
                        safe_post_orchestration_state_comment(
                            repo=repo,
                            target_type="issue",
                            target_number=issue["number"],
                            dry_run=args.dry_run,
                            state=build_orchestration_state(
                                status="blocked",
                                task_type="issue",
                                issue_number=issue["number"],
                                pr_number=None,
                                branch=issue_branch,
                                base_branch=base_branch if base_branch else None,
                                runner=active_runner,
                                agent=active_agent,
                                model=active_model,
                                attempt=active_attempt,
                                stage="scope_check",
                                next_action="adjust_scope_or_force_reprocess",
                                error=short_error_text(scope_reason),
                            ),
                        )
                        safe_post_issue_scope_skip_comment(
                            repo=repo,
                            issue_number=issue["number"],
                            reason=scope_reason,
                            forced=False,
                            dry_run=args.dry_run,
                        )
                    print(
                        f"Skipping {issue_label}: out-of-scope for autonomous run "
                        "(--force-reprocess to override)."
                    )
                    continue

            if skip_if_pr_exists and not autonomous_mode:
                linked_open_pr = current_codehost_provider().find_open_pr_for_issue(repo=repo, issue=issue)
                if linked_open_pr is not None:
                    if issue_number_arg is not None:
                        linked_pr_number = linked_open_pr.get("number")
                        linked_pr_url = str(linked_open_pr.get("url") or "").strip()
                        linked_pr_context = (
                            f"PR #{linked_pr_number}"
                            if type(linked_pr_number) is int
                            else "a linked open PR"
                        )
                        if linked_pr_url:
                            linked_pr_context = f"{linked_pr_context} ({linked_pr_url})"
                        print(
                            f"Found linked open PR for {issue_label}: {linked_pr_context}; "
                            "skipping duplicate issue-flow and evaluating PR-review/recovery path."
                        )
                    else:
                        skipped_existing_pr += 1
                        batch_done_summary = f"Skipped {issue_label} because a linked PR already exists"
                        batch_current_summary = f"Batch {batch_index}/{len(issues)} completed without issue-flow for {issue_label}"
                        batch_action_items = [f"Left linked PR in place for {issue_label}"]
                        linked_pr_number = linked_open_pr.get("number")
                        linked_pr_url = str(linked_open_pr.get("url") or "").strip()
                        linked_pr_context = (
                            f"PR #{linked_pr_number}"
                            if type(linked_pr_number) is int
                            else "a linked open PR"
                        )
                        if linked_pr_url:
                            linked_pr_context = f"{linked_pr_context} ({linked_pr_url})"
                        print(
                            f"Skipping {issue_label}: {linked_pr_context} already exists "
                            "(--force-reprocess or --no-skip-if-pr-exists to override)."
                        )
                        continue

            if skip_if_branch_exists and remote_branch_exists(issue_branch):
                if issue_number_arg is not None:
                    print(
                        f"Found existing remote branch for {issue_label}: '{issue_branch}'; "
                        "continuing so the single-issue run can reuse that branch context."
                    )
                else:
                    skipped_existing_branch += 1
                    batch_done_summary = f"Skipped {issue_label} because branch '{issue_branch}' already exists"
                    batch_current_summary = f"Batch {batch_index}/{len(issues)} completed without branch reuse for {issue_label}"
                    batch_action_items = [f"Did not reuse existing branch '{issue_branch}' for {issue_label}"]
                    print(
                        f"Skipping {issue_label}: branch '{issue_branch}' already exists on origin "
                        "(--force-reprocess or --no-skip-if-branch-exists to override)."
                    )
                    continue

            if issue_number_arg is not None or autonomous_mode:
                if linked_open_pr is None:
                    linked_open_pr = current_codehost_provider().find_open_pr_for_issue(repo=repo, issue=issue)

                recovered_issue_state: dict | None = None
                issue_comments: list[dict] = []
                try:
                    issue_comments = current_tracker_provider().list_issue_comments(repo=repo, issue_id=issue["number"])
                    (
                        recovered_issue_state,
                        issue_state_warnings,
                    ) = select_latest_parseable_orchestration_state(
                        comments=issue_comments,
                        source_label=issue_label,
                    )
                    for warning in issue_state_warnings:
                        print(f"Warning: {warning}", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001
                    print(
                        "Warning: unable to recover orchestration state from "
                        f"{issue_label} comments: {exc}",
                        file=sys.stderr,
                    )

                recovered_pr_state: dict | None = None
                if linked_open_pr is not None:
                    linked_pr_number = linked_open_pr.get("number")
                    if type(linked_pr_number) is int:
                        try:
                            linked_pr_comments = current_codehost_provider().list_pr_comments(
                                repo=repo,
                                pr_number=linked_pr_number,
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
                clarification_answer: dict | None = None
                if recovered_issue_state is not None:
                    clarification_answer = find_waiting_for_author_answer(
                        comments=issue_comments,
                        recovered_state=recovered_issue_state,
                        author_login=_issue_author_login(issue),
                    )
                decomposition_rollup = build_decomposition_rollup_from_recovered_state(
                    recovered_state=recovered_state,
                    parent_issue=issue["number"],
                )
                if recovered_state is not None:
                    prefix = "[dry-run] " if args.dry_run else ""
                    print(
                        f"{prefix}Recovered orchestration state context: "
                        f"{format_recovered_state_context(recovered_state)}"
                    )
                if clarification_answer is not None:
                    print(
                        f"Found author clarification reply for {issue_label}: "
                        f"{clarification_answer.get('body')}"
                    )

                if recovered_state is not None:
                    recovered_status = str(recovered_state.get("status") or "")
                    orchestration_attempt = next_orchestration_attempt(recovered_state)
                if recovered_status in {"waiting-for-author", "blocked"} and force_issue_flow:
                    force_override_applied = True
                    print(
                        f"Recovered state is {recovered_status}, but continuing because "
                        "--force-issue-flow is set."
                    )

                mode, mode_reason = choose_execution_mode(
                    issue_number=issue["number"],
                    linked_open_pr=linked_open_pr,
                    force_issue_flow=force_issue_flow,
                    recovered_state=recovered_state,
                    clarification_answer=clarification_answer,
                )
                if mode == "skip":
                    batch_done_summary = f"Skipped {issue_label}: {mode_reason}"
                    batch_current_summary = f"Batch {batch_index}/{len(issues)} paused for {issue_label}"
                    batch_action_items = [f"Recovery state kept {issue_label} out of the autonomous batch"]
                    batch_blockers = [mode_reason]
                    print(
                        f"Skipping {issue_label}: {mode_reason} "
                        "(use --force-issue-flow to override)."
                    )
                    continue

                if conflict_recovery_only:
                    recovery_branch = issue_branch
                    recovery_base_branch = base_branch
                    recovery_label = issue_label
                    if mode == "pr-review":
                        if linked_open_pr is None:
                            raise RuntimeError(
                                f"Internal error: PR-review mode selected without linked PR for {issue_label}"
                            )
                        linked_pr_number = linked_open_pr.get("number")
                        recovery_branch = str(linked_open_pr.get("headRefName") or "").strip()
                        recovery_base_branch = str(linked_open_pr.get("baseRefName") or base_branch).strip()
                        if not recovery_branch:
                            raise RuntimeError(
                                f"Linked PR for {issue_label} has empty headRefName; cannot run conflict recovery"
                            )
                        if not recovery_base_branch:
                            raise RuntimeError(
                                f"Linked PR for {issue_label} has empty baseRefName; cannot run conflict recovery"
                            )
                        recovery_label = (
                            f"PR #{linked_pr_number}" if type(linked_pr_number) is int else issue_label
                        )

                    selected_mode_text = (
                        f"[dry-run] Selected mode: conflict-recovery-only (reason: {mode_reason}; "
                        f"target: {recovery_label}; branch '{recovery_branch}' -> base '{recovery_base_branch}')"
                        if args.dry_run
                        else f"Selected mode: conflict-recovery-only (reason: {mode_reason}; "
                        f"target: {recovery_label}; branch '{recovery_branch}' -> base '{recovery_base_branch}')"
                    )
                    print(selected_mode_text)

                    failure_stage = "prepare_branch"
                    branch_status = prepare_issue_branch(
                        base_branch=recovery_base_branch,
                        branch_name=recovery_branch,
                        dry_run=args.dry_run,
                        fail_on_existing=args.fail_on_existing,
                    )
                    if not args.dry_run:
                        issue_repo_root = current_repo_root()
                    print(f"Branch status for {recovery_label}: {branch_status}")
                    if branch_status != "reused":
                        raise RuntimeError(
                            f"Conflict recovery only requires an existing branch, but '{recovery_branch}' "
                            "was not found locally or on origin. Run the normal issue/PR flow first."
                        )

                    failure_stage = "sync_branch"
                    try:
                        run_conflict_recovery_for_branch(
                            branch_name=recovery_branch,
                            base_branch=recovery_base_branch,
                            strategy=args.sync_strategy,
                            dry_run=args.dry_run,
                            verify_recovered_branch=lambda _result: run_forced_recovery_verification(
                                branch_name=recovery_branch,
                                project_config=project_config,
                                repo_dir=os.getcwd(),
                                dry_run=args.dry_run,
                            ),
                            expected_repo_root=issue_repo_root,
                        )
                    except RuntimeError as exc:
                        print(
                            f"Conflict recovery result for branch '{recovery_branch}': "
                            f"needs manual intervention ({exc})"
                        )
                        raise
                    processed += 1
                    batch_done_summary = f"Completed conflict recovery setup for {recovery_label}"
                    batch_current_summary = f"Batch {batch_index}/{len(issues)} finished for {recovery_label}"
                    batch_action_items = [f"Ran conflict recovery against base '{recovery_base_branch}'"]
                    continue

                if autonomous_mode:
                    if recovered_status == "failed" and orchestration_attempt > max_attempts:
                        print(
                            f"Skipping {issue_label}: retry limit reached "
                            f"(attempt {orchestration_attempt - 1}/{max_attempts})."
                        )
                        continue

                    issue_claim, claim_warnings = select_latest_parseable_orchestration_claim(
                        comments=issue_comments,
                        source_label=issue_label,
                    )
                    for warning in claim_warnings:
                        print(f"Warning: {warning}", file=sys.stderr)
                    if is_active_orchestration_claim(issue_claim, run_id=run_id):
                        claim_payload = issue_claim.get("payload") if isinstance(issue_claim, dict) else {}
                        active_run_id = str(claim_payload.get("run_id") or "unknown")
                        print(
                            f"Skipping {issue_label}: active orchestration claim exists "
                            f"(run_id={active_run_id})."
                        )
                        continue
                    safe_post_orchestration_claim_comment(
                        repo=repo,
                        issue_number=issue["number"],
                        claim=build_orchestration_claim(
                            issue_number=issue["number"],
                            run_id=run_id,
                            status="claimed",
                            ttl_seconds=AUTONOMOUS_CLAIM_TTL_SECONDS,
                        ),
                        dry_run=args.dry_run,
                    )
                    claim_acquired = True

            issue_image_urls = collect_issue_image_urls(issue)
            has_issue_text = bool((issue.get("body") or "").strip())
            body_image_reason = (
                f"with {len(issue_image_urls)} embedded attachment reference(s)"
                if issue_image_urls
                else ""
            )

            if should_skip_issue_for_empty_body(
                mode=mode,
                include_empty=args.include_empty,
                has_issue_text=has_issue_text,
                issue_image_urls=issue_image_urls,
            ):
                print(f"Skipping {issue_label} (empty body)")
                continue

            if body_image_reason:
                print(f"{issue_label.capitalize()} {body_image_reason}")

            if mode == "issue-flow" and decompose_mode != "never":
                failure_stage = "decomposition_preflight"
                should_plan, assessment = should_issue_decompose(issue, decompose_mode)
                decomposition_assessment = assessment
                latest_plan = None
                plan_warnings: list[str] = []
                latest_payload_dict = {}
                latest_plan_is_execution_ready = False
                if should_plan or should_check_existing_decomposition_plan(issue, assessment):
                    issue_comments = current_tracker_provider().list_issue_comments(repo=repo, issue_id=issue["number"])
                    latest_plan, plan_warnings = select_latest_parseable_decomposition_plan(
                        comments=issue_comments,
                        source_label=f"issue #{issue['number']}",
                    )
                    for warning in plan_warnings:
                        print(f"Warning: {warning}", file=sys.stderr)

                    if latest_plan is not None:
                        latest_payload = latest_plan.get("payload")
                        latest_payload_dict = latest_payload if isinstance(latest_payload, dict) else {}
                        latest_plan_is_execution_ready = is_decomposition_plan_approved(latest_payload_dict)

                if latest_plan_is_execution_ready or should_plan:
                    if latest_plan is not None:

                        if is_decomposition_plan_approved(latest_payload_dict):
                            missing_children = _decomposition_plan_has_missing_children(latest_payload_dict)
                            if missing_children and create_child_issues:
                                created_children = _extract_ordered_linked_children(latest_payload_dict)
                                created_children_updates: list[dict] = []
                                for child in missing_children:
                                    created_child = current_tracker_provider().create_child_issue(
                                        repo=repo,
                                        parent_issue=issue,
                                        child=child,
                                        created_dependencies=created_children,
                                        dry_run=args.dry_run,
                                        parent_branch=issue_branch,
                                        base_branch=base_branch if base_branch else None,
                                    )
                                    created_order = child.get("order")
                                    if type(created_order) is int and created_order > 0:
                                        created_children[created_order] = created_child
                                    created_children_updates.append(created_child)

                                    if not args.dry_run:
                                        if isinstance(created_child.get("issue_number"), int):
                                            print(
                                                f"Created child issue #{created_child['issue_number']} "
                                                f"for order {child.get('order')} under issue #{issue['number']}"
                                            )
                                        else:
                                            raise RuntimeError(
                                                f"Created child issue payload for order {child.get('order')} "
                                                "was missing an integer issue number"
                                            )

                                latest_payload_dict = merge_created_children_into_plan_payload(
                                    latest_payload_dict,
                                    created_children_updates,
                                )
                                all_children_created = all(
                                    isinstance(child.get("issue_number"), int)
                                    for child in _normalize_created_children(
                                        latest_payload_dict.get("created_children")
                                    )
                                )

                                if all_children_created:
                                    latest_payload_dict = dict(latest_payload_dict)
                                    latest_payload_dict["status"] = "children_created"
                                    latest_payload_dict["next_action"] = "execute_children_in_order"

                                latest_payload_dict = attach_decomposition_resume_context(
                                    plan_payload=latest_payload_dict,
                                    parent_issue=issue,
                                    parent_branch=issue_branch,
                                    base_branch=base_branch if base_branch else None,
                                    next_action=str(
                                        latest_payload_dict.get("next_action")
                                        or "execute_children_in_order"
                                    ),
                                )

                                decomposition_rollup = build_decomposition_rollup_from_plan_payload(
                                    payload=latest_payload_dict,
                                )

                                post_decomposition_plan_comment(
                                    repo=repo,
                                    issue_number=issue["number"],
                                    payload=latest_payload_dict,
                                    dry_run=args.dry_run,
                                )
                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="issue",
                                    target_number=issue["number"],
                                    dry_run=args.dry_run,
                                    state=build_orchestration_state(
                                        status="waiting-for-author",
                                        task_type="issue",
                                        issue_number=issue["number"],
                                        pr_number=None,
                                        branch=issue_branch,
                                        base_branch=base_branch if base_branch else None,
                                        runner=args.runner,
                                        agent=args.agent,
                                        model=args.model,
                                        attempt=orchestration_attempt,
                                        stage="decomposition_plan",
                                        next_action="execute_children_in_order",
                                        error=(
                                            "Created child issues from approved decomposition plan"
                                            if all_children_created
                                            else "Waiting for all child issue creation in approved plan"
                                        ),
                                        decomposition=decomposition_rollup,
                                    ),
                                )
                                batch_done_summary = f"Prepared approved child issues for parent issue #{issue['number']}"
                                batch_current_summary = f"Batch {batch_index}/{len(issues)} waiting on child execution for issue #{issue['number']}"
                                batch_action_items = ["Created missing decomposition child issues", "Posted parent roll-up update"]
                                processed += 1
                                continue

                            if missing_children:
                                latest_payload_dict = attach_decomposition_resume_context(
                                    plan_payload=latest_payload_dict,
                                    parent_issue=issue,
                                    parent_branch=issue_branch,
                                    base_branch=base_branch if base_branch else None,
                                    next_action="create_missing_child_issues",
                                )
                                decomposition_rollup = build_decomposition_rollup_from_plan_payload(
                                    payload=latest_payload_dict,
                                )
                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="issue",
                                    target_number=issue["number"],
                                    dry_run=args.dry_run,
                                    state=build_orchestration_state(
                                        status="waiting-for-author",
                                        task_type="issue",
                                        issue_number=issue["number"],
                                        pr_number=None,
                                        branch=issue_branch,
                                        base_branch=base_branch if base_branch else None,
                                        runner=args.runner,
                                        agent=args.agent,
                                        model=args.model,
                                        attempt=orchestration_attempt,
                                        stage="decomposition_plan",
                                        next_action="create_missing_child_issues",
                                        error="Approved decomposition plan still has missing child issues",
                                        decomposition=decomposition_rollup,
                                    ),
                                )
                                batch_done_summary = f"Stopped issue #{issue['number']} until missing child issues are created"
                                batch_current_summary = f"Batch {batch_index}/{len(issues)} blocked on decomposition child creation"
                                batch_action_items = ["Kept approved decomposition plan in waiting state"]
                                batch_blockers = ["approved decomposition plan still has missing child issues"]
                                print(
                                    f"Approved decomposition plan for issue #{issue['number']} still has "
                                    f"{len(missing_children)} missing child issue(s); rerun with "
                                    "--create-child-issues to continue execution."
                                )
                                processed += 1
                                continue

                            latest_payload_dict = refresh_decomposition_plan_payload_from_child_states(
                                repo=repo,
                                plan_payload=latest_payload_dict,
                            )
                            decomposition_rollup = build_decomposition_rollup_from_plan_payload(
                                payload=latest_payload_dict,
                            )
                            selected_child = decomposition_rollup.get("next_child")
                            latest_payload_dict = attach_decomposition_resume_context(
                                plan_payload=latest_payload_dict,
                                parent_issue=issue,
                                parent_branch=issue_branch,
                                base_branch=base_branch if base_branch else None,
                                next_action=str(
                                    latest_payload_dict.get("next_action")
                                    or ("execute_next_child" if isinstance(selected_child, dict) else "review_completed_children")
                                ),
                                selected_child=selected_child if isinstance(selected_child, dict) else None,
                            )
                            decomposition_rollup = build_decomposition_rollup_from_plan_payload(
                                payload=latest_payload_dict,
                            )
                            selected_child = decomposition_rollup.get("next_child")
                            if not isinstance(selected_child, dict):
                                post_decomposition_plan_comment(
                                    repo=repo,
                                    issue_number=issue["number"],
                                    payload=latest_payload_dict,
                                    dry_run=args.dry_run,
                                )
                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="issue",
                                    target_number=issue["number"],
                                    dry_run=args.dry_run,
                                    state=build_orchestration_state(
                                        status=(
                                            "ready-for-review"
                                            if int(decomposition_rollup.get("progress", {}).get("completed") or 0)
                                            >= int(decomposition_rollup.get("total_children") or 0)
                                            else "blocked"
                                        ),
                                        task_type="issue",
                                        issue_number=issue["number"],
                                        pr_number=None,
                                        branch=issue_branch,
                                        base_branch=base_branch if base_branch else None,
                                        runner=args.runner,
                                        agent=args.agent,
                                        model=args.model,
                                        attempt=orchestration_attempt,
                                        stage="decomposition_execution",
                                        next_action=str(
                                            latest_payload_dict.get("next_action")
                                            or "review_completed_children"
                                        ),
                                        error=None,
                                        decomposition=decomposition_rollup,
                                    ),
                                )
                                batch_done_summary = f"No runnable decomposition child remained for issue #{issue['number']}"
                                batch_current_summary = f"Batch {batch_index}/{len(issues)} completed without a child execution"
                                batch_action_items = ["Refreshed decomposition roll-up from child states"]
                                print(
                                    f"Issue #{issue['number']} has no unblocked child issues ready to run; "
                                    f"{format_decomposition_rollup_context(decomposition_rollup)}"
                                )
                                processed += 1
                                continue

                            selected_child_issue_number = _as_positive_int(selected_child.get("issue_number"))
                            if selected_child_issue_number is None:
                                raise RuntimeError(
                                    f"Selected decomposition child for issue #{issue['number']} is missing an issue number"
                                )

                            decomposition_parent_issue = dict(issue)
                            decomposition_parent_branch = issue_branch
                            decomposition_parent_payload = dict(latest_payload_dict)
                            decomposition_child_note = build_decomposition_child_execution_note(
                                parent_issue=decomposition_parent_issue,
                                decomposition_rollup=decomposition_rollup,
                                selected_child=selected_child,
                            )
                            safe_post_orchestration_state_comment(
                                repo=repo,
                                target_type="issue",
                                target_number=decomposition_parent_issue["number"],
                                dry_run=args.dry_run,
                                state=build_orchestration_state(
                                    status="in-progress",
                                    task_type="issue",
                                    issue_number=decomposition_parent_issue["number"],
                                    pr_number=None,
                                    branch=decomposition_parent_branch,
                                    base_branch=base_branch if base_branch else None,
                                    runner=args.runner,
                                    agent=args.agent,
                                    model=args.model,
                                    attempt=orchestration_attempt,
                                    stage="decomposition_execution",
                                    next_action="run_selected_child_issue",
                                    error=None,
                                    decomposition=decomposition_rollup,
                                ),
                            )
                            issue = current_tracker_provider().get_issue(
                                repo=repo,
                                issue_id=selected_child_issue_number,
                            )
                            issue_image_urls = collect_issue_image_urls(issue)
                            issue_branch = branch_name_for_issue(
                                issue=issue,
                                prefix=args.branch_prefix,
                            )
                            state_target_type = "issue"
                            state_target_number = issue["number"]
                            state_pr_number = None
                            print(
                                f"Executing decomposition child issue #{issue['number']} for parent "
                                f"#{decomposition_parent_issue['number']}: step {selected_child.get('order')} "
                                f"{selected_child.get('title')}"
                            )
                            selected_decomposition_child = True
                            should_plan = False

                        if selected_decomposition_child:
                            pass
                        elif args.dry_run:
                            prefix = "[dry-run] "
                        else:
                            prefix = ""
                        if not selected_decomposition_child:
                            print(
                                f"{prefix}Decomposition plan already exists for issue #{issue['number']} "
                                f"({latest_plan.get('url') or 'no url'}); skipping duplicate plan."
                            )
                            processed += 1
                            continue

                    if not selected_decomposition_child:
                        payload = build_decomposition_plan_payload(issue=issue, assessment=assessment)
                        payload = attach_decomposition_resume_context(
                            plan_payload=payload,
                            parent_issue=issue,
                            parent_branch=issue_branch,
                            base_branch=base_branch if base_branch else None,
                            next_action="approve_plan_or_rerun_with_decompose_never",
                        )
                        post_decomposition_plan_comment(
                            repo=repo,
                            issue_number=issue["number"],
                            payload=payload,
                            dry_run=args.dry_run,
                        )
                        decomposition_rollup = build_decomposition_rollup_from_plan_payload(payload=payload)
                        safe_post_orchestration_state_comment(
                            repo=repo,
                            target_type="issue",
                            target_number=issue["number"],
                            dry_run=args.dry_run,
                            state=build_orchestration_state(
                                status="waiting-for-author",
                                task_type="issue",
                                issue_number=issue["number"],
                                pr_number=None,
                                branch=issue_branch,
                                base_branch=base_branch if base_branch else None,
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=orchestration_attempt,
                                stage="decomposition_plan",
                                next_action="approve_plan_or_rerun_with_decompose_never",
                                error="Task requires planning-only decomposition before implementation",
                                decomposition=decomposition_rollup,
                            ),
                        )
                        batch_done_summary = f"Posted planning-only decomposition plan for issue #{issue['number']}"
                        batch_current_summary = f"Batch {batch_index}/{len(issues)} waiting for plan approval"
                        batch_action_items = ["Posted decomposition plan", "Stopped before agent execution"]
                        batch_blockers = ["task requires planning-only decomposition before implementation"]
                        processed += 1
                        print(
                            f"Issue #{issue['number']} needs decomposition; posted planning-only plan "
                            "and stopped before agent execution."
                        )
                        continue

            processed += 1

            issue_image_paths: list[str] = []
            with tempfile.TemporaryDirectory(prefix=f"opencode-issue-{issue['number']}-images-") as image_download_dir:
                if issue_image_urls and not args.dry_run:
                    issue_image_paths = download_issue_images(
                        image_urls=issue_image_urls,
                        destination_dir=image_download_dir,
                        issue_number=issue["number"],
                    )
                elif issue_image_urls and args.dry_run:
                    print(
                        f"[dry-run] Would download {len(issue_image_urls)} image attachment(s) "
                        f"for {issue_label}"
                    )

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
                        build_prompt(issue, image_paths=issue_image_paths),
                        build_recovered_failure_context_note(recovered_state),
                    )
                elif (
                    recovered_state is not None
                    and str(recovered_state.get("status") or "") == "waiting-for-author"
                    and clarification_answer is not None
                ):
                    prompt_override = append_recovered_context_to_prompt(
                        build_prompt(issue, image_paths=issue_image_paths),
                        build_clarification_context_note(recovered_state, clarification_answer),
                    )
                elif decomposition_child_note:
                    prompt_override = append_recovered_context_to_prompt(
                        build_prompt(issue, image_paths=issue_image_paths),
                        decomposition_child_note,
                    )
                if mode == "pr-review":
                    if linked_open_pr is None:
                        raise RuntimeError(
                            f"Internal error: PR-review mode selected without linked PR for {issue_label}"
                        )

                    pr_number_raw = linked_open_pr.get("number")
                    if type(pr_number_raw) is not int:
                        raise RuntimeError(
                            f"Linked PR for {issue_label} has invalid number: {pr_number_raw}"
                        )
                    pr_number = pr_number_raw
                    state_target_type = "pr"
                    state_target_number = pr_number
                    state_pr_number = pr_number

                    print(
                        f"Auto-switch to PR-review mode for {issue_label}: {mode_reason}."
                    )
                    pull_request = current_codehost_provider().fetch_pull_request(repo=repo, number=pr_number)
                    merge_state = str(pull_request.get("mergeStateStatus") or "").strip().upper()
                    mergeable_state = str(pull_request.get("mergeable") or "").strip().upper()
                    merge_readiness_state = classify_pr_merge_readiness_state(
                        merge_state=merge_state,
                        mergeable=mergeable_state,
                    )
                    should_force_sync_rerun = merge_readiness_state in {"stale", "conflicting"}
                    if should_force_sync_rerun:
                        recovery_reason = (
                            "is stale against the base branch"
                            if merge_readiness_state == "stale"
                            else "is not mergeable with base yet"
                        )
                        print(
                            f"Linked PR #{pr_number} {recovery_reason} "
                            f"(mergeStateStatus={merge_state}); rerun will auto-sync and resolve routine conflicts"
                        )
                    pull_request, review_items, _review_stats = fetch_actionable_pr_review_feedback(
                        repo=repo,
                        pr_number=pr_number,
                        pull_request=pull_request,
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

                    if not review_items:
                        if should_force_sync_rerun:
                            skip_agent_run = True
                            print(
                                f"No actionable review comments for linked PR #{pr_number}; "
                                f"continuing with sync-only rerun because mergeStateStatus={merge_state}"
                            )
                        elif recovered_status in {"waiting-for-ci", "ready-to-merge"}:
                            current_attempt = orchestration_attempt_from_state(recovered_state)
                            ci_status = wait_for_pr_ci_status(
                                repo=repo,
                                pull_request=pull_request,
                            )
                            ci_overall = str(ci_status.get("overall") or "")
                            failing_checks = ci_status.get("failing_checks")
                            failing_checks_list = (
                                failing_checks if isinstance(failing_checks, list) else []
                            )
                            pending_checks = ci_status.get("pending_checks")
                            pending_checks_count = (
                                len(pending_checks) if isinstance(pending_checks, list) else 0
                            )
                            ci_checks_payload = (
                                ci_status.get("checks") if isinstance(ci_status.get("checks"), list) else []
                            )

                            if ci_overall == "pending":
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
                                        attempt=current_attempt,
                                         stage="ci_checks",
                                         next_action="wait_for_ci",
                                         error=None,
                                         ci_checks=ci_checks_payload,
                                         decomposition=decomposition_rollup,
                                     ),
                                 )
                                print(
                                    f"No actionable review comments for linked PR #{pr_number}; "
                                    f"CI checks are still pending ({pending_checks_count} pending), "
                                    "keeping waiting-for-ci state."
                                )
                                remove_agent_failure_label_from_issue(
                                    repo=repo,
                                    issue_number=issue["number"],
                                    dry_run=args.dry_run,
                                )
                                batch_done_summary = f"Linked PR #{pr_number} is still waiting for CI"
                                batch_current_summary = f"Batch {batch_index}/{len(issues)} completed in PR-review mode for {issue_label}"
                                batch_action_items = [f"Kept PR #{pr_number} in waiting-for-ci state"]
                                mark_autonomous_session_issue_processed(
                                    autonomous_session_state,
                                    issue_number=batch_issue_number,
                                    status="waiting-for-ci",
                                )
                                save_autonomous_session_state(
                                    autonomous_session_file,
                                    autonomous_session_state,
                                )
                                continue

                            if ci_overall == "failure":
                                failing_summary = format_failing_ci_checks_summary(failing_checks_list)
                                ci_diagnostics = collect_failing_ci_diagnostics(
                                    repo=repo,
                                    failing_checks=failing_checks_list,
                                )
                                diagnostics_summary = format_ci_diagnostics_summary(ci_diagnostics)
                                if str(ci_diagnostics.get("overall_classification") or "") == "transient":
                                    safe_post_orchestration_state_comment(
                                        repo=repo,
                                        target_type="pr",
                                        target_number=state_target_number,
                                        dry_run=args.dry_run,
                                        state=build_orchestration_state(
                                            status="blocked",
                                            task_type="pr",
                                            issue_number=issue["number"],
                                            pr_number=state_pr_number,
                                            branch=issue_branch,
                                            base_branch=target_base_branch,
                                            runner=args.runner,
                                            agent=args.agent,
                                            model=args.model,
                                            attempt=current_attempt,
                                            stage="ci_checks",
                                            next_action="retry_ci_after_transient_failure",
                                            error=short_error_text(f"{failing_summary}; {diagnostics_summary}"),
                                            ci_checks=ci_checks_payload,
                                            ci_diagnostics=ci_diagnostics,
                                            decomposition=decomposition_rollup,
                                        ),
                                    )
                                    print(
                                        f"No actionable review comments for linked PR #{pr_number}; "
                                        f"CI failure looks transient: {diagnostics_summary}."
                                    )
                                    remove_agent_failure_label_from_issue(
                                        repo=repo,
                                        issue_number=issue["number"],
                                        dry_run=args.dry_run,
                                    )
                                    batch_done_summary = f"Linked PR #{pr_number} is blocked on transient CI failure"
                                    batch_current_summary = f"Batch {batch_index}/{len(issues)} completed without a code retry for {issue_label}"
                                    batch_action_items = [f"Stopped PR #{pr_number} after classifying CI failure as transient"]
                                    batch_blockers = [diagnostics_summary]
                                    continue

                                if current_attempt >= max_attempts:
                                    safe_post_orchestration_state_comment(
                                        repo=repo,
                                        target_type="pr",
                                        target_number=state_target_number,
                                        dry_run=args.dry_run,
                                        state=build_orchestration_state(
                                            status="blocked",
                                            task_type="pr",
                                            issue_number=issue["number"],
                                            pr_number=state_pr_number,
                                            branch=issue_branch,
                                            base_branch=target_base_branch,
                                            runner=args.runner,
                                            agent=args.agent,
                                            model=args.model,
                                            attempt=current_attempt,
                                            stage="ci_checks",
                                            next_action="manual_ci_fix_required",
                                            error=short_error_text(
                                                f"{failing_summary}; retry limit reached at attempt {current_attempt}/{max_attempts}"
                                            ),
                                            ci_checks=ci_checks_payload,
                                            ci_diagnostics=ci_diagnostics,
                                            decomposition=decomposition_rollup,
                                        ),
                                    )
                                    print(
                                        f"No actionable review comments for linked PR #{pr_number}; "
                                        f"retry limit reached after {current_attempt}/{max_attempts} attempts: "
                                        f"{diagnostics_summary}"
                                    )
                                    remove_agent_failure_label_from_issue(
                                        repo=repo,
                                        issue_number=issue["number"],
                                        dry_run=args.dry_run,
                                    )
                                    batch_done_summary = f"Linked PR #{pr_number} reached the CI retry limit"
                                    batch_current_summary = f"Batch {batch_index}/{len(issues)} blocked on CI for {issue_label}"
                                    batch_action_items = [f"Left PR #{pr_number} for manual CI follow-up"]
                                    batch_blockers = [diagnostics_summary]
                                    continue

                                retry_attempt = current_attempt + 1
                                state_attempt = retry_attempt
                                review_attempt = retry_attempt
                                prompt_override = build_ci_failure_prompt(
                                    pull_request=pull_request,
                                    failing_checks=failing_checks_list,
                                    ci_diagnostics=ci_diagnostics,
                                    linked_issues=[issue],
                                )
                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="pr",
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
                                        attempt=retry_attempt,
                                        stage="ci_checks",
                                        next_action="run_ci_fix_agent",
                                        error=short_error_text(f"{failing_summary}; {diagnostics_summary}"),
                                        ci_checks=ci_checks_payload,
                                        ci_diagnostics=ci_diagnostics,
                                        decomposition=decomposition_rollup,
                                        merge_policy=merge_policy,
                                    ),
                                )
                                print(
                                    f"No actionable review comments for linked PR #{pr_number}; "
                                    f"CI is failing: {diagnostics_summary}. "
                                    f"Running CI fix attempt {retry_attempt}/{max_attempts}."
                                )

                            if ci_overall == "success":
                                finalize_pr_after_ci_success(
                                    repo=repo,
                                    pr_number=pr_number,
                                    linked_issues=[issue],
                                    merge_policy=merge_policy,
                                    target_type="pr",
                                    target_number=state_target_number,
                                    issue_number=issue["number"],
                                    branch=issue_branch,
                                    base_branch=target_base_branch,
                                    runner=args.runner,
                                    agent=args.agent,
                                    model=args.model,
                                    attempt=current_attempt,
                                    ci_checks=ci_checks_payload,
                                    decomposition=decomposition_rollup,
                                    project_config=project_config,
                                    repo_dir=os.getcwd(),
                                    dry_run=args.dry_run,
                                )
                                remove_agent_failure_label_from_issue(
                                    repo=repo,
                                    issue_number=issue["number"],
                                    dry_run=args.dry_run,
                                )
                                batch_done_summary = f"Linked PR #{pr_number} is ready to merge"
                                batch_current_summary = f"Batch {batch_index}/{len(issues)} finished after successful CI for {issue_label}"
                                batch_action_items = [f"Finalized PR #{pr_number} after green CI"]
                                mark_autonomous_session_issue_processed(
                                    autonomous_session_state,
                                    issue_number=batch_issue_number,
                                    status="ready-to-merge",
                                )
                                save_autonomous_session_state(
                                    autonomous_session_file,
                                    autonomous_session_state,
                                )
                                continue
                        elif recovered_status == "ready-for-review":
                            print(
                                f"No actionable review comments for linked PR #{pr_number}; "
                                f"keeping recovered state '{recovered_status}' and skipping duplicate issue-flow."
                            )
                            remove_agent_failure_label_from_issue(
                                repo=repo,
                                issue_number=issue["number"],
                                dry_run=args.dry_run,
                            )
                            batch_done_summary = f"Linked PR #{pr_number} remains ready for review"
                            batch_current_summary = f"Batch {batch_index}/{len(issues)} completed without new review work for {issue_label}"
                            batch_action_items = [f"Kept PR #{pr_number} in ready-for-review state"]
                            mark_autonomous_session_issue_processed(
                                autonomous_session_state,
                                issue_number=batch_issue_number,
                                status="ready-for-review",
                            )
                            save_autonomous_session_state(
                                autonomous_session_file,
                                autonomous_session_state,
                            )
                            continue
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
                                    attempt=state_attempt,
                                     stage="review_feedback",
                                     next_action="await_new_review_comments",
                                     error="No actionable review comments found",
                                     decomposition=decomposition_rollup,
                                 ),
                             )
                            print(
                                f"No actionable review comments for linked PR #{pr_number}; "
                                "skipping issue run."
                            )
                            remove_agent_failure_label_from_issue(
                                repo=repo,
                                issue_number=issue["number"],
                                dry_run=args.dry_run,
                            )
                            batch_done_summary = f"No actionable review comments remained for PR #{pr_number}"
                            batch_current_summary = f"Batch {batch_index}/{len(issues)} is waiting for new PR feedback"
                            batch_action_items = [f"Posted waiting-for-author state for PR #{pr_number}"]
                            continue

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
                            attempt=state_attempt,
                                stage="agent_run",
                                next_action="wait_for_agent_result",
                                error=None,
                                decomposition=decomposition_rollup,
                            ),
                        )

                    if review_items:
                        linked_issues = current_codehost_provider().load_pr_linked_issue_context(
                            repo=repo,
                            pull_request=pull_request,
                        )
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
                        elif (
                            recovered_state is not None
                            and str(recovered_state.get("status") or "") == "waiting-for-author"
                            and clarification_answer is not None
                        ):
                            prompt_override = append_recovered_context_to_prompt(
                                prompt_override,
                                build_clarification_context_note(recovered_state, clarification_answer),
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
                    target_base_branch = base_branch

                execution_settings = resolve_task_execution_settings(
                    args=args,
                    argv=raw_argv,
                    project_config=project_config,
                    issue=issue,
                    task_type="pr" if mode == "pr-review" else "issue",
                    scope_eligible=scope_eligible,
                    needs_decomposition=bool(decomposition_assessment.get("needs_decomposition")),
                )
                active_runner = str(execution_settings.get("runner") or args.runner)
                active_agent = str(execution_settings.get("agent") or args.agent)
                active_model = execution_settings.get("model")
                active_preset = _as_optional_string(execution_settings.get("preset"))
                active_track_tokens = bool(execution_settings.get("track_tokens", False))
                active_token_budget = execution_settings.get("token_budget")
                active_cost_budget_usd = execution_settings.get("cost_budget_usd")
                active_timeout_seconds = int(
                    execution_settings.get("agent_timeout_seconds") or args.agent_timeout_seconds
                )
                active_idle_timeout_seconds = execution_settings.get("agent_idle_timeout_seconds")
                active_max_attempts = int(execution_settings.get("max_attempts") or max_attempts)
                active_escalate_to_preset = _as_optional_string(execution_settings.get("escalate_to_preset"))
                attempt_plan = build_attempt_execution_plan(project_config, execution_settings)
                if not attempt_plan:
                    attempt_plan = [dict(execution_settings) | {"attempt": state_attempt or 1}]
                elif state_attempt > 1:
                    attempt_plan = [
                        dict(attempt_settings) | {"attempt": state_attempt + index - 1}
                        for index, attempt_settings in enumerate(attempt_plan, start=1)
                    ]
                if attempt_plan:
                    first_attempt_settings = attempt_plan[0]
                    active_attempt = int(first_attempt_settings.get("attempt") or state_attempt)
                    active_runner = str(first_attempt_settings.get("runner") or active_runner)
                    active_agent = str(first_attempt_settings.get("agent") or active_agent)
                    active_model = first_attempt_settings.get("model")
                    active_preset = _as_optional_string(first_attempt_settings.get("preset"))
                    active_track_tokens = bool(first_attempt_settings.get("track_tokens", active_track_tokens))
                    active_token_budget = first_attempt_settings.get("token_budget")
                    active_cost_budget_usd = first_attempt_settings.get("cost_budget_usd")
                    active_timeout_seconds = int(
                        first_attempt_settings.get("agent_timeout_seconds") or active_timeout_seconds
                    )
                    active_idle_timeout_seconds = first_attempt_settings.get("agent_idle_timeout_seconds")
                    active_max_attempts = int(first_attempt_settings.get("max_attempts") or active_max_attempts)
                    active_escalate_to_preset = _as_optional_string(first_attempt_settings.get("escalate_to_preset"))

                if active_preset is not None:
                    preset_prefix = "[dry-run] " if args.dry_run else ""
                    print(
                        f"{preset_prefix}Execution preset for {issue_label}: {active_preset} "
                        f"(runner={active_runner}, model={active_model or 'default'}, attempts={active_max_attempts})"
                    )

                stacked_base_context = (
                    target_base_branch if mode == "issue-flow" and base_branch_mode == "current" else None
                )

                if mode == "issue-flow":
                    if supports_issue_tracker_ops:
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
                                runner=active_runner,
                                agent=active_agent,
                                model=active_model,
                                attempt=active_attempt,
                                stage="agent_run",
                                next_action="wait_for_agent_result",
                                error=None,
                                decomposition=decomposition_rollup,
                            ),
                        )

                failure_stage = "prepare_branch"
                branch_status = prepare_issue_branch(
                    base_branch=target_base_branch,
                    branch_name=issue_branch,
                    dry_run=args.dry_run,
                    fail_on_existing=args.fail_on_existing,
                )
                if not args.dry_run:
                    issue_repo_root = current_repo_root()
                print(f"Branch status for {issue_label}: {branch_status}")

                reused_branch_sync_result: dict[str, object] | None = None

                if branch_status == "reused":
                    if args.sync_reused_branch:
                        failure_stage = "sync_branch"
                        reused_branch_sync_result = sync_reused_branch_with_base(
                            base_branch=target_base_branch,
                            branch_name=issue_branch,
                            strategy=args.sync_strategy,
                            dry_run=args.dry_run,
                        )
                        print_branch_sync_result(reused_branch_sync_result, dry_run=args.dry_run)
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

                pre_run_untracked_files: set[str] | None = None
                if not skip_agent_run and not args.dry_run:
                    pre_run_untracked_files = list_untracked_files()
                issue_hook_context = {
                    "hook_target": "issue" if mode == "issue-flow" else "pr",
                    "issue_number": issue["number"],
                    "pr_number": state_pr_number,
                    "branch": issue_branch,
                    "base_branch": target_base_branch,
                    "repo_dir": os.getcwd(),
                }

                issue_hook_env = build_workflow_hook_env(
                    repo=repo,
                    mode=mode,
                    issue_number=issue["number"],
                    pr_number=state_pr_number,
                    branch=issue_branch,
                    base_branch=target_base_branch,
                )

                if skip_agent_run:
                    print(
                        f"Skipping agent run for {issue_label} in pr-review mode: "
                        "no actionable review comments; running sync-only path"
                    )
                else:
                    failure_stage = "workflow_hooks"
                    run_configured_workflow_hooks(
                        hook_name="pre_agent",
                        configured_hooks=configured_hooks,
                        dry_run=args.dry_run,
                        cwd=os.getcwd(),
                        env=issue_hook_env,
                        context=issue_hook_context,
                    )
                    issue_agent_run_stats = {}
                    agent_result: dict[str, object] = {}
                    agent_error: RuntimeError | None = None
                    for attempt_settings in attempt_plan:
                        active_attempt = int(attempt_settings.get("attempt") or state_attempt)
                        state_attempt = active_attempt
                        active_runner = str(attempt_settings.get("runner") or active_runner)
                        active_agent = str(attempt_settings.get("agent") or active_agent)
                        active_model = attempt_settings.get("model")
                        active_preset = _as_optional_string(attempt_settings.get("preset"))
                        active_track_tokens = bool(attempt_settings.get("track_tokens", active_track_tokens))
                        active_token_budget = attempt_settings.get("token_budget")
                        active_cost_budget_usd = attempt_settings.get("cost_budget_usd")
                        active_timeout_seconds = int(
                            attempt_settings.get("agent_timeout_seconds") or active_timeout_seconds
                        )
                        active_idle_timeout_seconds = attempt_settings.get("agent_idle_timeout_seconds")
                        active_max_attempts = int(attempt_settings.get("max_attempts") or active_max_attempts)
                        active_escalate_to_preset = _as_optional_string(attempt_settings.get("escalate_to_preset"))
                        issue_agent_run_stats = {}
                        agent_result = {}

                        if active_attempt > 1:
                            print(f"Retrying {issue_label}: {_attempt_settings_summary(attempt_settings)}")
                            safe_post_orchestration_state_comment(
                                repo=repo,
                                target_type=state_target_type,
                                target_number=state_target_number,
                                dry_run=args.dry_run,
                                state=build_orchestration_state(
                                    status="in-progress",
                                    task_type="issue" if mode == "issue-flow" else "pr",
                                    issue_number=issue["number"],
                                    pr_number=state_pr_number,
                                    branch=issue_branch,
                                    base_branch=target_base_branch,
                                    runner=active_runner,
                                    agent=active_agent,
                                    model=active_model,
                                    attempt=active_attempt,
                                    stage="agent_run",
                                    next_action="wait_for_agent_result",
                                    error=None,
                                    decomposition=decomposition_rollup,
                                ),
                            )

                        failure_stage = "agent_run"
                        exit_code = run_agent(
                            issue=issue,
                            runner=active_runner,
                            agent=active_agent,
                            model=active_model,
                            dry_run=args.dry_run,
                            timeout_seconds=active_timeout_seconds,
                            idle_timeout_seconds=active_idle_timeout_seconds,
                            opencode_auto_approve=args.opencode_auto_approve,
                            image_paths=issue_image_paths,
                            prompt_override=prompt_override,
                            track_tokens=active_track_tokens,
                            token_budget=active_token_budget,
                            cost_budget_usd=active_cost_budget_usd,
                            run_stats=issue_agent_run_stats,
                            agent_result=agent_result,
                            expected_branch=issue_branch,
                            expected_repo_root=issue_repo_root,
                        )
                        if exit_code == 0:
                            agent_error = None
                            break

                        exit_summary = describe_exit_code(exit_code)
                        diagnosis = classify_opencode_failure(
                            return_code=exit_code,
                            model=active_model,
                        ) if active_runner == "opencode" else None
                        message = (
                            f"Agent failed for {issue_label} with {exit_summary}"
                            + (f" ({diagnosis})" if diagnosis else "")
                        )
                        agent_error = RuntimeError(message)
                        if active_attempt >= active_max_attempts:
                            break
                        next_attempt = active_attempt + 1
                        print(
                            f"{message}; escalating to attempt {next_attempt}/{active_max_attempts}."
                        )

                    if agent_error is not None:
                        raise agent_error

                    failure_stage = "workflow_hooks"
                    run_configured_workflow_hooks(
                        hook_name="post_agent",
                        configured_hooks=configured_hooks,
                        dry_run=args.dry_run,
                        cwd=os.getcwd(),
                        env=issue_hook_env,
                        context=issue_hook_context,
                    )
                    clarification_request = agent_result.get("clarification_request")
                    if isinstance(clarification_request, dict):
                        question = str(clarification_request.get("question") or "").strip()
                        reason = _as_optional_string(clarification_request.get("reason"))
                        if question:
                            safe_post_clarification_request_comment(
                                repo=repo,
                                target_type=state_target_type,
                                target_number=state_target_number,
                                question=question,
                                reason=reason,
                                dry_run=args.dry_run,
                            )
                            safe_post_orchestration_state_comment(
                                repo=repo,
                                target_type=state_target_type,
                                target_number=state_target_number,
                                dry_run=args.dry_run,
                                state=build_orchestration_state(
                                    status="waiting-for-author",
                                    task_type="issue" if mode == "issue-flow" else "pr",
                                    issue_number=issue["number"],
                                    pr_number=state_pr_number,
                                    branch=issue_branch,
                                    base_branch=target_base_branch,
                                    runner=active_runner,
                                    agent=active_agent,
                                    model=active_model,
                                    attempt=active_attempt,
                                    stage="agent_run",
                                    next_action="await_author_reply",
                                    error=reason or question,
                                    stats=issue_agent_run_stats,
                                    decomposition=decomposition_rollup,
                                ) | {"question": question, "reason": reason},
                            )
                            batch_done_summary = f"Paused {issue_label} for author clarification"
                            batch_current_summary = f"Batch {batch_index}/{len(issues)} waiting for author reply on {issue_label}"
                            batch_action_items = [f"Posted clarification request for {issue_label}"]
                            batch_blockers = [reason or question]
                            print(
                                f"Paused {issue_label} for author clarification: {question}"
                            )
                            if (
                                decomposition_parent_issue is not None
                                and decomposition_parent_branch is not None
                                and decomposition_parent_payload is not None
                            ):
                                decomposition_parent_payload, decomposition_rollup = post_parent_decomposition_rollup_update(
                                    repo=repo,
                                    parent_issue=decomposition_parent_issue,
                                    parent_branch=decomposition_parent_branch,
                                    base_branch=base_branch if base_branch else None,
                                    runner=active_runner,
                                    agent=active_agent,
                                    model=active_model,
                                    plan_payload=decomposition_parent_payload,
                                    dry_run=args.dry_run,
                                )
                            if not args.dry_run:
                                run_command(["git", "checkout", base_branch])
                            continue
                if not args.dry_run and not has_changes():
                    if (
                        branch_status == "reused"
                        and args.sync_reused_branch
                        and isinstance(reused_branch_sync_result, dict)
                        and bool(reused_branch_sync_result.get("changed"))
                    ):
                        print(
                            f"No file changes from agent for {issue_label}; "
                            "pushing sync-only branch updates"
                        )
                        failure_stage = "workflow_checks"
                        try:
                            run_forced_recovery_verification(
                                branch_name=issue_branch,
                                project_config=project_config,
                                repo_dir=os.getcwd(),
                                dry_run=False,
                            )
                        except RecoveryVerificationFailure as exc:
                            recovery_next_action = "inspect_recovery_verification"
                            linked_pr_number = linked_open_pr.get("number") if isinstance(linked_open_pr, dict) else None
                            if mode == "pr-review" and type(linked_pr_number) is int:
                                safe_post_recovery_verification_follow_up_comment(
                                    repo=repo,
                                    pr_number=linked_pr_number,
                                    branch_name=issue_branch,
                                    verification=exc.verification,
                                    recovery_result=reused_branch_sync_result,
                                    next_action=recovery_next_action,
                                    dry_run=args.dry_run,
                                )
                            safe_post_orchestration_state_comment(
                                repo=repo,
                                target_type=state_target_type,
                                target_number=state_target_number,
                                dry_run=args.dry_run,
                                state=build_orchestration_state(
                                    status="blocked",
                                    task_type="issue" if mode == "issue-flow" else "pr",
                                    issue_number=issue["number"],
                                    pr_number=state_pr_number,
                                    branch=issue_branch,
                                    base_branch=target_base_branch,
                                    runner=active_runner,
                                    agent=active_agent,
                                    model=active_model,
                                    attempt=state_attempt,
                                    stage="workflow_checks",
                                    next_action=recovery_next_action,
                                    error=short_error_text(str(exc)),
                                    workflow_checks=(
                                        exc.verification.get("commands")
                                        if isinstance(exc.verification.get("commands"), list)
                                        else None
                                    ),
                                    stats=issue_agent_run_stats,
                                    decomposition=decomposition_rollup,
                                ),
                            )
                            print(
                                f"Recovery verification failed for {issue_label} after sync-only rerun: {exc}. "
                                "Recorded blocked state with follow-up evidence."
                            )
                            if supports_issue_tracker_ops:
                                remove_agent_failure_label_from_issue(
                                    repo=repo,
                                    issue_number=issue["number"],
                                    dry_run=args.dry_run,
                                )
                            if not args.dry_run:
                                run_command(["git", "checkout", base_branch])
                            batch_done_summary = f"Blocked {issue_label} on recovery verification follow-up"
                            batch_current_summary = (
                                f"Batch {batch_index}/{len(issues)} blocked after sync-only recovery for {issue_label}"
                            )
                            batch_action_items = [
                                f"Posted recovery verification evidence for PR #{linked_pr_number}"
                                if type(linked_pr_number) is int
                                else f"Recorded recovery verification failure for {issue_label}"
                            ]
                            batch_blockers = [short_error_text(str(exc))]
                            mark_autonomous_session_issue_processed(
                                autonomous_session_state,
                                issue_number=batch_issue_number,
                                status="blocked",
                            )
                            save_autonomous_session_state(
                                autonomous_session_file,
                                autonomous_session_state,
                            )
                            continue
                        used_force_with_lease = (
                            str(reused_branch_sync_result.get("applied_strategy") or "") == "rebase"
                        )
                        failure_stage = "workflow_hooks"
                        run_configured_workflow_hooks(
                            hook_name="pre_pr_update",
                            configured_hooks=configured_hooks,
                            dry_run=False,
                            cwd=os.getcwd(),
                            env=issue_hook_env,
                            context=issue_hook_context,
                        )
                        failure_stage = "commit_push"
                        push_branch(
                            branch_name=issue_branch,
                            dry_run=False,
                            force_with_lease=used_force_with_lease,
                            expected_repo_root=issue_repo_root,
                        )
                        print(
                            f"Sync-only push result for {issue_label}: "
                            f"branch '{issue_branch}' pushed "
                            f"(force-with-lease: {'yes' if used_force_with_lease else 'no'})"
                        )
                        if mode == "pr-review" and linked_open_pr is not None:
                            linked_pr_number = linked_open_pr.get("number")
                            if type(linked_pr_number) is int:
                                print(
                                    f"PR #{linked_pr_number} rerun sync pushed; forced verification passed and "
                                    "GitHub mergeability should be recalculated without manual conflict steps"
                                )
                    else:
                        print(
                            f"No changes detected for {issue_label}; skipping commit and PR"
                        )
                        if supports_github_issue_ops or mode == "pr-review":
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
                                    runner=active_runner,
                                    agent=active_agent,
                                    model=active_model,
                                    attempt=state_attempt,
                                    stage="changes_pushed",
                                    next_action="wait_for_ci",
                                    error=None,
                                    stats=issue_agent_run_stats,
                                    decomposition=decomposition_rollup,
                                ),
                            )
                        elif mode == "issue-flow" and supports_issue_tracker_ops:
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
                                    runner=active_runner,
                                    agent=active_agent,
                                    model=active_model,
                                    attempt=active_attempt,
                                    stage="pr_ready",
                                    next_action="wait_for_review",
                                    error=None,
                                    stats=issue_agent_run_stats,
                                    decomposition=decomposition_rollup,
                                ),
                            )
                        failure_stage = "workflow_hooks"
                        run_configured_workflow_hooks(
                            hook_name="post_pr_update",
                            configured_hooks=configured_hooks,
                            dry_run=False,
                            cwd=os.getcwd(),
                            env=issue_hook_env,
                            context=issue_hook_context,
                        )
                        if (
                            decomposition_parent_issue is not None
                            and decomposition_parent_branch is not None
                            and decomposition_parent_payload is not None
                        ):
                            decomposition_parent_payload, decomposition_rollup = post_parent_decomposition_rollup_update(
                                repo=repo,
                                parent_issue=decomposition_parent_issue,
                                parent_branch=decomposition_parent_branch,
                                base_branch=base_branch if base_branch else None,
                                runner=active_runner,
                                agent=active_agent,
                                model=active_model,
                                plan_payload=decomposition_parent_payload,
                                dry_run=args.dry_run,
                            )
                        if supports_issue_tracker_ops:
                            remove_agent_failure_label_from_issue(
                                repo=repo,
                                issue_number=issue["number"],
                                dry_run=args.dry_run,
                            )
                        if not args.dry_run:
                            run_command(["git", "checkout", base_branch])
                        batch_done_summary = f"No code changes needed for {issue_label}"
                        batch_current_summary = f"Batch {batch_index}/{len(issues)} finished with existing branch state for {issue_label}"
                        batch_action_items = ["Skipped commit and PR because working tree was unchanged"]
                        mark_autonomous_session_issue_processed(
                            autonomous_session_state,
                            issue_number=batch_issue_number,
                            status="ready-for-review",
                        )
                        save_autonomous_session_state(
                            autonomous_session_file,
                            autonomous_session_state,
                        )
                        continue

                failure_stage = "commit_push"
                commit_changes(
                    issue=issue,
                    dry_run=args.dry_run,
                    pre_run_untracked_files=pre_run_untracked_files,
                    expected_branch=issue_branch,
                    expected_repo_root=issue_repo_root,
                )

                failure_stage = "workflow_checks"
                workflow_check_results = run_configured_workflow_checks(
                    checks=workflow_checks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                )

                failure_stage = "commit_push"
                if configured_hooks:
                    failure_stage = "workflow_hooks"
                    run_configured_workflow_hooks(
                        hook_name="pre_pr_update",
                        configured_hooks=configured_hooks,
                        dry_run=args.dry_run,
                        cwd=os.getcwd(),
                        env=issue_hook_env,
                        context=issue_hook_context,
                    )
                    failure_stage = "commit_push"
                push_branch(
                    branch_name=issue_branch,
                    dry_run=args.dry_run,
                    force_with_lease=(
                        branch_status == "reused"
                        and args.sync_reused_branch
                        and isinstance(reused_branch_sync_result, dict)
                        and bool(reused_branch_sync_result.get("changed"))
                        and str(reused_branch_sync_result.get("applied_strategy") or "") == "rebase"
                    ),
                    expected_repo_root=issue_repo_root,
                )
                pr_status, pr_url = current_codehost_provider().ensure_pr(
                    repo=repo,
                    base_branch=target_base_branch,
                    branch_name=issue_branch,
                    issue=issue,
                    dry_run=args.dry_run,
                    fail_on_existing=args.fail_on_existing,
                    stacked_base_context=stacked_base_context,
                )
                if pr_url:
                    touched_prs.append(pr_url)
                    print(f"PR status for {issue_label}: {pr_status} ({pr_url})")
                    batch_action_items = [f"Updated PR state for {issue_label}: {pr_status} ({pr_url})"]
                if mode == "issue-flow" and supports_github_issue_ops:
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
                            runner=active_runner,
                            agent=active_agent,
                            model=active_model,
                            attempt=active_attempt,
                            stage="pr_ready",
                            next_action="wait_for_review",
                            error=None,
                            workflow_checks=workflow_check_results,
                            stats=issue_agent_run_stats,
                            decomposition=decomposition_rollup,
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
                            runner=active_runner,
                            agent=active_agent,
                            model=active_model,
                            attempt=active_attempt,
                            stage="changes_pushed",
                            next_action="wait_for_ci",
                            error=None,
                            workflow_checks=workflow_check_results,
                            stats=issue_agent_run_stats,
                            decomposition=decomposition_rollup,
                        ),
                    )
                if (
                    decomposition_parent_issue is not None
                    and decomposition_parent_branch is not None
                    and decomposition_parent_payload is not None
                ):
                    decomposition_parent_payload, decomposition_rollup = post_parent_decomposition_rollup_update(
                        repo=repo,
                        parent_issue=decomposition_parent_issue,
                        parent_branch=decomposition_parent_branch,
                        base_branch=base_branch if base_branch else None,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        plan_payload=decomposition_parent_payload,
                        dry_run=args.dry_run,
                    )
                if supports_github_issue_ops:
                    remove_agent_failure_label_from_issue(
                        repo=repo,
                        issue_number=issue["number"],
                        dry_run=args.dry_run,
                    )

                failure_stage = "workflow_hooks"
                run_configured_workflow_hooks(
                    hook_name="post_pr_update",
                    configured_hooks=configured_hooks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                    env=issue_hook_env,
                    context=issue_hook_context,
                )
                mark_autonomous_session_issue_processed(
                    autonomous_session_state,
                    issue_number=batch_issue_number,
                    status=("ready-for-review" if mode == "issue-flow" else "waiting-for-ci"),
                )
                batch_done_summary = (
                    f"Prepared issue #{issue['number']} for review"
                    if mode == "issue-flow"
                    else f"Advanced linked PR #{state_pr_number} to waiting-for-ci"
                )
                batch_current_summary = f"Batch {batch_index}/{len(issues)} finished for {issue_label}"
                save_autonomous_session_state(
                    autonomous_session_file,
                    autonomous_session_state,
                )
                break

                if (
                    decomposition_parent_issue is not None
                    and decomposition_parent_branch is not None
                    and decomposition_parent_payload is not None
                ):
                    decomposition_parent_payload, decomposition_rollup = post_parent_decomposition_rollup_update(
                        repo=repo,
                        parent_issue=decomposition_parent_issue,
                        parent_branch=decomposition_parent_branch,
                        base_branch=base_branch if base_branch else None,
                                runner=active_runner,
                                agent=active_agent,
                                model=active_model,
                        plan_payload=decomposition_parent_payload,
                        dry_run=args.dry_run,
                    )

                if not args.dry_run:
                    run_command(["git", "checkout", base_branch])
                if supports_issue_tracker_ops:
                    remove_agent_failure_label_from_issue(
                        repo=repo,
                        issue_number=issue["number"],
                        dry_run=args.dry_run,
                    )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if isinstance(exc, ResidualUntrackedFilesError):
                failure_stage = "residual_untracked_validation"
            elif isinstance(exc, BranchContextMismatchError):
                failure_stage = "branch_context_validation"
            elif isinstance(exc, TokenBudgetExceededError):
                failure_stage = "token_budget"
            elif isinstance(exc, CostBudgetExceededError):
                failure_stage = "cost_budget"

            failure_status = failure_state_for_stage(failure_stage)
            next_action = failure_next_action_for_stage(failure_stage)
            workflow_results = exc.checks if isinstance(exc, WorkflowCheckFailure) else None
            if workflow_results is None and isinstance(exc, WorkflowHookFailure):
                workflow_results = exc.hooks
            residual_untracked_files = (
                exc.files if isinstance(exc, ResidualUntrackedFilesError) else None
            )
            if supports_issue_tracker_ops or mode == "pr-review":
                safe_post_orchestration_state_comment(
                    repo=repo,
                    target_type=state_target_type,
                    target_number=state_target_number,
                    dry_run=args.dry_run,
                    state=build_orchestration_state(
                        status=failure_status,
                        task_type="issue" if mode == "issue-flow" else "pr",
                        issue_number=issue["number"],
                        pr_number=state_pr_number,
                        branch=locals().get("issue_branch", None),
                        base_branch=locals().get("target_base_branch", None),
                        runner=active_runner,
                        agent=active_agent,
                        model=active_model,
                        attempt=state_attempt,
                        stage=failure_stage,
                        next_action=next_action,
                        error=short_error_text(str(exc)),
                        workflow_checks=workflow_results,
                        residual_untracked_files=residual_untracked_files,
                        stats=issue_agent_run_stats,
                        decomposition=decomposition_rollup,
                    ),
                )
            if (
                decomposition_parent_issue is not None
                and decomposition_parent_branch is not None
                and decomposition_parent_payload is not None
            ):
                try:
                    decomposition_parent_payload, decomposition_rollup = post_parent_decomposition_rollup_update(
                        repo=repo,
                        parent_issue=decomposition_parent_issue,
                        parent_branch=decomposition_parent_branch,
                        base_branch=base_branch if base_branch else None,
                        runner=active_runner,
                        agent=active_agent,
                        model=active_model,
                        plan_payload=decomposition_parent_payload,
                        dry_run=args.dry_run,
                    )
                except Exception as parent_exc:  # noqa: BLE001
                    print(
                        "Warning: failed to refresh parent decomposition roll-up for issue "
                        f"#{decomposition_parent_issue['number']}: {parent_exc}",
                        file=sys.stderr,
                    )
            if supports_issue_tracker_ops:
                safe_report_issue_automation_failure(
                    repo=repo,
                    issue_number=issue["number"],
                    run_id=run_id,
                    stage=failure_stage,
                    error=str(exc),
                    branch=locals().get("issue_branch", None),
                    base_branch=locals().get("target_base_branch", None),
                    runner=active_runner,
                    agent=active_agent,
                    model=active_model,
                    residual_untracked_files=residual_untracked_files,
                    next_action=next_action,
                    dry_run=args.dry_run,
                    already_reported_issue_numbers=reported_issue_failures,
                )
            batch_done_summary = f"Failed {issue_label} during {failure_stage}"
            batch_current_summary = f"Batch {batch_index}/{len(issues)} failed for {issue_label}"
            batch_action_items = [f"Posted failure diagnostics for {issue_label}"]
            batch_blockers = [short_error_text(str(exc))]
            print(f"{issue_label.capitalize()} failed: {exc}", file=sys.stderr)
            if args.stop_on_error:
                break
        finally:
            if autonomous_mode and claim_acquired and supports_github_issue_ops:
                safe_post_orchestration_claim_comment(
                    repo=repo,
                    issue_number=issue["number"],
                    claim=build_orchestration_claim(
                        issue_number=issue["number"],
                        run_id=run_id,
                        status="released",
                        ttl_seconds=1,
                    ),
                    dry_run=args.dry_run,
                )
            if autonomous_mode and issue_number_arg is None:
                update_autonomous_session_checkpoint(
                    autonomous_session_state,
                    run_id=run_id,
                    phase="running",
                    batch_index=batch_index,
                    total_batches=len(issues),
                    counts={
                        "processed": processed,
                        "failures": failures,
                        "skipped_existing_pr": skipped_existing_pr,
                        "skipped_existing_branch": skipped_existing_branch,
                        "skipped_blocked_dependencies": skipped_blocked_dependencies,
                        "skipped_out_of_scope": skipped_out_of_scope,
                    },
                    done=[batch_done_summary],
                    current=batch_current_summary,
                    next_items=preview_autonomous_issue_queue(issues, start_index=batch_index),
                    issue_pr_actions=batch_action_items,
                    in_progress=[],
                    blockers=blocked_dependency_summaries + batch_blockers,
                    next_checkpoint=(
                        "final autonomous summary"
                        if batch_index >= len(issues)
                        else f"when batch {batch_index + 1}/{len(issues)} starts"
                    ),
                )
                save_autonomous_session_state(autonomous_session_file, autonomous_session_state)
                print(format_autonomous_session_status_summary(autonomous_session_state))

    if autonomous_mode and issue_number_arg is None:
        update_autonomous_session_checkpoint(
            autonomous_session_state,
            run_id=run_id,
            phase="completed",
            batch_index=len(issues),
            total_batches=len(issues),
            counts={
                "processed": processed,
                "failures": failures,
                "skipped_existing_pr": skipped_existing_pr,
                "skipped_existing_branch": skipped_existing_branch,
                "skipped_blocked_dependencies": skipped_blocked_dependencies,
                "skipped_out_of_scope": skipped_out_of_scope,
            },
            done=[f"Autonomous batch loop finished across {len(issues)} runnable issue(s)"],
            current="Idle between autonomous runs",
            next_items=[],
            issue_pr_actions=final_issue_pr_actions,
            in_progress=[],
            blockers=blocked_dependency_summaries + ([f"{failures} batch failure(s) need follow-up"] if failures > 0 else []),
            next_checkpoint="when the next autonomous invocation starts",
            verification=post_batch_verification,
        )
        save_autonomous_session_state(autonomous_session_file, autonomous_session_state)
        print(format_autonomous_session_status_summary(autonomous_session_state))

    print(
        "Done. "
        f"Processed: {processed}, "
        f"skipped_existing_pr: {skipped_existing_pr}, "
        f"skipped_existing_branch: {skipped_existing_branch}, "
        f"skipped_out_of_scope: {skipped_out_of_scope}, "
        f"failures: {failures}"
    )
    if touched_prs:
        print("PRs:")
        for pr_url in touched_prs:
            print(f"- {pr_url}")
    verification_failed = bool(
        isinstance(post_batch_verification, dict)
        and str(post_batch_verification.get("status") or "") == "failed"
    )
    return _finish_main(1 if failures > 0 or verification_failed else 0, original_process_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
