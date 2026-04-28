# Retro: daemon-mode execution for next North Star tasks

Date: 2026-04-28

## Scope

We tried to execute the next two large North Star tasks through daemon mode instead of direct per-issue runner calls:

- #153: Add operator status summary for orchestration runs.
- #152: Add dedicated conflict-recovery mode for reused branches.

The daemon was launched from a fresh clone with `openai/gpt-5.4`, `--max-cycles`, and bounded `--limit` settings.

## Outcome

The implementation outcome was good:

- #153 was implemented by the daemon-launched worker and merged as PR #154.
- #152 was implemented by the daemon-launched worker and merged as PR #155.
- Both implementation PRs were verified by the worker before merge.
- The final open PR list was empty.

The orchestration outcome was mixed:

- Daemon mode successfully launched real worker execution and produced mergeable PRs.
- The first bounded daemon run did not process two distinct tasks as expected.
- Cycle 2 reprocessed #153 instead of moving to #152, because #153 still had an open PR / ready-for-review state and `--force-reprocess` made it eligible again.
- #152 had to be processed by a second bounded daemon run after #154 was merged.

## What went well

- Daemon mode was usable for real repository work, not just dry-run smoke testing.
- Status checkpoints made the long session understandable: done/current/next/problems/manual actions were easy to report between batches.
- The worker handled sizeable changes in central orchestration code and converged after intermediate test failures.
- PR gating was safe: clean PRs were inspected and merged sequentially.
- The new `orchestrator status` surface from #153 immediately improves operator visibility.
- The new `--conflict-recovery-only` mode from #152 directly addresses a pain point from the previous North Star batch.

## What was painful

- `--max-cycles 2 --limit 2 --force-reprocess` did not mean “process two distinct tasks”.
- The daemon selected #153 twice, which made the batch less predictable and required manual intervention to continue with #152.
- The daemon currently lacks an obvious single-pass batch mode for “take the next N tasks once each”.
- Large central runner edits still create long test cycles and large diffs.

## Unexpected behavior

Created follow-up issue #156 for daemon batch progression:

- expected: a bounded daemon run with two eligible issues processes two distinct issues;
- observed: daemon reprocessed the first issue in the same invocation;
- likely cause: current selection/reprocess semantics do not remember tasks already handled during the current daemon invocation.

## Assessment

The implementation work itself went well. Both requested North Star tasks landed and improved the product in meaningful ways.

The daemon-mode experiment was successful enough to keep using, but it exposed an important scheduler/selection gap. Daemon can execute work end-to-end, but for operator-controlled batches it needs better per-invocation progress semantics.

## Follow-ups

1. Fix #156 so bounded daemon batches can progress across distinct tasks.
2. Consider a documented `single-pass-batch` mode or equivalent default for `--max-cycles N --limit N`.
3. Keep using the new status command during future long-running daemon sessions.
4. Continue reducing central-runner change size through modularization before larger Go-core migration work.
