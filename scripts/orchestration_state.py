"""Helpers for orchestration state, claim, and decomposition comments.

This module keeps the runner entrypoint thin by isolating low-risk parsing and
selection helpers that are exercised directly by tests and reused across the
issue orchestration flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
from typing import Callable


ORCHESTRATION_STATE_MARKER = "<!-- orchestration-state:v1 -->"
ORCHESTRATION_CLAIM_MARKER = "<!-- orchestration-claim:v1 -->"
DECOMPOSITION_PLAN_MARKER = "<!-- orchestration-decomposition:v1 -->"
CLARIFICATION_REQUEST_MARKER = "<!-- orchestration-clarification-request:v1 -->"


def _as_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _first_json_object(raw: str) -> dict:
    start = raw.find("{")
    if start < 0:
        raise ValueError("state payload is missing JSON object")
    payload, _offset = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(payload, dict):
        raise ValueError("state payload JSON must be an object")
    return payload


def _parse_marked_json_payload(body: str, marker: str, missing_error: str) -> tuple[dict | None, str | None]:
    if marker not in body:
        return None, None

    after_marker = body.split(marker, maxsplit=1)[1].strip()
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
    return None, missing_error


def _build_latest_parseable_comment(
    comments: list[dict],
    source_label: str,
    comment_kind: str,
    parse_comment: Callable[[str], tuple[dict | None, str | None]],
    status_from_payload: Callable[[dict], str],
) -> tuple[dict | None, list[str]]:
    latest: dict | None = None
    warnings: list[str] = []

    for comment in comments:
        if not isinstance(comment, dict):
            continue

        body = str(comment.get("body") or "")
        payload, error = parse_comment(body)
        if payload is None:
            if error:
                created_at = str(comment.get("created_at") or "unknown-time")
                url = str(comment.get("html_url") or "")
                context = f" at {url}" if url else ""
                warnings.append(
                    f"ignoring malformed {comment_kind} comment in {source_label}"
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
            "status": status_from_payload(payload),
        }
        if latest is None or created_at >= str(latest.get("created_at") or ""):
            latest = candidate

    return latest, warnings


def _parse_iso_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_clarification_request_text(raw: str) -> tuple[dict | None, str | None]:
    payload, error = _parse_marked_json_payload(
        raw,
        CLARIFICATION_REQUEST_MARKER,
        "unable to parse clarification payload",
    )
    if payload is None:
        return None, error

    question = _as_optional_string(payload.get("question"))
    if not question:
        return None, "clarification payload must include a non-empty 'question'"

    normalized_payload = dict(payload)
    normalized_payload["question"] = question
    normalized_payload["reason"] = _as_optional_string(payload.get("reason")) or question
    return normalized_payload, None


def latest_clarification_request_from_agent_output(output: str) -> dict | None:
    payload, _error = parse_clarification_request_text(output)
    return payload


def parse_orchestration_state_comment_body(body: str) -> tuple[dict | None, str | None]:
    return _parse_marked_json_payload(
        body,
        ORCHESTRATION_STATE_MARKER,
        "unable to parse state payload",
    )


def normalize_orchestration_state_status(state_payload: dict) -> str:
    status_raw = state_payload.get("status")
    if not isinstance(status_raw, str):
        status_raw = state_payload.get("state")
    return str(status_raw or "").strip().lower()


def select_latest_parseable_orchestration_state(
    comments: list[dict],
    source_label: str,
) -> tuple[dict | None, list[str]]:
    return _build_latest_parseable_comment(
        comments=comments,
        source_label=source_label,
        comment_kind="orchestration state",
        parse_comment=parse_orchestration_state_comment_body,
        status_from_payload=normalize_orchestration_state_status,
    )


def parse_orchestration_claim_comment_body(body: str) -> tuple[dict | None, str | None]:
    return _parse_marked_json_payload(
        body,
        ORCHESTRATION_CLAIM_MARKER,
        "unable to parse claim payload",
    )


def select_latest_parseable_orchestration_claim(
    comments: list[dict],
    source_label: str,
) -> tuple[dict | None, list[str]]:
    return _build_latest_parseable_comment(
        comments=comments,
        source_label=source_label,
        comment_kind="orchestration claim",
        parse_comment=parse_orchestration_claim_comment_body,
        status_from_payload=lambda payload: str(payload.get("status") or "").strip().lower(),
    )


def build_orchestration_claim(
    issue_number: int,
    run_id: str,
    status: str,
    ttl_seconds: int,
) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + max(ttl_seconds, 1)
    return {
        "status": status,
        "issue": issue_number,
        "run_id": run_id,
        "worker": f"pid-{os.getpid()}",
        "claimed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def is_active_orchestration_claim(claim: dict | None, run_id: str | None = None) -> bool:
    if not isinstance(claim, dict):
        return False
    payload = claim.get("payload") if isinstance(claim.get("payload"), dict) else claim
    status = str(payload.get("status") or "").strip().lower()
    if status != "claimed":
        return False
    if run_id is not None and str(payload.get("run_id") or "") == run_id:
        return False
    expires_at = _parse_iso_timestamp(payload.get("expires_at"))
    if expires_at is None:
        return False
    return expires_at > datetime.now(timezone.utc)


def next_orchestration_attempt(recovered_state: dict | None) -> int:
    if not isinstance(recovered_state, dict):
        return 1
    payload = recovered_state.get("payload") if isinstance(recovered_state.get("payload"), dict) else {}
    previous_attempt = payload.get("attempt")
    if type(previous_attempt) is int and previous_attempt > 0:
        return previous_attempt + 1
    return 1


def parse_decomposition_plan_comment_body(body: str) -> tuple[dict | None, str | None]:
    return _parse_marked_json_payload(
        body,
        DECOMPOSITION_PLAN_MARKER,
        "unable to parse decomposition payload",
    )


def select_latest_parseable_decomposition_plan(
    comments: list[dict],
    source_label: str,
) -> tuple[dict | None, list[str]]:
    return _build_latest_parseable_comment(
        comments=comments,
        source_label=source_label,
        comment_kind="decomposition",
        parse_comment=parse_decomposition_plan_comment_body,
        status_from_payload=lambda payload: str(payload.get("status") or "").strip().lower(),
    )
