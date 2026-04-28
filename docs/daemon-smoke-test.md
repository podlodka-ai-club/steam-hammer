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

Recommended scope:

- Use 2-3 issues only.
- Use a fresh repo clone for the batch, or an explicit `issue -> clone_path` mapping prepared outside the CLI.
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
| Expected repo root / clone | Each worker's `clone_path` matches the fresh clone/root that was assigned before launch. For `run batch --detach`, that usually means the same fresh repo root for all child workers. | A worker reports an unexpected `clone_path`, a reused dirty root, or a clone that belongs to another issue's planned slot. |
| PR branch ownership | The linked PR shown by `status --worker issue-N` belongs to the same issue branch and does not point at another worker's issue context. | The linked PR head branch belongs to a different issue, or one PR/branch appears to be shared across unrelated issues. |
| No cross-contamination | Batch summaries, linked PRs, conflicts, and latest states stay one-to-one with their issue IDs. | Any issue summary includes another issue's branch, PR, clone, or readiness state as if it were its own. |
| Verification before merge | Before merge, the linked PR shows clean readiness and successful verification evidence, such as `merge-result verification=passed` and any required post-batch `verify` result. | Merge happens without clean verification, or verification evidence belongs to the wrong issue/branch. |

Notes:

- If the smoke is intended to exercise detached concurrency only, stop after the ownership checks and do not merge anything.
- If the smoke is intended to validate real merge progression, keep the batch bounded at 2-3 issues and require the verification row above to pass for every PR independently.
