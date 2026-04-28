# Retro: detached worker monitoring loop

Date: 2026-04-28

## Scope

We tried a broader orchestration style for three parallel strategic tasks:

- #168: first-class detached worker orchestration mode.
- #169: Go core migration with a status/dependency parsing slice.
- #170: branch recovery and sync helper modularization.

The orchestrator started one detached worker per issue in separate fresh clones, wrote logs to `/tmp/steam-hammer-<issue>.log`, monitored process/log status periodically, and handled PR verification/merge sequencing afterward.

## What worked well

- The detached-worker pattern let three substantial tasks progress concurrently without blocking the orchestration session.
- One log per issue made it easy to track process health without reading full logs constantly.
- The rule “poll status every minute, read logs only after no progress or completion” reduced noise and kept attention on tracker state.
- PRs were merged safely by verifying merge-result behavior before sequential squash merges.
- The orchestrator recovered cleanly from local checkout mistakes without destructive git operations.

## What was intentionally different

- The orchestrator did not tail logs continuously.
- Routine checks used process liveness, log line counts, and GitHub PR state.
- Detailed log reads were reserved for completion, failure, or stalled output.
- Detached workers were allowed to create overlapping PRs; the orchestrator handled merge order and verification.

## Issues completed

- #169 merged as PR #172.
- #170 merged as PR #171.
- #168 merged as PR #173.

Final state after the batch:

- open PRs: none;
- open issues from the batch: none;
- local `main`: synchronized with `origin/main`.

## Pain points

- Running verification from PR branches can leave the local checkout away from `main`; the orchestrator must explicitly return to `main` before pulling.
- Parallel central-runner work still creates conflict risk, so merge-result verification remains important.
- Some test output is still noisy even when tests pass.

## Process rule to keep

For broad batches:

1. Create or confirm tracker issues.
2. Start detached workers in fresh clones with one log file per issue.
3. Check status once per minute using process liveness, log line counts, and PR lists.
4. Read logs only when a worker finishes, fails, or stops producing output for about three minutes.
5. Verify PRs on the effective merge result before merging central-runner changes.
6. Merge sequentially and recover stale PRs with focused conflict recovery before full reruns.
