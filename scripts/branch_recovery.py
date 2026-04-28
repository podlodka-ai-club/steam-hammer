from __future__ import annotations

from collections.abc import Callable


def local_branch_exists(
    branch_name: str,
    *,
    command_succeeds: Callable[[list[str]], bool],
) -> bool:
    return command_succeeds(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"])


def remote_branch_exists(
    branch_name: str,
    *,
    command_succeeds: Callable[[list[str]], bool],
) -> bool:
    return command_succeeds(
        ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch_name}"]
    )


def list_conflicted_paths(*, run_capture: Callable[[list[str]], str]) -> list[str]:
    output = run_capture(["git", "diff", "--name-only", "--diff-filter=U"])
    return [line.strip() for line in output.splitlines() if line.strip()]


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
    return {
        "branch_name": branch_name,
        "remote_base_ref": remote_base_ref,
        "requested_strategy": requested_strategy,
        "applied_strategy": applied_strategy,
        "status": status,
        "changed": changed,
        "auto_resolved": auto_resolved,
    }


def print_branch_sync_result(result: dict[str, object], *, dry_run: bool = False) -> None:
    branch_name = str(result.get("branch_name") or "")
    remote_base_ref = str(result.get("remote_base_ref") or "")
    applied_strategy = str(result.get("applied_strategy") or "")
    status = str(result.get("status") or "")
    prefix = "[dry-run] " if dry_run else ""

    if status == "already-current":
        print(
            f"{prefix}Conflict recovery result for branch '{branch_name}': already current with '{remote_base_ref}'"
        )
        return

    if status == "auto-resolved":
        print(
            f"{prefix}Conflict recovery result for branch '{branch_name}': auto-resolved conflicts against "
            f"'{remote_base_ref}' via {applied_strategy}"
        )
        return

    if status == "synced-cleanly":
        print(
            f"{prefix}Conflict recovery result for branch '{branch_name}': synced cleanly with "
            f"'{remote_base_ref}' via {applied_strategy}"
        )
        return

    print(
        f"{prefix}Conflict recovery result for branch '{branch_name}': status={status or 'unknown'} "
        f"against '{remote_base_ref}'"
    )


def push_recovered_branch(
    branch_name: str,
    result: dict[str, object],
    dry_run: bool,
    *,
    push_branch: Callable[..., None],
) -> None:
    changed = bool(result.get("changed"))
    if not changed:
        return

    applied_strategy = str(result.get("applied_strategy") or "")
    force_with_lease = applied_strategy == "rebase"
    push_branch(
        branch_name=branch_name,
        dry_run=dry_run,
        force_with_lease=force_with_lease,
    )
    prefix = "[dry-run] " if dry_run else ""
    print(
        f"{prefix}Conflict recovery push result for branch '{branch_name}': pushed "
        f"(force-with-lease: {'yes' if force_with_lease else 'no'})"
    )


def auto_resolve_merge_conflicts_with_base(
    *,
    list_conflicted_paths: Callable[[], list[str]],
    run_command: Callable[[list[str]], object],
) -> int:
    conflicted_paths = list_conflicted_paths()
    if not conflicted_paths:
        raise RuntimeError("Merge failed, but no conflicted files were detected")

    for path in conflicted_paths:
        run_command(["git", "checkout", "--theirs", "--", path])

    run_command(["git", "add", "-A"])
    run_command(["git", "commit", "--no-edit"])
    return len(conflicted_paths)


def merge_sync_with_auto_resolution(
    remote_base_ref: str,
    branch_name: str,
    requested_strategy: str,
    *,
    run_command: Callable[[list[str]], object],
    command_succeeds: Callable[[list[str]], bool],
    current_head_sha: Callable[[], str],
    auto_resolve_merge_conflicts_with_base: Callable[[], int],
    build_branch_sync_result: Callable[..., dict[str, object]],
) -> dict[str, object]:
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
    return build_branch_sync_result(
        branch_name=branch_name,
        remote_base_ref=remote_base_ref,
        requested_strategy=requested_strategy,
        applied_strategy="merge",
        status="auto-resolved" if synced else "already-current",
        changed=synced,
        auto_resolved=synced,
    )


def prepare_issue_branch(
    base_branch: str,
    branch_name: str,
    dry_run: bool,
    fail_on_existing: bool,
    *,
    local_branch_exists: Callable[[str], bool],
    remote_branch_exists: Callable[[str], bool],
    run_command: Callable[[list[str]], object],
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
    *,
    run_command: Callable[[list[str]], object],
    command_succeeds: Callable[[list[str]], bool],
    current_head_sha: Callable[[], str],
    merge_sync_with_auto_resolution: Callable[[str, str, str], dict[str, object]],
    build_branch_sync_result: Callable[..., dict[str, object]],
) -> dict[str, object]:
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
        return build_branch_sync_result(
            branch_name=branch_name,
            remote_base_ref=remote_base_ref,
            requested_strategy=strategy,
            applied_strategy=strategy,
            status="dry-run",
            changed=False,
            auto_resolved=False,
        )

    print(
        f"Sync attempt: reused branch '{branch_name}' with '{remote_base_ref}' "
        f"using '{strategy}' strategy"
    )

    run_command(["git", "fetch", "origin", base_branch])

    if strategy == "merge":
        return merge_sync_with_auto_resolution(
            remote_base_ref,
            branch_name,
            strategy,
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
            remote_base_ref,
            branch_name,
            strategy,
        )

    after_sync_sha = current_head_sha()
    synced = before_sync_sha != after_sync_sha
    if synced:
        print(f"Reused branch '{branch_name}' updated after rebase sync")
    else:
        print(f"Reused branch '{branch_name}' already up to date with '{remote_base_ref}'")
    return build_branch_sync_result(
        branch_name=branch_name,
        remote_base_ref=remote_base_ref,
        requested_strategy=strategy,
        applied_strategy="rebase",
        status="synced-cleanly" if synced else "already-current",
        changed=synced,
        auto_resolved=False,
    )


def run_conflict_recovery_for_branch(
    *,
    branch_name: str,
    base_branch: str,
    strategy: str,
    dry_run: bool,
    sync_reused_branch_with_base: Callable[..., dict[str, object]],
    print_branch_sync_result: Callable[..., None],
    verify_recovered_branch: Callable[[dict[str, object]], None] | None = None,
    push_recovered_branch: Callable[..., None],
) -> dict[str, object]:
    result = sync_reused_branch_with_base(
        base_branch=base_branch,
        branch_name=branch_name,
        strategy=strategy,
        dry_run=dry_run,
    )
    print_branch_sync_result(result, dry_run=dry_run)
    if verify_recovered_branch is not None:
        verify_recovered_branch(result)
    push_recovered_branch(branch_name=branch_name, result=result, dry_run=dry_run)
    return result
