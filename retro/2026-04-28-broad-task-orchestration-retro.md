# Retro: broad-task orchestration experiment

Date: 2026-04-28

## Scope

We intentionally tested the orchestrator on broader tasks instead of narrow one-issue/one-PR work.

Parent issues:

- #183: first-class autonomous detached batch mode.
- #184: automated PR recovery pipeline.
- #185: move orchestration state toward reusable core.

The goal was to exercise decomposition, child issue tracking, detached workers, merge-result verification, dependency sequencing, recovery, and broad-session status discipline.

## What shipped

Broad parent #183 completed through child issues:

- #187 → PR #199: added `orchestrator run batch --ids ...` with dry-run/detach support.
- #188 → PR #200: persisted batch metadata and child worker registry links.
- #189 → PR #201: exposed detached batch status summaries.

Broad parent #184 completed through child issues:

- #190 → PR #198: classified PR readiness as clean/stale/conflicting/unknown.
- #191 → PR #202: added forced verification for PR recovery paths.
- #192: functionality landed through PR #203; ownership anomaly tracked in #204.

Broad parent #185 completed through child issues:

- #193 → PR #196: documented reusable orchestration state boundaries.
- #194 → PR #203: extracted Go worker registry state into `internal/core/workers`.
- #195 → PR #205: extracted Python merge-result verification into `scripts/merge_result_verification.py`.

Blocker fixed during the run:

- #186 → PR #197: fixed decomposition child issue creation for local `gh` versions without `gh issue create --json`, and fixed early failure handling around unset runner context.

## What worked well

- Broad issues were correctly identified as needing decomposition and were stopped before implementation.
- Human-refined decomposition plans produced a manageable child issue graph.
- Detached workers worked well for independent first-wave child tasks.
- Merge-result verification caught a real issue in #203 before merge.
- Focused PR-review recovery fixed #203 without manual code edits.
- Sequential dependency handling worked for parent #183 and #184 after the first children landed.

## What did not work well

- Generated decomposition plans were too mechanical:
  - titles were truncated;
  - one plan included a test command as a child task.
- Approved decomposition plans were not picked up cleanly after an older `waiting-for-author` state; `--force-issue-flow` was required.
- Child issue creation assumed `gh issue create --json`, which the installed `gh` did not support.
- Concurrent worker branch context became unsafe:
  - #192 implementation was committed in the #194 branch context;
  - #192 then failed to open its own PR because its branch had no commits beyond main;
  - #204 remains open to track this safety bug.
- The orchestrator occasionally ended on worker branches after verification/merge commands, requiring explicit checkout back to `main` before `git pull --ff-only`.

## Verification pattern used

For central runner, Go CLI, or overlapping changes:

1. Fetch PR into a fresh verification clone.
2. Merge current `origin/main` or PR head onto main-equivalent state.
3. Run:
   - `python3 -m unittest discover -s tests -q`
   - `go test ./...`
4. Merge only after green verification.

## Current final state

- Open PRs: none.
- Open issues from this session: #204 only.
- Parents #183, #184, #185 are closed.
- Working tree on `main` is clean and synced after the session.

## Follow-up priority

Fix #204 before the next broad concurrent batch.

Recommended acceptance for #204:

- Before commit/push/PR creation, assert current branch matches the expected issue/PR branch.
- Fail fast with a factual tracker comment if the branch context is wrong.
- Add regression coverage for branch mismatch before commit/push.
- Prefer this fix before launching multiple concurrent broad-child workers again.
