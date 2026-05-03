# Daemon Smoke Test

This note records the bounded daemon smoke path that is safe to run from a clean workspace without creating live issue/branch/PR side effects.

## Why The Suggested Live Command Was Not Used

The issue's suggested command is a real autonomous run:

```bash
go run ./cmd/orchestrator run daemon \
  --repo podlodka-ai-club/steam-hammer \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.4 \
  --opencode-auto-approve \
  --poll-interval-seconds 120 \
  --limit 1 \
  --force-reprocess \
  --agent-timeout-seconds 3600 \
  --agent-idle-timeout-seconds 900
```

Running that command from a shared repository is not a smoke test anymore: it can claim a live issue, create a branch, push it, and open a PR.

## Safe Bounded Alternative

1. Verify the repo is ready and the default branch resolves to `main`:

```bash
go run ./cmd/orchestrator doctor \
  --repo podlodka-ai-club/steam-hammer \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.4
```

Observed result on 2026-04-28:

- `Clean worktree: working tree is clean`
- `Default branch: default branch is 'main'`
- `Selected runner (opencode): found at ~/.opencode/bin/opencode`
- `Doctor summary: 9 pass, 5 warn, 0 fail`
- Non-blocking warnings were limited to optional config files not being present and the runner smoke check being skipped.

2. Run a single daemon poll in `--dry-run` mode:

```bash
go run ./cmd/orchestrator run daemon \
  --repo podlodka-ai-club/steam-hammer \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.4 \
  --opencode-auto-approve \
  --poll-interval-seconds 120 \
  --limit 1 \
  --force-reprocess \
  --agent-timeout-seconds 3600 \
  --agent-idle-timeout-seconds 900 \
  --dry-run
```

`run daemon` already bounds `--dry-run` to one cycle, so no extra `--max-cycles 1` is required.

## Observed Behavior

Observed output on 2026-04-28:

- Selected base branch: `main`
- Base mode: `default`
- The daemon inspected one eligible issue: `#147`
- It recovered prior orchestration state for that issue
- It would claim the issue, post `in-progress`, create branch `issue-fix/147-reduce-python-test-noise-and-define-fast`, run `opencode`, commit, push, create a PR, and release the claim
- Final summary: `Done. Processed: 1, skipped_existing_pr: 0, skipped_existing_branch: 0, skipped_out_of_scope: 0, failures: 0`

This validates the current operator path for the daemon entrypoint:

1. Resolve repository and default branch.
2. Poll issues.
3. Scope-check the selected issue.
4. Recover orchestration state when present.
5. Claim work.
6. Transition to `agent_run`.
7. Create or reuse branch state.
8. Run the selected agent.
9. Commit, push, and create a PR.
10. Transition to `ready-for-review` and release the claim.

## Logs Location

There is no dedicated daemon logfile today.

- `doctor` and `run daemon` stream their output to the current process `stdout`/`stderr`
- Agent output is also streamed directly to the terminal
- If you need a retained artifact, capture shell output explicitly, for example with `tee`

## Gaps And Next Actions

- This smoke was run from a clean workspace, but not as a live autonomous run on a fresh checked-out `main`, because the live command would create real work items and repository side effects.
- The safe reproducible smoke path today is `doctor` plus single-cycle `run daemon --dry-run`.
- The CLI does not emit a dedicated logfile path or machine-readable smoke summary, so evidence collection still depends on terminal capture.

## Detached Batch Branch-Isolation Smoke

After #204, a small concurrent detached batch is only considered safe when operators explicitly verify ownership boundaries before any merge.

Recorded outcome on 2026-04-28:

- Status: green for the local detached/concurrent ownership smoke.
- Evidence command 1: `go test ./internal/cli ./internal/core/workers`
- Evidence command 2: `python3 -m unittest tests.test_staging_behavior tests.test_existing_branch_pr_reuse tests.test_post_batch_verification -q`
- Result: both commands passed.
- Blocker follow-up: not needed from this smoke run.

What this smoke confirms:

- `internal/cli` detached batch tests cover one worker per issue, fresh per-worker clone paths, and persisted batch metadata with per-child status commands.
- `internal/core/workers` tests cover predictable worker registry paths and persisted clone/log/state metadata.
- Python ownership tests fail commit/push before side effects when the current branch or repo root does not match the expected issue worker context.

What this smoke does not claim:

- This is not a fresh live GitHub batch that opens real PRs for 2-3 issues.
- Operator ownership checks from the checklist below still remain the merge gate for any real detached batch.

Recommended scope:

- Use 2-3 issues only.
- Use a clean source repo checkout for the batch launch. `run batch --detach` now prepares a fresh per-worker clone automatically under each worker directory.
- Do not expand to a broader autonomous batch until every check below passes.

Suggested operator flow:

1. Run the bounded preflight:

```bash
go run ./cmd/orchestrator doctor --repo owner/repo
```

2. Launch a small detached batch from the fresh clone:

```bash
go run ./cmd/orchestrator run batch --ids 71,72 --repo owner/repo --detach
```

3. Inspect the local worker registry:

```bash
go run ./cmd/orchestrator status --workers
go run ./cmd/orchestrator status --worker issue-71
go run ./cmd/orchestrator status --worker issue-72
```

Pass/fail criteria:

| Check | Pass | Fail |
| --- | --- | --- |
| Worker registry scope | `status --workers` shows exactly the expected 2-3 workers for this batch and no unrelated live workers that could be mistaken for the same issues. | Missing worker, unexpected extra worker, or registry entries that make issue ownership ambiguous. |
| Expected branch per issue | Each worker resolves to its own issue and later to its own deterministic issue branch. | Any worker reuses or reports another issue's branch. |
| Expected repo root / clone | Each worker's `clone_path` matches its own fresh managed clone, typically `<worker-dir>/issue-N/repo`, and no two workers share the same path. | A worker reports an unexpected `clone_path`, a reused dirty root, or a clone path shared with another issue. |
| PR branch ownership | The linked PR shown by `status --worker issue-N` belongs to the same issue branch and does not point at another worker's issue context. | The linked PR head branch belongs to a different issue, or one PR/branch appears to be shared across unrelated issues. |
| No cross-contamination | Batch summaries, linked PRs, conflicts, and latest states stay one-to-one with their issue IDs. | Any issue summary includes another issue's branch, PR, clone, or readiness state as if it were its own. |
| Verification before merge | Before merge, the linked PR shows clean readiness and successful verification evidence, such as `merge-result verification=passed` and any required post-batch `verify` result. | Merge happens without clean verification, or verification evidence belongs to the wrong issue/branch. |

Notes:

- If the smoke is intended to exercise detached concurrency only, stop after the ownership checks and do not merge anything.
- If the smoke is intended to validate real merge progression, keep the batch bounded at 2-3 issues and require the verification row above to pass for every PR independently.

## Detached Autonomous Go Smoke Commands

Use these commands for the Go wrapper autonomous detached smoke. The default path below is bounded and safe: `--dry-run` avoids live issue claims, branch creation, pushes, PRs, and merges, while `--detach` still exercises worker metadata, log, session, status, and clone preparation surfaces.

1. Preflight the environment and runner lookup:

```bash
go run ./cmd/orchestrator doctor \
  --repo podlodka-ai-club/steam-hammer \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.4 \
  --doctor-smoke-check
```

Expected output includes:

```text
Doctor diagnostics
[PASS] Git repository: inside a git work tree
[PASS] Clean worktree: working tree is clean
[PASS] GitHub CLI: found at ...
[PASS] gh auth: authenticated
[PASS] Repository access: verified access to podlodka-ai-club/steam-hammer
[PASS] Runner smoke check: CLI invocation enabled
Doctor summary: N pass, 0 warn, 0 fail
```

2. Launch a bounded detached autonomous daemon smoke:

```bash
go run ./cmd/orchestrator run daemon \
  --repo podlodka-ai-club/steam-hammer \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.4 \
  --limit 2 \
  --poll-interval-seconds 1 \
  --max-parallel-tasks 2 \
  --max-cycles 1 \
  --dry-run \
  --detach \
  --post-batch-verify
```

Expected output includes one block per detached daemon worker:

```text
started detached worker daemon-1
pid: <pid>
log: <repo>/.orchestrator/workers/daemon-1/worker.log
state: <repo>/.orchestrator/workers/daemon-1/worker.json
session: <repo>/.orchestrator/workers/daemon-1/session.json
next: orchestrator status --worker daemon-1
started detached worker daemon-2
pid: <pid>
log: <repo>/.orchestrator/workers/daemon-2/worker.log
state: <repo>/.orchestrator/workers/daemon-2/worker.json
session: <repo>/.orchestrator/workers/daemon-2/session.json
next: orchestrator status --worker daemon-2
```

3. Inspect the detached registry and each worker:

```bash
go run ./cmd/orchestrator status --workers
go run ./cmd/orchestrator status --worker daemon-1
go run ./cmd/orchestrator status --worker daemon-2
go run ./cmd/orchestrator status --autonomous-session-file .orchestrator/workers/daemon-1/session.json
go run ./cmd/orchestrator status --autonomous-session-file .orchestrator/workers/daemon-2/session.json
```

Expected `status --workers` output includes both daemon workers and points to their next status commands:

```text
daemon-1	<process-status>	lines=<n>	log=<freshness>	next: orchestrator status --worker daemon-1
daemon-2	<process-status>	lines=<n>	log=<freshness>	next: orchestrator status --worker daemon-2
```

Expected `status --worker daemon-N` output includes:

```text
worker: daemon-N
target: daemon
repo: podlodka-ai-club/steam-hammer
process: <running|exited|unknown>
clone: <repo>/.orchestrator/workers/daemon-N/repo
agent: runner=opencode agent=build model=openai/gpt-5.4
log-progress: lines=<n>
log: <repo>/.orchestrator/workers/daemon-N/worker.log
state: <repo>/.orchestrator/workers/daemon-N/worker.json
session: <repo>/.orchestrator/workers/daemon-N/session.json
next: <status command or completion hint>
```

Expected session status output includes the autonomous checkpoint summary, for example:

```text
Autonomous session status
Phase: ...
Current: ...
Active workers: ...
Next checkpoint: ...
```

Safe defaults and live side effects:

- `--dry-run` is the safe default for smoke evidence. It bounds daemon polling to one cycle when no explicit cycle count is provided, and the command above also sets `--max-cycles 1` for clarity.
- `--detach` by itself is not a dry run. Without `--dry-run`, detached daemon workers may claim live issues, create branches, run agents, commit, push, open PRs, post state comments, and run post-batch verification.
- `--opencode-auto-approve` is intentionally omitted from the safe smoke command. Add it only for an intentional live run where skipping OpenCode interactive approvals is acceptable.
- `--post-batch-verify` is safe in the dry-run smoke because it only exercises the verification path after the bounded worker cycle. In a live run it can publish follow-up issues only if `--create-followup-issue` is also passed.

To intentionally opt in to live detached autonomous side effects, remove `--dry-run` only after the preflight and status checks pass, keep `--limit`, `--max-parallel-tasks`, and `--max-cycles` small, and verify worker ownership before any merge progression:

```bash
go run ./cmd/orchestrator run daemon \
  --repo podlodka-ai-club/steam-hammer \
  --runner opencode \
  --agent build \
  --model openai/gpt-5.4 \
  --limit 2 \
  --poll-interval-seconds 120 \
  --max-parallel-tasks 2 \
  --max-cycles 1 \
  --detach \
  --post-batch-verify
```

Common troubleshooting:

| Symptom | Likely Cause | Safe Response |
| --- | --- | --- |
| `orchestrator: detached worker state not found: .../worker.json` | The worker name or `--worker-dir` does not match the launch command, or the worker never started. | Re-run `status --workers` with the same `--worker-dir` used at launch, then inspect the exact worker name printed by the launch command. |
| `orchestrator: failed to inspect detached worker state: ...` | `worker.json` is malformed, unreadable, or points at missing paths. | Do not restart over the same worker. Preserve the file, inspect the matching `worker.log`, and use a new `--worker-dir` for another smoke attempt. |
| `detached worker <name> is already running with pid <pid>` | A worker with the same name still has a live process according to the registry. | Use `status --worker <name>` and wait for it to exit, or use a separate `--worker-dir` for a new smoke. |
| `clone:` is missing or shared between daemon workers | Detached clone preparation failed or worker metadata is stale. | Treat ownership as ambiguous. Do not continue to live side effects or merge; relaunch from a clean source checkout or a fresh `--worker-dir`. |
| `linked-branch`, `linked-pr`, or session summaries mention another issue/worker | Branch, PR, or state-comment ownership is contaminated. | Stop the smoke. Do not merge. Investigate the referenced issue/PR and start a new bounded run only after ownership is clear. |
| `process: unknown` with stale `log-freshness` and no new `log-progress` | The worker process is gone or cannot be inspected, and the log is no longer advancing. | Read the worker log and session status. Treat missing completion evidence as failed smoke evidence. |
