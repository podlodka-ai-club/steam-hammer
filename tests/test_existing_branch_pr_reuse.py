import io
import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    ensure_pr,
    main,
    prepare_issue_branch,
    push_branch,
    sync_reused_branch_with_base,
)


class ExistingBranchAndPrReuseTests(unittest.TestCase):
    @patch("scripts.run_github_issues_to_opencode.run_command")
    def test_push_branch_uses_force_with_lease_when_requested(self, run_command_mock) -> None:
        push_branch(
            branch_name="issue-fix/33-sync-reused-branch",
            dry_run=False,
            force_with_lease=True,
        )

        run_command_mock.assert_called_once_with(
            ["git", "push", "-u", "--force-with-lease", "origin", "issue-fix/33-sync-reused-branch"]
        )

    @patch("scripts.run_github_issues_to_opencode.run_command")
    @patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False)
    @patch("scripts.run_github_issues_to_opencode.local_branch_exists", return_value=True)
    def test_prepare_issue_branch_reuses_existing_local_branch(
        self,
        _local_branch_exists,
        _remote_branch_exists,
        run_command_mock,
    ) -> None:
        status = prepare_issue_branch(
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            dry_run=False,
            fail_on_existing=False,
        )

        self.assertEqual(status, "reused")
        self.assertEqual(
            run_command_mock.call_args_list,
            [
                unittest.mock.call(["git", "checkout", "main"]),
                unittest.mock.call(["git", "checkout", "issue-fix/26-rerun"]),
            ],
        )

    @patch("scripts.run_github_issues_to_opencode.run_command")
    @patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False)
    @patch("scripts.run_github_issues_to_opencode.local_branch_exists", return_value=False)
    def test_prepare_issue_branch_creates_new_branch_when_missing(
        self,
        _local_branch_exists,
        _remote_branch_exists,
        run_command_mock,
    ) -> None:
        status = prepare_issue_branch(
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            dry_run=False,
            fail_on_existing=False,
        )

        self.assertEqual(status, "created")
        self.assertEqual(
            run_command_mock.call_args_list,
            [
                unittest.mock.call(["git", "checkout", "main"]),
                unittest.mock.call(["git", "checkout", "-b", "issue-fix/26-rerun"]),
            ],
        )

    @patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False)
    @patch("scripts.run_github_issues_to_opencode.local_branch_exists", return_value=True)
    def test_prepare_issue_branch_fails_in_strict_mode(
        self,
        _local_branch_exists,
        _remote_branch_exists,
    ) -> None:
        with self.assertRaises(RuntimeError):
            prepare_issue_branch(
                base_branch="main",
                branch_name="issue-fix/26-rerun",
                dry_run=False,
                fail_on_existing=True,
            )

    @patch("scripts.run_github_issues_to_opencode.run_command")
    @patch("scripts.run_github_issues_to_opencode.current_head_sha")
    def test_sync_reused_branch_with_base_rebase_happy_path(
        self,
        current_head_sha_mock,
        run_command_mock,
    ) -> None:
        current_head_sha_mock.side_effect = ["sha-before", "sha-after"]
        changed = sync_reused_branch_with_base(
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            strategy="rebase",
            dry_run=False,
        )

        self.assertEqual(
            run_command_mock.call_args_list,
            [
                unittest.mock.call(["git", "fetch", "origin", "main"]),
                unittest.mock.call(["git", "rebase", "origin/main"]),
            ],
        )
        self.assertTrue(changed)

    @patch("scripts.run_github_issues_to_opencode.command_succeeds", return_value=True)
    @patch("scripts.run_github_issues_to_opencode.run_command")
    @patch("scripts.run_github_issues_to_opencode.current_head_sha")
    def test_sync_reused_branch_with_base_rebase_conflict_falls_back_to_merge(
        self,
        current_head_sha_mock,
        run_command_mock,
        command_succeeds_mock,
    ) -> None:
        current_head_sha_mock.side_effect = ["sha-before-rebase", "sha-before-merge", "sha-after"]
        run_command_mock.side_effect = [
            None,
            RuntimeError("Command failed: git rebase origin/main"),
            None,
        ]

        changed = sync_reused_branch_with_base(
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            strategy="rebase",
            dry_run=False,
        )

        self.assertTrue(changed)
        self.assertEqual(
            run_command_mock.call_args_list,
            [
                unittest.mock.call(["git", "fetch", "origin", "main"]),
                unittest.mock.call(["git", "rebase", "origin/main"]),
                unittest.mock.call(["git", "merge", "--no-edit", "-X", "theirs", "origin/main"]),
            ],
        )
        command_succeeds_mock.assert_called_once_with(["git", "rebase", "--abort"])

    @patch("scripts.run_github_issues_to_opencode.run_command")
    @patch("scripts.run_github_issues_to_opencode.current_head_sha")
    @patch("scripts.run_github_issues_to_opencode.list_conflicted_paths")
    def test_sync_reused_branch_with_base_merge_conflict_auto_resolves(
        self,
        list_conflicted_paths_mock,
        current_head_sha_mock,
        run_command_mock,
    ) -> None:
        list_conflicted_paths_mock.return_value = ["README.md", "scripts/run_github_issues_to_opencode.py"]
        current_head_sha_mock.side_effect = ["sha-before", "sha-after"]
        run_command_mock.side_effect = [
            None,
            RuntimeError("Command failed: git merge --no-edit -X theirs origin/main"),
            None,
            None,
            None,
            None,
        ]

        changed = sync_reused_branch_with_base(
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            strategy="merge",
            dry_run=False,
        )

        self.assertTrue(changed)
        self.assertEqual(
            run_command_mock.call_args_list,
            [
                unittest.mock.call(["git", "fetch", "origin", "main"]),
                unittest.mock.call(["git", "merge", "--no-edit", "-X", "theirs", "origin/main"]),
                unittest.mock.call(["git", "checkout", "--theirs", "--", "README.md"]),
                unittest.mock.call(
                    ["git", "checkout", "--theirs", "--", "scripts/run_github_issues_to_opencode.py"]
                ),
                unittest.mock.call(["git", "add", "-A"]),
                unittest.mock.call(["git", "commit", "--no-edit"]),
            ],
        )

    @patch("scripts.run_github_issues_to_opencode.command_succeeds", return_value=True)
    @patch("scripts.run_github_issues_to_opencode.run_command")
    @patch("scripts.run_github_issues_to_opencode.current_head_sha", return_value="sha-before")
    @patch("scripts.run_github_issues_to_opencode.list_conflicted_paths", return_value=[])
    def test_sync_reused_branch_with_base_merge_conflict_auto_resolve_failure_raises(
        self,
        _list_conflicted_paths,
        _current_head_sha,
        run_command_mock,
        command_succeeds_mock,
    ) -> None:
        run_command_mock.side_effect = [
            None,
            RuntimeError("Command failed: git merge --no-edit -X theirs origin/main"),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            r"Failed to auto-resolve merge conflicts while syncing reused branch",
        ):
            sync_reused_branch_with_base(
                base_branch="main",
                branch_name="issue-fix/26-rerun",
                strategy="merge",
                dry_run=False,
            )

        command_succeeds_mock.assert_called_once_with(["git", "merge", "--abort"])

    def test_sync_reused_branch_with_base_rejects_unknown_strategy(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"Unsupported sync strategy 'squash'"):
            sync_reused_branch_with_base(
                base_branch="main",
                branch_name="issue-fix/26-rerun",
                strategy="squash",
                dry_run=False,
            )

    def test_main_stops_issue_when_reused_branch_sync_fails(self) -> None:
        args = type("Args", (), {
            "repo": "owner/repo",
            "issue": 33,
            "state": "open",
            "limit": 10,
            "runner": "opencode",
            "agent": "build",
            "model": None,
            "agent_timeout_seconds": 900,
            "agent_idle_timeout_seconds": None,
            "opencode_auto_approve": False,
            "branch_prefix": "issue-fix",
            "include_empty": False,
            "stop_on_error": False,
            "fail_on_existing": False,
            "force_issue_flow": False,
            "sync_reused_branch": True,
            "sync_strategy": "rebase",
            "dir": ".",
            "local_config": "local-config.json",
            "dry_run": False,
        })()

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 33,
                    "title": "Sync reused branch",
                    "body": "rerun",
                    "url": "https://github.com/owner/repo/issues/33",
                },
            ),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused"),
            patch(
                "scripts.run_github_issues_to_opencode.sync_reused_branch_with_base",
                side_effect=RuntimeError("sync failed"),
            ),
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch("sys.stderr", new_callable=io.StringIO) as stderr_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        run_agent_mock.assert_not_called()
        self.assertIn("Issue #33 failed: sync failed", stderr_mock.getvalue())

    def test_main_pushes_sync_only_rebase_updates_with_force_with_lease(self) -> None:
        args = type("Args", (), {
            "repo": "owner/repo",
            "issue": 33,
            "state": "open",
            "limit": 10,
            "runner": "opencode",
            "agent": "build",
            "model": None,
            "agent_timeout_seconds": 900,
            "agent_idle_timeout_seconds": None,
            "opencode_auto_approve": False,
            "branch_prefix": "issue-fix",
            "include_empty": False,
            "stop_on_error": False,
            "fail_on_existing": False,
            "force_issue_flow": False,
            "sync_reused_branch": True,
            "sync_strategy": "rebase",
            "dir": ".",
            "local_config": "local-config.json",
            "dry_run": False,
        })()

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 33,
                    "title": "Sync reused branch",
                    "body": "rerun",
                    "url": "https://github.com/owner/repo/issues/33",
                },
            ),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused"),
            patch(
                "scripts.run_github_issues_to_opencode.sync_reused_branch_with_base",
                return_value=True,
            ),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
            patch("scripts.run_github_issues_to_opencode.has_changes", return_value=False),
            patch("scripts.run_github_issues_to_opencode.push_branch") as push_branch_mock,
            patch(
                "scripts.run_github_issues_to_opencode.ensure_pr",
                return_value=("reused", "https://github.com/owner/repo/pull/34"),
            ) as ensure_pr_mock,
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        push_branch_mock.assert_called_once_with(
            branch_name="issue-fix/33-sync-reused-branch",
            dry_run=False,
            force_with_lease=True,
        )
        ensure_pr_mock.assert_called_once_with(
            repo="owner/repo",
            base_branch="main",
            branch_name="issue-fix/33-sync-reused-branch",
            issue={
                "number": 33,
                "title": "Sync reused branch",
                "body": "rerun",
                "url": "https://github.com/owner/repo/issues/33",
            },
            dry_run=False,
            fail_on_existing=False,
            stacked_base_context=None,
        )
        run_command_mock.assert_called_once_with(["git", "checkout", "main"])

    def test_main_pr_review_mode_rerun_with_conflicted_open_pr_auto_resolves_and_pushes(self) -> None:
        args = type("Args", (), {
            "repo": "owner/repo",
            "issue": 35,
            "state": "open",
            "limit": 10,
            "runner": "opencode",
            "agent": "build",
            "model": None,
            "agent_timeout_seconds": 900,
            "agent_idle_timeout_seconds": None,
            "opencode_auto_approve": False,
            "branch_prefix": "issue-fix",
            "include_empty": False,
            "stop_on_error": False,
            "fail_on_existing": False,
            "force_issue_flow": False,
            "sync_reused_branch": True,
            "sync_strategy": "rebase",
            "dir": ".",
            "local_config": "local-config.json",
            "dry_run": False,
        })()

        def run_command_side_effect(command: list[str]) -> None:
            if command == ["git", "rebase", "origin/main"]:
                raise RuntimeError("Command failed: git rebase origin/main")
            if command == ["git", "merge", "--no-edit", "-X", "theirs", "origin/main"]:
                raise RuntimeError("Command failed: git merge --no-edit -X theirs origin/main")

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 35,
                    "title": "Auto-resolve PR conflicts in pr-review mode",
                    "body": "Fix conflict handling",
                    "url": "https://github.com/owner/repo/issues/35",
                },
            ),
            patch(
                "scripts.run_github_issues_to_opencode.find_open_pr_for_issue",
                return_value={
                    "number": 77,
                    "headRefName": "issue-fix/35-auto-resolve-pr-conflicts",
                    "baseRefName": "main",
                },
            ),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_pull_request",
                return_value={
                    "number": 77,
                    "title": "Fix sync conflicts",
                    "url": "https://github.com/owner/repo/pull/77",
                    "state": "OPEN",
                    "mergeStateStatus": "DIRTY",
                    "body": "PR body",
                    "reviews": [],
                    "author": {"login": "pr-owner"},
                },
            ),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_pr_review_threads",
                return_value=[
                    {
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {
                                    "body": "Please resolve sync conflicts automatically",
                                    "path": "scripts/run_github_issues_to_opencode.py",
                                    "line": 1,
                                    "outdated": False,
                                    "author": {"login": "reviewer"},
                                    "url": "https://github.com/owner/repo/pull/77#discussion_r1",
                                }
                            ]
                        },
                    }
                ],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments",
                return_value=[],
            ),
            patch("scripts.run_github_issues_to_opencode.load_linked_issue_context", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused"),
            patch(
                "scripts.run_github_issues_to_opencode.current_head_sha",
                side_effect=["sha-before-rebase", "sha-before-merge", "sha-after-merge"],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.list_conflicted_paths",
                return_value=["README.md"],
            ),
            patch("scripts.run_github_issues_to_opencode.command_succeeds", return_value=True),
            patch(
                "scripts.run_github_issues_to_opencode.run_command",
                side_effect=run_command_side_effect,
            ),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
            patch("scripts.run_github_issues_to_opencode.has_changes", return_value=False),
            patch("scripts.run_github_issues_to_opencode.push_branch") as push_branch_mock,
            patch(
                "scripts.run_github_issues_to_opencode.ensure_pr",
                return_value=("reused", "https://github.com/owner/repo/pull/77"),
            ) as ensure_pr_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        push_branch_mock.assert_called_once_with(
            branch_name="issue-fix/35-auto-resolve-pr-conflicts",
            dry_run=False,
            force_with_lease=True,
        )
        ensure_pr_mock.assert_called_once_with(
            repo="owner/repo",
            base_branch="main",
            branch_name="issue-fix/35-auto-resolve-pr-conflicts",
            issue={
                "number": 35,
                "title": "Auto-resolve PR conflicts in pr-review mode",
                "body": "Fix conflict handling",
                "url": "https://github.com/owner/repo/issues/35",
            },
            dry_run=False,
            fail_on_existing=False,
            stacked_base_context=None,
        )

        output = stdout_mock.getvalue()
        self.assertIn("Selected mode: pr-review", output)
        self.assertIn("mergeStateStatus=DIRTY", output)
        self.assertIn("Conflict detected during rebase sync", output)
        self.assertIn("Conflict detected during merge sync", output)
        self.assertIn("Sync-only push result for issue #35", output)
        self.assertIn("PR #77 rerun sync pushed", output)
        self.assertIn(
            "GitHub mergeability should be recalculated without manual conflict steps",
            output,
        )

    def test_main_pr_review_mode_conflicted_pr_without_actionable_comments_still_syncs(self) -> None:
        args = type("Args", (), {
            "repo": "owner/repo",
            "issue": 35,
            "state": "open",
            "limit": 10,
            "runner": "opencode",
            "agent": "build",
            "model": None,
            "agent_timeout_seconds": 900,
            "agent_idle_timeout_seconds": None,
            "opencode_auto_approve": False,
            "branch_prefix": "issue-fix",
            "include_empty": False,
            "stop_on_error": False,
            "fail_on_existing": False,
            "force_issue_flow": False,
            "sync_reused_branch": True,
            "sync_strategy": "rebase",
            "dir": ".",
            "local_config": "local-config.json",
            "dry_run": False,
        })()

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 35,
                    "title": "Auto-resolve PR conflicts in pr-review mode",
                    "body": "Fix conflict handling",
                    "url": "https://github.com/owner/repo/issues/35",
                },
            ),
            patch(
                "scripts.run_github_issues_to_opencode.find_open_pr_for_issue",
                return_value={
                    "number": 77,
                    "headRefName": "issue-fix/35-auto-resolve-pr-conflicts",
                    "baseRefName": "main",
                },
            ),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_pull_request",
                return_value={
                    "number": 77,
                    "title": "Fix sync conflicts",
                    "url": "https://github.com/owner/repo/pull/77",
                    "state": "OPEN",
                    "mergeStateStatus": "DIRTY",
                    "body": "PR body",
                    "reviews": [],
                    "author": {"login": "pr-owner"},
                },
            ),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_review_threads", return_value=[]),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments",
                return_value=[],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.normalize_review_items",
                return_value=([], {}),
            ),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused"),
            patch(
                "scripts.run_github_issues_to_opencode.sync_reused_branch_with_base",
                return_value=True,
            ),
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.has_changes", return_value=False),
            patch("scripts.run_github_issues_to_opencode.push_branch") as push_branch_mock,
            patch(
                "scripts.run_github_issues_to_opencode.ensure_pr",
                return_value=("reused", "https://github.com/owner/repo/pull/77"),
            ) as ensure_pr_mock,
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        run_agent_mock.assert_not_called()
        push_branch_mock.assert_called_once_with(
            branch_name="issue-fix/35-auto-resolve-pr-conflicts",
            dry_run=False,
            force_with_lease=True,
        )
        ensure_pr_mock.assert_called_once_with(
            repo="owner/repo",
            base_branch="main",
            branch_name="issue-fix/35-auto-resolve-pr-conflicts",
            issue={
                "number": 35,
                "title": "Auto-resolve PR conflicts in pr-review mode",
                "body": "Fix conflict handling",
                "url": "https://github.com/owner/repo/issues/35",
            },
            dry_run=False,
            fail_on_existing=False,
            stacked_base_context=None,
        )
        run_command_mock.assert_called_once_with(["git", "checkout", "main"])

        output = stdout_mock.getvalue()
        self.assertIn("Selected mode: pr-review", output)
        self.assertIn("mergeStateStatus=DIRTY", output)
        self.assertIn("No actionable review comments for linked PR #77", output)
        self.assertIn("Skipping agent run for issue #35 in pr-review mode", output)
        self.assertIn("Sync-only push result for issue #35", output)
        self.assertIn("PR #77 rerun sync pushed", output)
        self.assertIn(
            "GitHub mergeability should be recalculated without manual conflict steps",
            output,
        )

    @patch("scripts.run_github_issues_to_opencode.open_pr")
    @patch(
        "scripts.run_github_issues_to_opencode.find_existing_pr",
        return_value={"number": 99, "url": "https://github.com/owner/repo/pull/99"},
    )
    def test_ensure_pr_reuses_existing_open_pr(
        self,
        _find_existing_pr,
        open_pr_mock,
    ) -> None:
        status, url = ensure_pr(
            repo="owner/repo",
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            issue={"number": 26, "title": "Handle reruns", "url": "https://example.com"},
            dry_run=False,
            fail_on_existing=False,
        )

        self.assertEqual(status, "reused")
        self.assertEqual(url, "https://github.com/owner/repo/pull/99")
        open_pr_mock.assert_not_called()

    @patch("scripts.run_github_issues_to_opencode.open_pr", return_value="https://github.com/owner/repo/pull/101")
    @patch("scripts.run_github_issues_to_opencode.find_existing_pr", return_value=None)
    def test_ensure_pr_creates_when_missing(
        self,
        _find_existing_pr,
        _open_pr,
    ) -> None:
        status, url = ensure_pr(
            repo="owner/repo",
            base_branch="main",
            branch_name="issue-fix/26-rerun",
            issue={"number": 26, "title": "Handle reruns", "url": "https://example.com"},
            dry_run=False,
            fail_on_existing=False,
        )

        self.assertEqual(status, "created")
        self.assertEqual(url, "https://github.com/owner/repo/pull/101")

    @patch(
        "scripts.run_github_issues_to_opencode.find_existing_pr",
        return_value={"number": 99, "url": "https://github.com/owner/repo/pull/99"},
    )
    def test_ensure_pr_fails_in_strict_mode(self, _find_existing_pr) -> None:
        with self.assertRaises(RuntimeError):
            ensure_pr(
                repo="owner/repo",
                base_branch="main",
                branch_name="issue-fix/26-rerun",
                issue={"number": 26, "title": "Handle reruns", "url": "https://example.com"},
                dry_run=False,
                fail_on_existing=True,
            )

    @patch(
        "scripts.run_github_issues_to_opencode.find_existing_pr",
        return_value={
            "number": 24,
            "url": "https://github.com/owner/repo/pull/24",
            "baseRefName": "main",
        },
    )
    def test_ensure_pr_strict_mode_reports_existing_and_selected_bases(
        self,
        _find_existing_pr,
    ) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            r"to 'main' \(#24; selected base 'issue-fix/26-some-other-branch'\)",
        ):
            ensure_pr(
                repo="owner/repo",
                base_branch="issue-fix/26-some-other-branch",
                branch_name="issue-fix/23-pr-review-comments",
                issue={
                    "number": 23,
                    "title": "PR review comments",
                    "url": "https://example.com/issues/23",
                },
                dry_run=False,
                fail_on_existing=True,
            )

    def test_main_skips_issue_when_linked_open_pr_exists_in_batch_mode(self) -> None:
        args = type("Args", (), {
            "repo": "owner/repo",
            "issue": None,
            "pr": None,
            "from_review_comments": False,
            "state": "open",
            "limit": 10,
            "runner": "opencode",
            "agent": "build",
            "model": None,
            "agent_timeout_seconds": 900,
            "agent_idle_timeout_seconds": None,
            "opencode_auto_approve": False,
            "branch_prefix": "issue-fix",
            "include_empty": False,
            "stop_on_error": False,
            "fail_on_existing": False,
            "force_issue_flow": False,
            "skip_if_pr_exists": True,
            "skip_if_branch_exists": True,
            "force_reprocess": False,
            "sync_reused_branch": True,
            "sync_strategy": "rebase",
            "base_branch": "default",
            "dir": ".",
            "local_config": "local-config.json",
            "dry_run": True,
        })()

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issues",
                return_value=[
                    {
                        "number": 33,
                        "title": "Do not duplicate",
                        "body": "non-empty",
                        "url": "https://github.com/owner/repo/issues/33",
                    }
                ],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.find_open_pr_for_issue",
                return_value={
                    "number": 44,
                    "url": "https://github.com/owner/repo/pull/44",
                },
            ),
            patch("scripts.run_github_issues_to_opencode.remote_branch_exists") as remote_branch_exists_mock,
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        remote_branch_exists_mock.assert_not_called()
        prepare_issue_branch_mock.assert_not_called()
        run_agent_mock.assert_not_called()
        self.assertIn("Skipping issue #33: PR #44", stdout_mock.getvalue())
        self.assertIn("Processed: 0", stdout_mock.getvalue())
        self.assertIn("skipped_existing_pr: 1", stdout_mock.getvalue())

    def test_main_single_issue_reuses_existing_remote_branch_context(self) -> None:
        args = type("Args", (), {
            "repo": "owner/repo",
            "issue": 33,
            "pr": None,
            "from_review_comments": False,
            "state": "open",
            "limit": 10,
            "runner": "opencode",
            "agent": "build",
            "model": None,
            "agent_timeout_seconds": 900,
            "agent_idle_timeout_seconds": None,
            "opencode_auto_approve": False,
            "branch_prefix": "issue-fix",
            "include_empty": False,
            "stop_on_error": False,
            "fail_on_existing": False,
            "force_issue_flow": False,
            "skip_if_pr_exists": False,
            "skip_if_branch_exists": True,
            "force_reprocess": False,
            "sync_reused_branch": True,
            "sync_strategy": "rebase",
            "base_branch": "default",
            "dir": ".",
            "local_config": "local-config.json",
            "dry_run": True,
        })()

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 33,
                    "title": "Do not duplicate",
                    "body": "non-empty",
                    "url": "https://github.com/owner/repo/issues/33",
                },
            ),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None) as find_open_pr_mock,
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=True),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0) as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.has_changes", return_value=False),
            patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("reused", "https://example/pull/44")),
            patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        find_open_pr_mock.assert_called_once_with(repo="owner/repo", issue_number=33)
        prepare_issue_branch_mock.assert_called_once()
        run_agent_mock.assert_called_once()
        self.assertIn("Found existing remote branch for issue #33", stdout_mock.getvalue())
        self.assertIn("Processed: 1", stdout_mock.getvalue())
        self.assertIn("skipped_existing_branch: 0", stdout_mock.getvalue())


if __name__ == "__main__":
    unittest.main()
