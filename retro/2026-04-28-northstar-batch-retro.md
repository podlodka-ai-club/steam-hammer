# Retro: North Star PR batch session

Date: 2026-04-28

## Outcome

- Merged the remaining North Star PR batch.
- Resolved repeated conflict cycles by rerunning workers for issues #96, #97, #100, and #101.
- Created follow-up issue #138 for post-merge Python test failures.
- Ran worker for #138, created PR #139, merged it.
- Final open PR list: empty.
- Final verification passed:
  - `python3 -m unittest discover -s tests` — 193 tests OK.
  - `go test ./...` — OK.
- Local `main` ended clean and up to date with `origin/main`.

## What went well

- We did not merge dirty/conflicting PRs blindly.
- GitHub remained the tracker source of truth:
  - added PR comments with branch/recovery context;
  - created issue #138 when post-merge verification found a blocker.
- Sequential merge + rerun loop worked despite heavy overlap.
- Final verification caught a real regression in merge policy normalization before stopping.
- Provider-qualified model string `openai/gpt-5.4` was used successfully.

## What was painful

- Many PRs edited the same hot files, especially:
  - `scripts/run_github_issues_to_opencode.py`
  - `tests/test_orchestration_state_recovery.py`
  - workflow/config example files
- Each merge often made the remaining PRs dirty/conflicting again.
- Worker reruns sometimes re-implemented or extended issue work instead of doing only branch conflict recovery.
- Full Python suite takes about 5 minutes and emits noisy mocked `gh` warnings.
- Runner output around rebase failures and merge fallback is verbose and hard to scan.

## Process improvements

- Merge large epic batches in smaller waves grouped by file ownership / conflict surface.
- After each merge, automatically:
  1. refresh open PR mergeability,
  2. merge any clean PRs,
  3. rerun only dirty/conflicting PRs.
- Add a dedicated conflict-recovery mode that only syncs/rebases and resolves conflicts, without asking the worker to revisit the entire issue scope.
- Add a post-batch verification issue/checklist automatically when a large batch finishes.
- Suppress or isolate expected mocked `gh` warnings in tests so real failures are easier to spot.

## Action items

1. Run a daemon smoke test on clean `main`.
2. Create a task to reduce full Python suite noise and/or split slow verification modes.
3. Create a task for smarter reused-branch conflict recovery.
4. Consider splitting the large Python runner into smaller modules to reduce conflict pressure.
