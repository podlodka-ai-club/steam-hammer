# Final Smoke Checklist

This note records the final bounded smoke coverage for the Go-first runtime boundary after #268.

## Boundary Under Test

- Go owns execution-mode selection, fresh-branch `run issue`, daemon selection/claim logic, detached worker surfaces, and post-batch verification wiring.
- Python is a compatibility adapter for the still-unmigrated runtime paths: `run pr`, reused-branch/conflict-recovery issue paths, and batch/daemon worker execution loops that have not moved to Go yet.
- Guardrail: when Go already selected `pr-review`, `run issue` must route to the explicit PR compatibility adapter (`--pr ... --from-review-comments`) instead of delegating the whole issue decision back to `--issue` Python flow.

## Checklist

| Scenario | Coverage command | Expected evidence | Result |
| --- | --- | --- | --- |
| One-shot issue flow | `go test ./internal/cli -run TestRunIssueUsesGoNativeHappyPath -count=1` | Native issue path completes without Python runner calls. | Passed on 2026-05-01 |
| PR review flow | `go test ./internal/cli -run 'TestRunIssueRoutesLinkedPRToPRCompatibilityAdapter|TestRunIssueRoutesReadyToMergeRecoveryToPRCompatibilityAdapter|TestRunPRCommandWiresPythonRunner' -count=1` | Go routes linked-PR issue recovery into explicit PR adapter; PR command still uses compatibility adapter. | Passed on 2026-05-01 |
| Detached batch flow | `go test ./internal/cli -run 'TestRunBatchDetachStartsOneWorkerPerIssue|TestRunBatchDetachPersistsBatchMetadataForChildWorkers' -count=1` | One worker per issue, isolated state, predictable worker metadata. | Passed on 2026-05-01 |
| Daemon with `--max-parallel-tasks 3` | `go test ./internal/cli -run TestRunDaemonDetachStartsThreeWorkersWhenRequested -count=1` | Three detached daemon workers are prepared with isolated directories/state. | Passed on 2026-05-01 |
| Verified merge queue path | `python3 -m unittest tests.test_pr_review_comments_mode tests.test_orchestration_state_recovery -q` | Readiness/merge-result verification reaches `ready-to-merge` and status summaries preserve verification evidence. | Passed on 2026-05-01 |

## Full Verification

- `go test ./...` -> passed on 2026-05-01
- `python3 -m unittest discover -s tests -q` -> passed on 2026-05-01

## Notes

- These are bounded repository-level smokes, not live GitHub side-effect runs.
- Live operator steps for detached/autonomous execution remain documented in `docs/daemon-smoke-test.md`.
- The Python unittest output includes fixture-driven warning/error lines from mocked scenarios, but the suite completed `OK` and is treated as passing verification.
