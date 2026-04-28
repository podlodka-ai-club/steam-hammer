#!/usr/bin/env python3

import argparse
import base64
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


LOCAL_CONFIG_RELATIVE_PATH = "local-config.json"
PROJECT_CONFIG_RELATIVE_PATH = "project-config.json"
BUILTIN_DEFAULTS = {
    "tracker": "github",
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

TRACKER_GITHUB = "github"
TRACKER_JIRA = "jira"
TRACKER_CHOICES = {TRACKER_GITHUB, TRACKER_JIRA}

JIRA_ENV_VARS = {
    "base_url": "JIRA_BASE_URL",
    "email": "JIRA_EMAIL",
    "api_token": "JIRA_API_TOKEN",
}
JIRA_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")

ORCHESTRATION_STATE_MARKER = "<!-- orchestration-state:v1 -->"
AGENT_FAILURE_REPORT_MARKER = "<!-- orchestration-agent-failure:v1 -->"
SCOPE_DECISION_MARKER = "<!-- orchestration-scope:v1 -->"
DECOMPOSITION_PLAN_MARKER = "<!-- orchestration-decomposition:v1 -->"
CLARIFICATION_REQUEST_MARKER = "<!-- orchestration-clarification-request:v1 -->"
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


def is_trackable_issue_number(value: object) -> bool:
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return value.strip().isdigit()
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


def _parse_tracker(tracker: object) -> str:
    normalized = str(tracker or "").strip().lower()
    if normalized not in TRACKER_CHOICES:
        raise RuntimeError(f"Unsupported tracker '{tracker}'. Expected one of: {', '.join(sorted(TRACKER_CHOICES))}")
    return normalized


def issue_tracker(issue: dict) -> str:
    return _parse_tracker(issue.get("tracker") or TRACKER_GITHUB)


def format_issue_ref(issue_number: object, tracker: str = TRACKER_GITHUB) -> str:
    normalized_tracker = _parse_tracker(tracker)
    if normalized_tracker == TRACKER_JIRA:
        return str(issue_number)
    return f"#{issue_number}"


def format_issue_label(issue_number: object, tracker: str = TRACKER_GITHUB) -> str:
    return f"issue {format_issue_ref(issue_number, tracker=tracker)}"


def format_issue_ref_from_issue(issue: dict) -> str:
    return format_issue_ref(issue.get("number"), tracker=issue_tracker(issue))


def format_issue_label_from_issue(issue: dict) -> str:
    return format_issue_label(issue.get("number"), tracker=issue_tracker(issue))


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


def validate_tracker_requirements(tracker: str, pr_mode_requested: bool) -> None:
    normalized_tracker = _parse_tracker(tracker)
    if pr_mode_requested and normalized_tracker != TRACKER_GITHUB:
        raise RuntimeError("--pr / --from-review-comments mode only supports --tracker github")
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

    issue_labels = set(_issue_label_names(issue))
    issue_author = _issue_author_login(issue)

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

    return {
        "eligible": True,
        "reason": "scope rules passed",
        "matched": {},
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


def run_check_command(command: list[str], cwd: str | None = None) -> tuple[bool, str, str, int]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
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


WORKFLOW_COMMAND_ORDER = ["test", "lint", "build"]

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
    return "blocked" if failure_stage in {"workflow_checks", "residual_untracked_validation", "token_budget"} else "failed"


def failure_next_action_for_stage(failure_stage: str) -> str:
    if failure_stage == "workflow_checks":
        return "fix_workflow_checks_and_retry"
    if failure_stage == "residual_untracked_validation":
        return "stage_or-remove-residual-untracked-files"
    if failure_stage == "token_budget":
        return "raise_token_budget_or_split_issue"
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


def configured_workflow_commands(project_config: dict) -> list[tuple[str, str]]:
    workflow = project_config.get("workflow") if isinstance(project_config, dict) else None
    if not isinstance(workflow, dict):
        return []

    commands = workflow.get("commands")
    if not isinstance(commands, dict):
        return []

    configured: list[tuple[str, str]] = []
    for check_name in WORKFLOW_COMMAND_ORDER:
        command = commands.get(check_name)
        if command is None:
            continue
        command_text = str(command).strip()
        if not command_text:
            continue
        configured.append((check_name, command_text))
    return configured


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
            "number,title,body,url,state,labels,author",
        ]
    )
    issues = json.loads(output)
    if not isinstance(issues, list):
        raise RuntimeError("Unexpected response from gh issue list")
    for issue in issues:
        if isinstance(issue, dict):
            issue.setdefault("tracker", TRACKER_GITHUB)
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
            "number,title,body,url,state,labels,author",
        ]
    )
    issue = json.loads(output)
    if not isinstance(issue, dict):
        raise RuntimeError(f"Unexpected response fetching issue #{number}")
    issue.setdefault("tracker", TRACKER_GITHUB)
    return issue


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
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name:
        raise RuntimeError(f"Invalid repo format '{repo}'. Expected owner/name.")
    return owner, name


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
    output = run_capture(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url,state,mergeStateStatus,headRefName,headRefOid,baseRefName,author,closingIssuesReferences,reviews,files",
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


def parse_clarification_request_text(raw: str) -> tuple[dict | None, str | None]:
    if CLARIFICATION_REQUEST_MARKER not in raw:
        return None, None

    after_marker = raw.split(CLARIFICATION_REQUEST_MARKER, maxsplit=1)[1].strip()
    if not after_marker:
        return None, "marker found but payload is empty"

    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", after_marker, flags=re.DOTALL)
    candidates = fenced_matches if fenced_matches else [after_marker]

    parse_errors: list[str] = []
    for candidate in candidates:
        try:
            payload = _first_json_object(candidate)
        except (ValueError, json.JSONDecodeError) as exc:
            parse_errors.append(str(exc))
            continue

        question = _as_optional_string(payload.get("question"))
        if not question:
            parse_errors.append("clarification payload must include a non-empty 'question'")
            continue

        normalized_payload = dict(payload)
        normalized_payload["question"] = question
        normalized_payload["reason"] = _as_optional_string(payload.get("reason")) or question
        return normalized_payload, None

    if parse_errors:
        return None, parse_errors[-1]
    return None, "unable to parse clarification payload"


def latest_clarification_request_from_agent_output(output: str) -> dict | None:
    payload, _error = parse_clarification_request_text(output)
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


def parse_decomposition_plan_comment_body(body: str) -> tuple[dict | None, str | None]:
    if DECOMPOSITION_PLAN_MARKER not in body:
        return None, None

    after_marker = body.split(DECOMPOSITION_PLAN_MARKER, maxsplit=1)[1].strip()
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
    return None, "unable to parse decomposition payload"


def select_latest_parseable_decomposition_plan(
    comments: list[dict],
    source_label: str,
) -> tuple[dict | None, list[str]]:
    latest: dict | None = None
    warnings: list[str] = []

    for comment in comments:
        if not isinstance(comment, dict):
            continue

        body = str(comment.get("body") or "")
        payload, error = parse_decomposition_plan_comment_body(body)
        if payload is None:
            if error:
                created_at = str(comment.get("created_at") or "unknown-time")
                url = str(comment.get("html_url") or "")
                context = f" at {url}" if url else ""
                warnings.append(
                    f"ignoring malformed decomposition comment in {source_label}"
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
            "status": str(payload.get("status") or "").strip().lower(),
        }
        if latest is None or created_at >= str(latest.get("created_at") or ""):
            latest = candidate

    return latest, warnings


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
    refreshed_payload = dict(plan_payload)
    refreshed_created_children = _normalize_created_children(plan_payload.get("created_children") or [])
    blockers: list[str] = []

    for index, child in enumerate(refreshed_created_children):
        issue_number = _as_positive_int(child.get("issue_number"))
        if issue_number is None:
            continue

        child_issue = fetch_issue(repo=repo, number=issue_number)
        child_comments = fetch_issue_comments(repo=repo, issue_number=issue_number)
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

    output = run_capture(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            child_title,
            "--body",
            body,
            "--json",
            "number,url",
        ]
    )
    created = json.loads(output)
    if not isinstance(created, dict):
        raise RuntimeError("Unexpected response from gh issue create")

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
    latest = read_pr_ci_status_for_pull_request(repo=repo, pull_request=pull_request)
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
        latest = read_pr_ci_status_for_pull_request(repo=repo, pull_request=pull_request)
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

    return (
        "You are working on an existing GitHub pull request CI failure cycle in the current git branch.\n"
        "Diagnose the failing CI checks using the provided logs and implement the safest repository fix in files.\n"
        "Do not run git commands; git actions are handled by orchestration script.\n\n"
        f"Pull Request: #{pr_number} - {pr_title}\n"
        f"PR URL: {pr_url}\n\n"
        "PR description:\n"
        f"{pr_body}\n\n"
        "Linked issue context:\n"
        f"{'\n'.join(issue_context_lines)}\n\n"
        "Failing CI checks:\n"
        f"{'\n'.join(failing_lines) if failing_lines else '- No failing checks supplied.'}\n\n"
        "CI diagnostics and failing logs:\n\n"
        f"{'\n\n'.join(diagnostics_lines) if diagnostics_lines else 'No CI diagnostics available.'}\n"
    )


def orchestration_attempt_from_state(state: dict | None) -> int:
    if not isinstance(state, dict):
        return 1
    attempt = state.get("attempt")
    return attempt if type(attempt) is int and attempt > 0 else 1


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
    tracker = issue_tracker(issue)
    issue_ref = str(issue.get("number") or "").strip()
    if tracker == TRACKER_JIRA:
        issue_ref = issue_ref.lower()
    return f"{prefix}/{issue_ref}-{slugify(issue['title'])}"


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
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", branch_name).strip("-") or "pr-branch"


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
    ensure_agent_failure_label(repo=repo, dry_run=dry_run)
    if dry_run:
        print(
            f"[dry-run] Would add label '{AGENT_FAILURE_LABEL_NAME}' to issue #{issue_number}"
        )
        return
    run_command(
        [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--add-label",
            AGENT_FAILURE_LABEL_NAME,
        ]
    )


def issue_has_label(repo: str, issue_number: int, label_name: str) -> bool:
    labels_output = run_capture(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
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


def remove_agent_failure_label_from_issue(repo: str, issue_number: int, dry_run: bool) -> None:
    if dry_run:
        print(
            f"[dry-run] Would remove label '{AGENT_FAILURE_LABEL_NAME}' from issue #{issue_number} if present"
        )
        return

    try:
        if not issue_has_label(repo=repo, issue_number=issue_number, label_name=AGENT_FAILURE_LABEL_NAME):
            return

        run_command(
            [
                "gh",
                "issue",
                "edit",
                str(issue_number),
                "--repo",
                repo,
                "--remove-label",
                AGENT_FAILURE_LABEL_NAME,
            ]
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: failed to remove label '{AGENT_FAILURE_LABEL_NAME}' from issue #{issue_number}: {exc}",
            file=sys.stderr,
        )


def safe_report_issue_automation_failure(
    repo: str,
    issue_number: int,
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
                f"[dry-run] Would post agent failure report comment to issue #{issue_number}: "
                f"stage={stage} run_id={run_id}"
            )
        else:
            run_command(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(issue_number),
                    "--repo",
                    repo,
                    "--body",
                    body,
                ]
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


def build_issue_scope_skip_comment(issue_number: int, reason: str, forced: bool) -> str:
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
    issue_number: int,
    payload: dict,
    dry_run: bool,
) -> None:
    if dry_run:
        print(
            f"[dry-run] Would post decomposition plan to issue #{issue_number}: "
            f"children={len(payload.get('proposed_children') or [])}"
        )
        return

    run_command(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            repo,
            "--body",
            format_decomposition_plan_comment(payload),
        ]
    )


def safe_post_issue_scope_skip_comment(
    repo: str,
    issue_number: int,
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
                f"[dry-run] Would post scope decision comment to issue #{issue_number}: "
                f"decision={'forced-in-scope' if forced else 'out-of-scope'}"
            )
            return

        run_command(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo,
                "--body",
                body,
            ]
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
    if force_issue_flow:
        return "issue-flow", "--force-issue-flow is set"

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
            f"recovered orchestration state is {recovered_status}; skipping until explicitly forced",
        )

    if recovered_status == "waiting-for-ci" and linked_open_pr is not None:
        pr_number = linked_open_pr.get("number")
        return (
            "pr-review",
            f"recovered orchestration state is waiting-for-ci and linked open PR #{pr_number} exists",
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
    run_stats: dict[str, object] | None = None,
    agent_result: dict[str, object] | None = None,
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
        run_stats=run_stats,
        agent_result=agent_result,
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
    run_stats: dict[str, object] | None = None,
    agent_result: dict[str, object] | None = None,
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


def commit_changes(
    issue: dict,
    dry_run: bool,
    pre_run_untracked_files: set[str] | None = None,
) -> str:
    message = issue_commit_title(issue)
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
    stage_worktree_changes(pre_run_untracked_files)
    run_command(["git", "commit", "-m", message])

    residual_untracked_files = residual_untracked_files_after_baseline(pre_run_untracked_files)
    if residual_untracked_files:
        raise ResidualUntrackedFilesError(
            files=residual_untracked_files,
            stage="issue_commit_validation",
        )

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


def commit_pr_review_changes(
    pull_request: dict,
    dry_run: bool,
    pre_run_untracked_files: set[str] | None = None,
) -> str:
    message = f"Address review comments for PR #{pull_request['number']}"
    if dry_run:
        print(f"[dry-run] Would commit with message: {message}")
        return message
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


def open_pr(
    repo: str,
    base_branch: str,
    branch_name: str,
    issue: dict,
    dry_run: bool,
    stacked_base_context: str | None = None,
) -> str:
    issue_ref = format_issue_ref_from_issue(issue)
    title = issue_commit_title(issue)
    body = (
        "## Summary\n"
        f"- Implements fix for {issue_ref}\n"
        f"- Source issue: {issue['url']}\n\n"
    )
    if issue_tracker(issue) == TRACKER_GITHUB:
        body += f"Closes {issue_ref}\n"
    if stacked_base_context:
        body += (
            "\n## Stack Context\n"
            f"- Stacked on current branch: `{stacked_base_context}`\n"
            f"- Base for this PR is `{stacked_base_context}` (not repository default branch)\n"
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
    stacked_base_context: str | None = None,
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
        stacked_base_context=stacked_base_context,
    )
    return "created", pr_url


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


def _validate_project_workflow(config: dict, config_path: str) -> None:
    supported_workflow_keys = {"commands"}
    unsupported_workflow = sorted(set(config) - supported_workflow_keys)
    if unsupported_workflow:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'workflow': "
            + ", ".join(unsupported_workflow)
        )

    commands = config.get("commands")
    if commands is None:
        return
    if not isinstance(commands, dict):
        raise RuntimeError("Project config key 'workflow.commands' must be an object")

    supported_commands = {"test", "lint", "build"}
    unsupported_commands = sorted(set(commands) - supported_commands)
    if unsupported_commands:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'workflow.commands': "
            + ", ".join(unsupported_commands)
        )

    for key in supported_commands:
        if key in commands and commands[key] is not None and not isinstance(commands[key], str):
            raise RuntimeError(
                f"Project config key 'workflow.commands.{key}' must be a string or null"
            )
        if key in commands and isinstance(commands[key], str) and not commands[key].strip():
            raise RuntimeError(
                f"Project config key 'workflow.commands.{key}' must be a non-empty string or null"
            )


def _validate_project_defaults(config: dict, config_path: str) -> None:
    supported_defaults_keys = {
        "runner",
        "agent",
        "model",
        "track_tokens",
        "token_budget",
        "preset",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "max_attempts",
    }
    unsupported_defaults = sorted(set(config) - supported_defaults_keys)
    if unsupported_defaults:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'defaults': "
            + ", ".join(unsupported_defaults)
        )

    if "runner" in config and config["runner"] not in {"claude", "opencode"}:
        raise RuntimeError("Project config key 'defaults.runner' must be one of: claude, opencode")

    if "agent" in config:
        value = config["agent"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Project config key 'defaults.agent' must be a non-empty string")

    if "model" in config and config["model"] is not None and not isinstance(config["model"], str):
        raise RuntimeError("Project config key 'defaults.model' must be a string or null")

    if "track_tokens" in config and not isinstance(config["track_tokens"], bool):
        raise RuntimeError("Project config key 'defaults.track_tokens' must be a boolean")

    if "token_budget" in config:
        value = config["token_budget"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                "Project config key 'defaults.token_budget' must be a positive integer or null"
            )

    if "preset" in config:
        value = config["preset"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Project config key 'defaults.preset' must be a non-empty string")

    if "agent_timeout_seconds" in config:
        value = config["agent_timeout_seconds"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(
                "Project config key 'defaults.agent_timeout_seconds' must be a positive integer"
            )

    if "agent_idle_timeout_seconds" in config:
        value = config["agent_idle_timeout_seconds"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                "Project config key 'defaults.agent_idle_timeout_seconds' must be a positive integer or null"
            )

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(
                "Project config key 'defaults.max_attempts' must be a positive integer"
            )


def _validate_project_scope(config: dict, config_path: str) -> None:
    supported_scope_keys = {"defaults"}
    unsupported_scope = sorted(set(config) - supported_scope_keys)
    if unsupported_scope:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'scope': "
            + ", ".join(unsupported_scope)
        )

    if "defaults" in config and not isinstance(config["defaults"], dict):
        raise RuntimeError("Project config key 'scope.defaults' must be an object")

    defaults = config.get("defaults")
    if not isinstance(defaults, dict):
        return

    supported_defaults_keys = {"labels", "authors"}
    unsupported_defaults = sorted(set(defaults) - supported_defaults_keys)
    if unsupported_defaults:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'scope.defaults': "
            + ", ".join(unsupported_defaults)
        )

    for section_key in ["labels", "authors"]:
        section = defaults.get(section_key)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise RuntimeError(
                f"Project config key 'scope.defaults.{section_key}' must be an object"
            )

        supported_section_keys = {"allow", "deny"}
        unsupported_section = sorted(set(section) - supported_section_keys)
        if unsupported_section:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'scope.defaults.{section_key}': "
                + ", ".join(unsupported_section)
            )

        for rule_key in ["allow", "deny"]:
            values = section.get(rule_key)
            if values is None:
                continue
            if not isinstance(values, list):
                raise RuntimeError(
                    f"Project config key 'scope.defaults.{section_key}.{rule_key}' must be an array of strings"
                )
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(
                        f"Project config key 'scope.defaults.{section_key}.{rule_key}' must contain non-empty strings"
                    )


def _validate_project_retry(config: dict, config_path: str) -> None:
    supported_retry_keys = {"max_attempts", "escalate_to_preset"}
    unsupported_retry = sorted(set(config) - supported_retry_keys)
    if unsupported_retry:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'retry': "
            + ", ".join(unsupported_retry)
        )

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError("Project config key 'retry.max_attempts' must be a positive integer")

    if "escalate_to_preset" in config:
        value = config["escalate_to_preset"]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RuntimeError(
                "Project config key 'retry.escalate_to_preset' must be a non-empty string or null"
            )


def _validate_preset_config(config: dict, config_path: str, prefix: str) -> None:
    supported_preset_keys = {
        "runner",
        "agent",
        "model",
        "track_tokens",
        "token_budget",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "max_attempts",
        "escalate_to_preset",
    }
    unsupported = sorted(set(config) - supported_preset_keys)
    if unsupported:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under '{prefix}': "
            + ", ".join(unsupported)
        )

    if "runner" in config and config["runner"] not in {"claude", "opencode"}:
        raise RuntimeError(f"Project config key '{prefix}.runner' must be one of: claude, opencode")

    if "agent" in config:
        value = config["agent"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Project config key '{prefix}.agent' must be a non-empty string")

    if "model" in config and config["model"] is not None and not isinstance(config["model"], str):
        raise RuntimeError(f"Project config key '{prefix}.model' must be a string or null")

    if "track_tokens" in config and not isinstance(config["track_tokens"], bool):
        raise RuntimeError(f"Project config key '{prefix}.track_tokens' must be a boolean")

    if "token_budget" in config:
        value = config["token_budget"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(f"Project config key '{prefix}.token_budget' must be a positive integer or null")

    if "agent_timeout_seconds" in config:
        value = config["agent_timeout_seconds"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(f"Project config key '{prefix}.agent_timeout_seconds' must be a positive integer")

    if "agent_idle_timeout_seconds" in config:
        value = config["agent_idle_timeout_seconds"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                f"Project config key '{prefix}.agent_idle_timeout_seconds' must be a positive integer or null"
            )

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(f"Project config key '{prefix}.max_attempts' must be a positive integer")

    if "escalate_to_preset" in config:
        value = config["escalate_to_preset"]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RuntimeError(
                f"Project config key '{prefix}.escalate_to_preset' must be a non-empty string or null"
            )


def _validate_project_presets(config: dict, config_path: str) -> None:
    for preset_name, preset_config in config.items():
        if not isinstance(preset_name, str) or not preset_name.strip():
            raise RuntimeError(
                f"Project config {config_path} preset names must be non-empty strings"
            )
        if not isinstance(preset_config, dict):
            raise RuntimeError(
                f"Project config key 'presets.{preset_name}' must be an object"
            )
        _validate_preset_config(preset_config, config_path, f"presets.{preset_name}")


def _validate_retry_references(config: dict, preset_names: set[str], config_path: str, prefix: str) -> None:
    target = _as_optional_string(config.get("escalate_to_preset"))
    if target is not None and target not in preset_names:
        raise RuntimeError(
            f"Project config key '{prefix}.escalate_to_preset' references unknown preset '{target}'"
        )


def _validate_project_communication(config: dict, config_path: str) -> None:
    supported_communication_keys = {"verbosity"}
    unsupported_communication = sorted(set(config) - supported_communication_keys)
    if unsupported_communication:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'communication': "
            + ", ".join(unsupported_communication)
        )

    if "verbosity" in config and config["verbosity"] not in {"low", "normal", "high"}:
        raise RuntimeError(
            "Project config key 'communication.verbosity' must be one of: low, normal, high"
        )


def validate_project_config(config: dict, config_path: str) -> dict:
    supported_top_level_keys = {
        "workflow",
        "defaults",
        "scope",
        "retry",
        "communication",
        "presets",
    }

    unsupported = sorted(set(config) - supported_top_level_keys)
    if unsupported:
        unsupported_text = ", ".join(unsupported)
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path}: {unsupported_text}"
        )

    for key in ["workflow", "defaults", "scope", "retry", "communication", "presets"]:
        if key in config and not isinstance(config[key], dict):
            raise RuntimeError(f"Project config key '{key}' must be an object")

    workflow = config.get("workflow")
    if isinstance(workflow, dict):
        _validate_project_workflow(workflow, config_path)

    defaults = config.get("defaults")
    if isinstance(defaults, dict):
        _validate_project_defaults(defaults, config_path)

    scope = config.get("scope")
    if isinstance(scope, dict):
        _validate_project_scope(scope, config_path)

    retry = config.get("retry")
    if isinstance(retry, dict):
        _validate_project_retry(retry, config_path)

    communication = config.get("communication")
    if isinstance(communication, dict):
        _validate_project_communication(communication, config_path)

    presets = config.get("presets")
    if isinstance(presets, dict):
        _validate_project_presets(presets, config_path)

    preset_names = set(presets) if isinstance(presets, dict) else set()
    if isinstance(defaults, dict):
        default_preset = _as_optional_string(defaults.get("preset"))
        if default_preset is not None and default_preset not in preset_names:
            raise RuntimeError(
                f"Project config key 'defaults.preset' references unknown preset '{default_preset}'"
            )
    if isinstance(retry, dict):
        _validate_retry_references(retry, preset_names, config_path, "retry")
    if isinstance(presets, dict):
        for preset_name, preset_config in presets.items():
            if isinstance(preset_config, dict):
                _validate_retry_references(
                    preset_config,
                    preset_names,
                    config_path,
                    f"presets.{preset_name}",
                )

    return config


def load_project_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, encoding="utf-8") as config_file:
            data = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in project config {config_path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read project config {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Project config {config_path} must contain a JSON object")

    return validate_project_config(config=data, config_path=config_path)


def project_cli_defaults(project_config: dict) -> dict:
    defaults = project_config.get("defaults")
    cli_defaults: dict = {}
    if isinstance(defaults, dict):
        for key in [
            "runner",
            "agent",
            "model",
            "track_tokens",
            "token_budget",
            "preset",
            "agent_timeout_seconds",
            "agent_idle_timeout_seconds",
            "max_attempts",
        ]:
            if key in defaults:
                cli_defaults[key] = defaults[key]

    retry = project_config.get("retry")
    if isinstance(retry, dict):
        for key in ["max_attempts", "escalate_to_preset"]:
            if key in retry:
                cli_defaults[key] = retry[key]
    return cli_defaults


def validate_local_config(config: dict, config_path: str) -> dict:
    supported_keys = {
        "tracker",
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

    local_config_path = resolve_local_config_path(getattr(args, "local_config", None), target_dir)
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
        description="Fetch GitHub issues with gh and run an AI agent for each issue body."
    )
    parser.add_argument(
        "--repo", help="GitHub repo in owner/name format. Defaults to current gh repo."
    )
    parser.add_argument(
        "--tracker",
        default=BUILTIN_DEFAULTS["tracker"],
        choices=sorted(TRACKER_CHOICES),
        help="Issue tracker to fetch from (default: github).",
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
    from_review_comments = bool(getattr(args, "from_review_comments", False))
    force_issue_flow = bool(getattr(args, "force_issue_flow", False))
    has_force_issue_flow_flag = hasattr(args, "force_issue_flow")
    skip_if_pr_exists = bool(getattr(args, "skip_if_pr_exists", False))
    skip_if_branch_exists = bool(getattr(args, "skip_if_branch_exists", False))
    force_reprocess = bool(getattr(args, "force_reprocess", False))
    pr_followup_branch_prefix = getattr(args, "pr_followup_branch_prefix", None)
    allow_pr_branch_switch = bool(getattr(args, "allow_pr_branch_switch", False))
    isolate_worktree = bool(getattr(args, "isolate_worktree", False))
    post_pr_summary = bool(getattr(args, "post_pr_summary", False))
    track_tokens = bool(getattr(args, "track_tokens", False))
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
            workflow_checks = configured_workflow_commands(project_config)

            if workflow_checks:
                configured_names = ", ".join(name for name, _ in workflow_checks)
                prefix = "[dry-run] " if args.dry_run else ""
                print(f"{prefix}Configured workflow checks: {configured_names}")
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
            if from_review_comments and pr_number_arg is None:
                raise RuntimeError("--from-review-comments requires --pr <number>.")
            if pr_number_arg is not None and not from_review_comments:
                raise RuntimeError("--pr requires --from-review-comments.")
            validate_tracker_requirements(tracker=tracker, pr_mode_requested=pr_mode_requested)
            if issue_number_arg is not None:
                issue_number_arg = normalize_issue_number(issue_number_arg, tracker=tracker)

            if not pr_mode_requested and base_branch_mode == "current":
                for warning in current_branch_stack_warnings():
                    print(f"Warning: {warning}", file=sys.stderr)

            ensure_clean_worktree()
            repo = args.repo or detect_repo()
            if pr_mode_requested:
                base_branch = ""
                issues = []
            else:
                if base_branch_mode == "current":
                    base_branch = current_branch()
                else:
                    base_branch = detect_default_branch(repo)
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
                    if tracker == TRACKER_JIRA:
                        issues = [fetch_jira_issue(issue_key=str(issue_number_arg))]
                    else:
                        issues = [fetch_issue(repo=repo, number=issue_number_arg)]
                else:
                    if tracker == TRACKER_JIRA:
                        jira_jql = {
                            "open": "status != Done ORDER BY created DESC",
                            "closed": "status = Done ORDER BY created DESC",
                            "all": "ORDER BY created DESC",
                        }[args.state]
                        issues = fetch_jira_issues(jql=jira_jql, limit=args.limit)
                    else:
                        issues = fetch_issues(repo=repo, state=args.state, limit=args.limit)
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
            pr_attempt = 1
            if type(pr_number_arg) is not int:
                raise RuntimeError("--pr must be an integer pull request number")

            failure_stage = "fetch_pr"
            pull_request = fetch_pull_request(repo=repo, number=pr_number_arg)
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

            recovered_pr_state: dict | None = None
            pr_clarification_answer: dict | None = None
            try:
                pr_comments = fetch_issue_comments(repo=repo, issue_number=pr_number_arg)
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
            if pr_clarification_answer is None:
                pr_clarification_answer = find_waiting_for_author_answer(
                    comments=pr_comments if "pr_comments" in locals() else [],
                    recovered_state=recovered_pr_state,
                    author_login=pr_author_login,
                )

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
                    attempt=pr_attempt,
                    stage="agent_run",
                    next_action="wait_for_agent_result",
                    error=None,
                    decomposition=pr_recovered_decomposition_rollup,
                ),
            )

            if not review_items:
                ci_prompt_override: str | None = None
                recovered_pr_status = ""
                if isinstance(recovered_pr_state, dict):
                    recovered_pr_status = str(recovered_pr_state.get("status") or "")

                if recovered_pr_status == "waiting-for-ci":
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
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
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
                        pr_attempt = retry_attempt
                        linked_issues = load_linked_issue_context(repo=repo, pull_request=pull_request)
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

                    if ci_overall == "success":
                        required_file_validation = validate_required_files_in_pr(
                            pull_request=pull_request,
                            linked_issues=load_linked_issue_context(repo=repo, pull_request=pull_request),
                        )

                        if required_file_validation.get("status") == "blocked":
                            missing_files = required_file_validation.get("missing_files")
                            missing_summary = ", ".join(sorted(str(file) for file in missing_files))
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
                                    next_action="update_pr_with_required_files",
                                    error=f"Missing required file evidence: {missing_summary}",
                                    ci_checks=ci_checks_payload,
                                    decomposition=pr_recovered_decomposition_rollup,
                                    required_file_validation=required_file_validation,
                                ),
                            )
                            print(
                                f"PR #{pr_number_arg} CI passed but required file evidence check failed. "
                                f"Missing files: {missing_summary}"
                            )
                            return _finish_main(0, original_process_cwd)

                        safe_post_orchestration_state_comment(
                            repo=repo,
                            target_type="pr",
                            target_number=pr_number_arg,
                            dry_run=args.dry_run,
                            state=build_orchestration_state(
                                status="ready-to-merge",
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
                                next_action="ready_for_merge",
                                error=None,
                                ci_checks=ci_checks_payload,
                                decomposition=pr_recovered_decomposition_rollup,
                                required_file_validation=required_file_validation,
                            ),
                        )
                        print(
                            f"CI checks passed for PR #{pr_number_arg}; marking orchestration state as ready-to-merge."
                        )
                        return _finish_main(0, original_process_cwd)

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
                        attempt=pr_attempt,
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

            linked_issues = load_linked_issue_context(repo=repo, pull_request=pull_request)
            prompt = ci_prompt_override if 'ci_prompt_override' in locals() and ci_prompt_override is not None else build_pr_review_prompt(
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
            pr_agent_run_stats: dict[str, object] = {}
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
                            attempt=1,
                            stage="agent_run",
                            next_action="await_author_reply",
                            error=reason or question,
                            stats=pr_agent_run_stats,
                            decomposition=pr_recovered_decomposition_rollup,
                        ) | {"question": question, "reason": reason},
                    )
                    print(f"Paused PR #{pr_number_arg} for author clarification: {question}")
                    if pr_followup_branch_prefix and not args.dry_run:
                        run_command(["git", "checkout", base_branch_for_run])
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
                        attempt=pr_attempt,
                        stage="post_agent_check",
                        next_action="await_more_feedback_or_manual_changes",
                        error="Agent produced no repository changes",
                        stats=pr_agent_run_stats,
                        decomposition=pr_recovered_decomposition_rollup,
                    ),
                )
                print(f"No changes detected for PR #{pr_number_arg}; skipping commit and push")
                if pr_followup_branch_prefix:
                    run_command(["git", "checkout", base_branch_for_run])
                return _finish_main(0, original_process_cwd)

            failure_stage = "commit_push"
            commit_pr_review_changes(
                pull_request=pull_request,
                dry_run=args.dry_run,
                pre_run_untracked_files=pre_run_untracked_files,
            )

            failure_stage = "workflow_checks"
            workflow_check_results = run_configured_workflow_checks(
                checks=workflow_checks,
                dry_run=args.dry_run,
                cwd=os.getcwd(),
            )

            failure_stage = "commit_push"
            if pr_followup_branch_prefix:
                push_branch(branch_name=active_branch, dry_run=args.dry_run)
                print(f"Pushed follow-up branch for PR #{pr_number_arg}: {active_branch}")
            else:
                push_branch(branch_name=active_branch, dry_run=args.dry_run)

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
                        attempt=pr_attempt,
                        stage="changes_pushed",
                        next_action="wait_for_ci",
                        error=None,
                        workflow_checks=workflow_check_results,
                        stats=pr_agent_run_stats,
                        decomposition=pr_recovered_decomposition_rollup,
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
            return _finish_main(0, original_process_cwd)
        except Exception as exc:  # noqa: BLE001
            if pr_number_arg is not None:
                failed_pr_number = pr_state_context.get("pr")
                if type(failed_pr_number) is int:
                    if isinstance(exc, ResidualUntrackedFilesError):
                        failure_stage = "residual_untracked_validation"
                    elif isinstance(exc, TokenBudgetExceededError):
                        failure_stage = "token_budget"

                    failure_status = failure_state_for_stage(failure_stage)
                    next_action = failure_next_action_for_stage(failure_stage)
                    workflow_results = (
                        exc.checks if isinstance(exc, WorkflowCheckFailure) else None
                    )
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
                            runner=args.runner,
                            agent=args.agent,
                            model=args.model,
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

    run_id = generate_run_id()
    failures = 0
    processed = 0
    skipped_existing_pr = 0
    skipped_existing_branch = 0
    skipped_out_of_scope = 0
    touched_prs: list[str] = []
    reported_issue_failures: set[int] = set()

    for issue in issues:
        try:
            failure_stage = "issue_setup"
            workflow_check_results: list[dict] | None = None
            linked_open_pr: dict | None = None
            recovered_state: dict | None = None
            recovered_status = ""
            mode = "issue-flow"
            mode_reason = "batch issue processing"
            force_override_applied = False
            skip_agent_run = False
            supports_github_issue_ops = False
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
            state_attempt = 1
            supports_github_issue_ops = issue_tracker(issue) == TRACKER_GITHUB and type(issue["number"]) is int

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
                    if supports_github_issue_ops:
                        safe_post_issue_scope_skip_comment(
                            repo=repo,
                            issue_number=issue["number"],
                            reason=scope_reason,
                            forced=True,
                            dry_run=args.dry_run,
                        )
                else:
                    skipped_out_of_scope += 1
                    if supports_github_issue_ops:
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
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=1,
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

            if skip_if_pr_exists and supports_github_issue_ops:
                linked_open_pr = find_open_pr_for_issue(repo=repo, issue_number=issue["number"])
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
                skipped_existing_branch += 1
                print(
                    f"Skipping {issue_label}: branch '{issue_branch}' already exists on origin "
                    "(--force-reprocess or --no-skip-if-branch-exists to override)."
                )
                continue

            if issue_number_arg is not None and has_force_issue_flow_flag and supports_github_issue_ops:
                if linked_open_pr is None:
                    linked_open_pr = find_open_pr_for_issue(repo=repo, issue_number=issue["number"])

                recovered_issue_state: dict | None = None
                try:
                    issue_comments = fetch_issue_comments(repo=repo, issue_number=issue["number"])
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
                    print(
                        f"Skipping {issue_label}: {mode_reason} "
                        "(use --force-issue-flow to override)."
                    )
                    continue

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
                latest_plan = None
                plan_warnings: list[str] = []
                latest_payload_dict = {}
                latest_plan_is_execution_ready = False
                if should_plan or should_check_existing_decomposition_plan(issue, assessment):
                    issue_comments = fetch_issue_comments(repo=repo, issue_number=issue["number"])
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
                                    created_child = create_decomposition_child_issue(
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
                                        attempt=1,
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
                                        attempt=1,
                                        stage="decomposition_plan",
                                        next_action="create_missing_child_issues",
                                        error="Approved decomposition plan still has missing child issues",
                                        decomposition=decomposition_rollup,
                                    ),
                                )
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
                                        attempt=1,
                                        stage="decomposition_execution",
                                        next_action=str(
                                            latest_payload_dict.get("next_action")
                                            or "review_completed_children"
                                        ),
                                        error=None,
                                        decomposition=decomposition_rollup,
                                    ),
                                )
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
                                    attempt=1,
                                    stage="decomposition_execution",
                                    next_action="run_selected_child_issue",
                                    error=None,
                                    decomposition=decomposition_rollup,
                                ),
                            )
                            issue = fetch_issue(repo=repo, number=selected_child_issue_number)
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
                                attempt=1,
                                stage="decomposition_plan",
                                next_action="approve_plan_or_rerun_with_decompose_never",
                                error="Task requires planning-only decomposition before implementation",
                                decomposition=decomposition_rollup,
                            ),
                        )
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

                    if not review_items:
                        if should_force_sync_rerun:
                            skip_agent_run = True
                            print(
                                f"No actionable review comments for linked PR #{pr_number}; "
                                f"continuing with sync-only rerun because mergeStateStatus={merge_state}"
                            )
                        elif recovered_status == "waiting-for-ci":
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
                                    continue

                                retry_attempt = current_attempt + 1
                                state_attempt = retry_attempt
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
                                    ),
                                )
                                print(
                                    f"No actionable review comments for linked PR #{pr_number}; "
                                    f"CI is failing: {diagnostics_summary}. "
                                    f"Running CI fix attempt {retry_attempt}/{max_attempts}."
                                )

                            if ci_overall == "success":
                                required_file_validation = validate_required_files_in_pr(
                                    pull_request=pull_request,
                                    linked_issues=[issue],
                                )

                                if required_file_validation.get("status") == "blocked":
                                    missing_files = required_file_validation.get("missing_files")
                                    missing_summary = ", ".join(sorted(str(file) for file in missing_files))
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
                                            next_action="update_pr_with_required_files",
                                            error=f"Missing required file evidence: {missing_summary}",
                                            ci_checks=ci_checks_payload,
                                            decomposition=decomposition_rollup,
                                            required_file_validation=required_file_validation,
                                        ),
                                    )
                                    print(
                                        f"No actionable review comments for linked PR #{pr_number}; "
                                        "CI checks passed but required file evidence check failed. "
                                        f"Missing files: {missing_summary}"
                                    )
                                    remove_agent_failure_label_from_issue(
                                        repo=repo,
                                        issue_number=issue["number"],
                                        dry_run=args.dry_run,
                                    )
                                    continue

                                safe_post_orchestration_state_comment(
                                    repo=repo,
                                    target_type="pr",
                                    target_number=state_target_number,
                                    dry_run=args.dry_run,
                                    state=build_orchestration_state(
                                        status="ready-to-merge",
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
                                        next_action="ready_for_merge",
                                        error=None,
                                        ci_checks=ci_checks_payload,
                                        decomposition=decomposition_rollup,
                                        required_file_validation=required_file_validation,
                                    ),
                                )
                                print(
                                    f"No actionable review comments for linked PR #{pr_number}; "
                                    "CI checks passed, marking ready-to-merge."
                                )
                                remove_agent_failure_label_from_issue(
                                    repo=repo,
                                    issue_number=issue["number"],
                                    dry_run=args.dry_run,
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

                stacked_base_context = (
                    target_base_branch if mode == "issue-flow" and base_branch_mode == "current" else None
                )

                if mode == "issue-flow":
                    if supports_github_issue_ops:
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
                print(f"Branch status for {issue_label}: {branch_status}")

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

                pre_run_untracked_files: set[str] | None = None
                if not skip_agent_run and not args.dry_run:
                    pre_run_untracked_files = list_untracked_files()

                if skip_agent_run:
                    print(
                        f"Skipping agent run for {issue_label} in pr-review mode: "
                        "no actionable review comments; running sync-only path"
                    )
                else:
                    failure_stage = "agent_run"
                    issue_agent_run_stats = {}
                    agent_result: dict[str, object] = {}
                    exit_code = run_agent(
                        issue=issue,
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        dry_run=args.dry_run,
                        timeout_seconds=args.agent_timeout_seconds,
                        idle_timeout_seconds=args.agent_idle_timeout_seconds,
                        opencode_auto_approve=args.opencode_auto_approve,
                        image_paths=issue_image_paths,
                        prompt_override=prompt_override,
                        track_tokens=track_tokens,
                        token_budget=token_budget,
                        run_stats=issue_agent_run_stats,
                        agent_result=agent_result,
                    )
                    if exit_code != 0:
                        exit_summary = describe_exit_code(exit_code)
                        diagnosis = classify_opencode_failure(
                            return_code=exit_code,
                            model=args.model,
                        ) if args.runner == "opencode" else None
                        message = (
                            f"Agent failed for {issue_label} with {exit_summary}"
                            + (f" ({diagnosis})" if diagnosis else "")
                        )
                        raise RuntimeError(message)
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
                                    runner=args.runner,
                                    agent=args.agent,
                                    model=args.model,
                                    attempt=1,
                                    stage="agent_run",
                                    next_action="await_author_reply",
                                    error=reason or question,
                                    stats=issue_agent_run_stats,
                                    decomposition=decomposition_rollup,
                                ) | {"question": question, "reason": reason},
                            )
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
                                    runner=args.runner,
                                    agent=args.agent,
                                    model=args.model,
                                    plan_payload=decomposition_parent_payload,
                                    dry_run=args.dry_run,
                                )
                            if not args.dry_run:
                                run_command(["git", "checkout", base_branch])
                            continue
                if not args.dry_run and not has_changes():
                    if branch_status == "reused" and args.sync_reused_branch and reused_branch_sync_changed:
                        print(
                            f"No file changes from agent for {issue_label}; "
                            "pushing sync-only branch updates"
                        )
                        used_force_with_lease = args.sync_strategy == "rebase"
                        push_branch(
                            branch_name=issue_branch,
                            dry_run=False,
                            force_with_lease=used_force_with_lease,
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
                            stacked_base_context=stacked_base_context,
                        )
                        if pr_url:
                            touched_prs.append(pr_url)
                            print(f"PR status for {issue_label}: {pr_status} ({pr_url})")
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
                                    attempt=state_attempt,
                                    stage="changes_pushed",
                                    next_action="wait_for_ci",
                                    error=None,
                                    stats=issue_agent_run_stats,
                                    decomposition=decomposition_rollup,
                                ),
                            )
                        elif mode == "issue-flow" and supports_github_issue_ops:
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
                        run_command(["git", "checkout", base_branch])
                        continue

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
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=state_attempt,
                                stage="post_agent_check",
                                next_action="await_more_context",
                                error="No changes produced",
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
                    run_command(["git", "checkout", base_branch])
                    continue

                failure_stage = "commit_push"
                commit_changes(
                    issue=issue,
                    dry_run=args.dry_run,
                    pre_run_untracked_files=pre_run_untracked_files,
                )

                failure_stage = "workflow_checks"
                workflow_check_results = run_configured_workflow_checks(
                    checks=workflow_checks,
                    dry_run=args.dry_run,
                    cwd=os.getcwd(),
                )

                failure_stage = "commit_push"
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
                    stacked_base_context=stacked_base_context,
                )
                if pr_url:
                    touched_prs.append(pr_url)
                    print(f"PR status for {issue_label}: {pr_status} ({pr_url})")
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
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=1,
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
                                runner=args.runner,
                                agent=args.agent,
                                model=args.model,
                                attempt=state_attempt,
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

                if not args.dry_run:
                    run_command(["git", "checkout", base_branch])
                if supports_github_issue_ops:
                    remove_agent_failure_label_from_issue(
                        repo=repo,
                        issue_number=issue["number"],
                        dry_run=args.dry_run,
                    )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if isinstance(exc, ResidualUntrackedFilesError):
                failure_stage = "residual_untracked_validation"
            elif isinstance(exc, TokenBudgetExceededError):
                failure_stage = "token_budget"

            failure_status = failure_state_for_stage(failure_stage)
            next_action = failure_next_action_for_stage(failure_stage)
            workflow_results = exc.checks if isinstance(exc, WorkflowCheckFailure) else None
            residual_untracked_files = (
                exc.files if isinstance(exc, ResidualUntrackedFilesError) else None
            )
            if supports_github_issue_ops or mode == "pr-review":
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
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
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
                        runner=args.runner,
                        agent=args.agent,
                        model=args.model,
                        plan_payload=decomposition_parent_payload,
                        dry_run=args.dry_run,
                    )
                except Exception as parent_exc:  # noqa: BLE001
                    print(
                        "Warning: failed to refresh parent decomposition roll-up for issue "
                        f"#{decomposition_parent_issue['number']}: {parent_exc}",
                        file=sys.stderr,
                    )
            if supports_github_issue_ops:
                safe_report_issue_automation_failure(
                    repo=repo,
                    issue_number=issue["number"],
                    run_id=run_id,
                    stage=failure_stage,
                    error=str(exc),
                    branch=locals().get("issue_branch", None),
                    base_branch=locals().get("target_base_branch", None),
                    runner=args.runner,
                    agent=args.agent,
                    model=args.model,
                    residual_untracked_files=residual_untracked_files,
                    next_action=next_action,
                    dry_run=args.dry_run,
                    already_reported_issue_numbers=reported_issue_failures,
                )
            print(f"{issue_label.capitalize()} failed: {exc}", file=sys.stderr)
            if args.stop_on_error:
                break

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
    return _finish_main(1 if failures > 0 else 0, original_process_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
