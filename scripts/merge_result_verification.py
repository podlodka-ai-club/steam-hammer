"""Helpers for merge-result verification policy and execution.

This module isolates the decision and temp-clone execution flow from the main
runner so the behavior can be tested at a narrower boundary.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from scripts.project_config import configured_workflow_commands


POST_BATCH_VERIFICATION_DEFAULT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("python-tests", "python3 -m unittest discover -s tests -q"),
    ("go-test", "go test ./..."),
)
CENTRAL_RUNNER_PATH_PREFIXES: tuple[str, ...] = (
    "scripts/",
    "cmd/orchestrator/",
    "internal/cli/",
    "internal/core/",
)
DOC_ONLY_PATH_PREFIXES: tuple[str, ...] = (
    "docs/",
    "retro/",
)
DOC_ONLY_FILE_EXTENSIONS = {".md", ".rst", ".txt"}


def pull_request_changed_paths(pull_request: dict | None) -> list[str]:
    files = pull_request.get("files") if isinstance(pull_request, dict) else None
    if not isinstance(files, list):
        return []

    changed_paths: list[str] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        changed_paths.append(path)
    return changed_paths


def is_docs_only_path(path: str) -> bool:
    normalized = str(path or "").strip().lower()
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in DOC_ONLY_PATH_PREFIXES):
        return True
    _, extension = os.path.splitext(normalized)
    return extension in DOC_ONLY_FILE_EXTENSIONS


def touches_central_runner_files(changed_paths: list[str]) -> bool:
    return any(
        path.startswith(prefix)
        for path in changed_paths
        for prefix in CENTRAL_RUNNER_PATH_PREFIXES
    )


def determine_merge_result_verification_need(
    *,
    repo: str,
    pull_request: dict,
    list_open_pull_requests,
    fetch_pull_request,
) -> dict[str, object]:
    pr_number = pull_request.get("number")
    base_branch = str(pull_request.get("baseRefName") or "").strip()
    changed_paths = pull_request_changed_paths(pull_request)
    if not changed_paths:
        return {
            "required": False,
            "reason": "no-changed-files",
            "summary": "skipped (no changed files reported)",
            "changed_files": [],
            "overlapping_prs": [],
        }

    if all(is_docs_only_path(path) for path in changed_paths):
        return {
            "required": False,
            "reason": "docs-only",
            "summary": "skipped (docs-only PR)",
            "changed_files": changed_paths,
            "overlapping_prs": [],
        }

    if touches_central_runner_files(changed_paths):
        return {
            "required": True,
            "reason": "central-runner-files",
            "summary": "required (touches central runner files)",
            "changed_files": changed_paths,
            "overlapping_prs": [],
        }

    current_paths = set(changed_paths)
    overlapping_prs: list[dict[str, object]] = []
    for candidate in list_open_pull_requests(repo=repo):
        if candidate.get("number") == pr_number:
            continue
        if base_branch and str(candidate.get("baseRefName") or "").strip() != base_branch:
            continue

        candidate_number = candidate.get("number")
        if type(candidate_number) is not int:
            continue
        candidate_details = fetch_pull_request(repo=repo, number=candidate_number)
        candidate_paths = set(pull_request_changed_paths(candidate_details))
        overlap = sorted(current_paths & candidate_paths)
        if not overlap:
            continue
        overlapping_prs.append(
            {
                "number": candidate_number,
                "head_ref": str(candidate.get("headRefName") or "").strip(),
                "files": overlap,
            }
        )

    if overlapping_prs:
        overlapping_numbers = ", ".join(f"#{entry['number']}" for entry in overlapping_prs)
        return {
            "required": True,
            "reason": "overlapping-open-prs",
            "summary": f"required (overlaps with open PRs: {overlapping_numbers})",
            "changed_files": changed_paths,
            "overlapping_prs": overlapping_prs,
        }

    return {
        "required": False,
        "reason": "non-overlapping",
        "summary": "skipped (no overlap and no central runner files)",
        "changed_files": changed_paths,
        "overlapping_prs": [],
    }


def summarize_merge_result_verification_results(results: list[dict[str, object]]) -> str:
    command_count = len(results)
    passed_count = sum(1 for result in results if str(result.get("status") or "") == "passed")
    failed = [result for result in results if str(result.get("status") or "") == "failed"]
    if failed:
        failed_names = ", ".join(str(result.get("name") or "command") for result in failed)
        return f"failed ({passed_count}/{command_count} passed; failed: {failed_names})"
    return f"passed ({passed_count}/{command_count} commands)"


def merge_result_verification_commands(
    *,
    project_config: dict,
    cwd: str | None,
    detect_post_batch_verification_commands,
) -> list[tuple[str, str]]:
    commands = configured_workflow_commands(project_config)
    if commands:
        return commands
    return detect_post_batch_verification_commands(cwd=cwd)


def verify_pull_request_merge_result(
    *,
    repo: str,
    pull_request: dict,
    project_config: dict,
    repo_dir: str,
    dry_run: bool,
    determine_need,
    resolve_commands,
    run_command,
    run_check_command,
    workflow_output_excerpt,
    short_error_text,
) -> dict[str, object]:
    decision = determine_need(repo=repo, pull_request=pull_request)
    verification: dict[str, object] = {
        "status": "skipped",
        "summary": str(decision.get("summary") or "skipped"),
        "required": bool(decision.get("required")),
        "reason": str(decision.get("reason") or "unknown"),
        "changed_files": list(decision.get("changed_files") or []),
        "overlapping_prs": list(decision.get("overlapping_prs") or []),
        "checkout": "temp-clone",
        "commands": [],
    }
    if not bool(decision.get("required")):
        return verification

    commands = resolve_commands(project_config=project_config, cwd=repo_dir)
    if not commands:
        verification.update(
            {
                "status": "failed",
                "summary": "failed (no merge-result verification commands detected)",
                "error": "Merge-result verification is required, but no verification commands are configured or detectable.",
            }
        )
        return verification

    verification["commands"] = [
        {"name": check_name, "command": command_text, "status": "pending"}
        for check_name, command_text in commands
    ]
    if dry_run:
        verification.update(
            {
                "status": "dry-run",
                "summary": f"dry-run ({len(commands)} commands in temp clone)",
            }
        )
        return verification

    pr_number = pull_request.get("number")
    base_branch = str(pull_request.get("baseRefName") or "").strip()
    head_branch = str(pull_request.get("headRefName") or "").strip()
    if not base_branch or not head_branch:
        verification.update(
            {
                "status": "failed",
                "summary": "failed (missing branch metadata)",
                "error": "Pull request is missing base/head branch metadata required for merge-result verification.",
            }
        )
        return verification

    clone_dir = tempfile.mkdtemp(prefix=f"merge-verify-pr-{pr_number or 'unknown'}-")
    try:
        run_command(["git", "clone", "--quiet", repo_dir, clone_dir])
        run_command(["git", "-C", clone_dir, "fetch", "origin", base_branch, head_branch])
        run_command(["git", "-C", clone_dir, "checkout", "--detach", f"origin/{base_branch}"])
        ok_merge, _stdout_merge, stderr_merge, merge_exit_code = run_check_command(
            ["git", "merge", "--no-ff", "--no-commit", f"origin/{head_branch}"],
            cwd=clone_dir,
        )
        if not ok_merge:
            verification.update(
                {
                    "status": "failed",
                    "summary": "failed (could not construct merge result)",
                    "error": short_error_text(stderr_merge or "git merge failed"),
                    "merge_exit_code": merge_exit_code,
                }
            )
            return verification

        results: list[dict[str, object]] = []
        for check_name, command_text in commands:
            ok, stdout_text, stderr_text, exit_code = run_check_command(
                ["bash", "-lc", command_text],
                cwd=clone_dir,
            )
            result: dict[str, object] = {
                "name": check_name,
                "command": command_text,
                "status": "passed" if ok else "failed",
                "exit_code": exit_code,
            }
            if stdout_text:
                result["stdout_excerpt"] = workflow_output_excerpt(stdout_text)
            if stderr_text:
                result["stderr_excerpt"] = workflow_output_excerpt(stderr_text)
            results.append(result)
            if not ok:
                break

        verification["commands"] = results
        failed = [result for result in results if str(result.get("status") or "") == "failed"]
        if failed:
            first_failed = failed[0]
            verification.update(
                {
                    "status": "failed",
                    "summary": summarize_merge_result_verification_results(results),
                    "error": short_error_text(
                        str(first_failed.get("stderr_excerpt") or "").strip()
                        or str(first_failed.get("stdout_excerpt") or "").strip()
                        or f"Merge-result verification failed: {str(first_failed.get('name') or 'command')}"
                    ),
                }
            )
            return verification

        verification.update(
            {
                "status": "passed",
                "summary": summarize_merge_result_verification_results(results),
                "error": None,
            }
        )
        return verification
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
