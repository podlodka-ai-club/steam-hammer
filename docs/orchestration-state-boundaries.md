# Reusable Orchestration State Boundaries

This document defines the state boundaries that already exist in the repository and should stay stable during the Python-to-Go migration. The goal is to make the current surfaces reusable without changing operator-visible behavior, machine-readable markers, or recovery semantics.

## Why These Boundaries Exist

The current implementation persists orchestration progress across several surfaces with different lifetimes:

- issue/PR comments are the externally visible audit log and recovery source;
- `worker.json` is local detached-process metadata for operator UX;
- verification payloads summarize whether a PR or a batch is safe to continue;
- `session.json` tracks autonomous batch progress between daemon iterations.

The migration rule is simple: move producers and consumers behind these boundaries first, then replace implementation details under them.

## Boundary Matrix

| Boundary | Current carrier | Current producer | Current consumer | Primary concern |
| --- | --- | --- | --- | --- |
| Issue/PR orchestration state | issue/PR comment with `<!-- orchestration-state:v1 -->` | Python runner | Python recovery/status, Go status via Python bridge | Shared schema + Go core domain |
| Detached worker state | `.orchestrator/workers/<name>/worker.json` | Go CLI | Go CLI | Go core |
| Verification result | nested JSON payloads in state/session data | Python runner | Python progression/status, Go session status summary | Shared schema + Go core domain |
| Batch/session state | `.orchestrator/workers/daemon/session.json` or custom `--autonomous-session-file` | Python runner | Python resume/status, Go detached status | Shared schema + Go core domain |

## 1. Issue/PR State Boundary

This is the canonical orchestration state machine surface for tracked work.

Carrier:
- issue or PR comment body
- marker: `<!-- orchestration-state:v1 -->`
- JSON payload formatted by `build_orchestration_state()` and `format_orchestration_state_comment()` in `scripts/run_github_issues_to_opencode.py`

Stable top-level fields today:
- `status`
- `task_type`
- `issue`
- `pr`
- `branch`
- `base_branch`
- `runner`
- `agent`
- `model`
- `attempt`
- `stage`
- `next_action`
- `error`
- `timestamp`

Optional nested fields already in use:
- `workflow_checks`
- `ci_checks`
- `ci_diagnostics`
- `residual_untracked_files`
- `residual_untracked_count`
- `stats`
- `decomposition`
- `required_file_validation`
- `merge_readiness`
- `merge_policy`

Stable status vocabulary today:
- `in-progress`
- `ready-for-review`
- `failed`
- `blocked`
- `waiting-for-author`
- `waiting-for-ci`
- `ready-to-merge`

Ownership split:
- Shared schema concern: comment marker, top-level keys, status vocabulary, and the meaning of `stage` plus `next_action`.
- Python module concern: current formatting, parsing, and recovery helpers in `scripts/orchestration_state.py` and the runner.
- Go core concern: future canonical state-machine model and state-store implementation that must emit the same externally visible payload semantics.

Migration guidance:
- Keep the marker and top-level keys unchanged while Go becomes a producer.
- Treat unknown optional fields as additive and forward-compatible.
- Preserve the rule that malformed comments are ignored and the latest parseable payload wins.
- Keep tracker comments as the source of truth for issue/PR progression even if an internal store is added later.

## 2. Detached Worker State Boundary

This is local runtime metadata for detached processes. It is not the source of truth for issue/PR progression.

Carrier:
- `.orchestrator/workers/<issue-N|pr-N|daemon>/worker.json`
- written and read by `internal/cli/detached.go`

Stable fields today:
- `name`
- `mode`
- `target_kind`
- `target_id`
- `repo`
- `tracker`
- `codehost`
- `runner`
- `agent`
- `model`
- `command`
- `started_at`
- `pid`
- `log_path`
- `session_path`
- `state_path`
- `clone_path`
- `work_dir`

Boundary rules:
- `worker.json` exists to make detached workers inspectable without reconstructing paths or process metadata.
- It should reference issue/PR state indirectly through `target_kind`, `target_id`, repo settings, and optional `session_path`; it should not duplicate the full orchestration state payload.
- It is acceptable for Go to enrich this file for local UX as long as new fields are additive.

Ownership split:
- Go core concern: detached worker lifecycle, local metadata shape, and status/report rendering.
- Shared schema concern: only the persisted JSON keys that operator tooling may rely on.
- Python module concern: none beyond being invoked by Go when the linked issue/PR status is queried.

Migration guidance:
- Keep `worker.json` Go-owned; do not move this boundary into the Python runner.
- If the detached execution engine changes, preserve the file location and current keys or provide additive aliases during a transition.
- Keep linked issue/PR state derived from tracker comments instead of copying it into `worker.json`.

## 3. Verification Result Boundary

Verification results are reusable verdict payloads rather than a separate persistence channel.

Current forms:
- `merge_readiness.merge_result_verification` inside issue/PR orchestration state
- `checkpoint.verification` inside autonomous session state

Stable fields shared by current verification payloads:
- `status`
- `summary`
- `next_action`
- `commands`

Command result item shape used today:
- `name`
- `command`
- `status`
- `exit_code`
- optional `stdout_excerpt`
- optional `stderr_excerpt`

Post-batch verification also carries:
- `follow_up_issue` with status-driven fields such as `status`, `title`, `body`, `issue_number`, `issue_url`

Boundary rules:
- Verification is a decision object that explains whether execution may continue, pause, or create follow-up work.
- `status` and `next_action` are the stable control fields; detailed command output is supporting evidence.
- Merge-result verification and post-batch verification may differ in policy, but should reuse the same base verdict shape.

Ownership split:
- Shared schema concern: verdict fields, command-result item shape, and the meaning of `status` plus `next_action`.
- Python module concern: current execution of merge-result and post-batch commands.
- Go core concern: future reusable verification types consumed by merge/readiness/session logic.

Migration guidance:
- Introduce Go types for verification results before moving command execution.
- Reuse the same verdict shape across PR progression and autonomous batch verification.
- Add new evidence fields only additively; do not rename `status`, `summary`, `next_action`, or `commands`.

## 4. Batch/Session State Boundary

This is the resumable autonomous-run surface.

Carrier:
- `session.json` for detached daemon runs
- any file passed through `--autonomous-session-file`
- written by `load_autonomous_session_state()`, `save_autonomous_session_state()`, and `update_autonomous_session_checkpoint()` in the Python runner
- summarized by Go in `internal/cli/detached.go`

Stable top-level fields today:
- `processed_issues`
- `checkpoint`

`processed_issues` boundary role:
- append/update map keyed by issue number string
- stores per-issue completion/recovery facts for future autonomous passes

`checkpoint` stable fields today:
- `run_id`
- `phase`
- `batch_index`
- `total_batches`
- `counts`
- `done`
- `current`
- `next`
- `issue_pr_actions`
- `in_progress`
- `blockers`
- `next_checkpoint`
- `updated_at`
- optional `verification`

`counts` stable fields today:
- `processed`
- `failures`
- `skipped_existing_pr`
- `skipped_existing_branch`
- `skipped_blocked_dependencies`
- `skipped_out_of_scope`

Boundary rules:
- `session.json` is resumable runtime state, not an externally visible audit log.
- `checkpoint` is the human-readable progress summary; `processed_issues` is the machine-friendly memory of what already happened in the run.
- Go is currently a partial consumer of this schema and should continue tolerating missing or extra fields.

Ownership split:
- Shared schema concern: top-level `processed_issues` and `checkpoint`, plus stable checkpoint keys.
- Python module concern: current checkpoint mutation logic during autonomous batch execution.
- Go core concern: future daemon/session model, detached status reporting, and resume semantics.

Migration guidance:
- Preserve `processed_issues` and `checkpoint` as the top-level shape while the producer moves from Python to Go.
- Keep checkpoint updates monotonic and append-only in spirit: later writes should clarify progress, not reinterpret past status names.
- Do not couple `session.json` to GitHub-specific response objects; keep it as normalized orchestration state.

## Recommended Implementation Split

Go core should own:
- canonical state-machine enums and structs for issue/PR state, verification verdicts, and session checkpoints;
- detached worker metadata and local status UX;
- future parsing/writing of the shared schemas once behavior parity is locked.

Python modules should keep owning during the transition:
- current production of orchestration-state comments;
- current production of autonomous checkpoint updates;
- current execution of merge-result and post-batch verification commands.

Shared schema should remain stable across both stacks:
- comment markers and parse rules;
- JSON key names and status vocabulary used by recovery/status tooling;
- additive-extension rules and malformed-payload tolerance.

## Migration Sequence Without Behavior Changes

1. Define Go structs that match the current persisted payloads exactly.
2. Make Go readers tolerant first: parse current Python payloads without changing output.
3. Move decision logic behind Go domain types while Python remains the writer.
4. Switch writers one boundary at a time, starting with local files (`worker.json`, then `session.json`) before tracker comments.
5. Move issue/PR comment writing last, after status summaries and recovery logic produce byte-for-byte compatible meaning.
6. Only add fields additively; do not rename markers, statuses, or top-level keys in the migration path.

Following this order keeps recovery, detached status inspection, and operator-facing tracker history stable while the core implementation moves underneath.
