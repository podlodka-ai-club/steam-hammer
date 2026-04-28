import argparse
import io
import unittest
from unittest.mock import call, patch

from scripts.run_github_issues_to_opencode import (
    current_branch_stack_warnings,
    detect_default_branch,
    find_existing_pr,
    main,
    open_pr,
)


class IssueBaseBranchResolutionTests(unittest.TestCase):
    @patch("scripts.run_github_issues_to_opencode.run_capture", return_value="main\n")
    def test_detect_default_branch_returns_repo_default(self, run_capture_mock) -> None:
        branch = detect_default_branch("owner/repo")

        self.assertEqual(branch, "main")
        run_capture_mock.assert_called_once_with(
            [
                "gh",
                "repo",
                "view",
                "owner/repo",
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ]
        )

    @patch("scripts.run_github_issues_to_opencode.run_capture")
    def test_find_existing_pr_falls_back_to_head_when_base_mismatch(
        self,
        run_capture_mock,
    ) -> None:
        run_capture_mock.side_effect = [
            "[]",
            '[{"number":24,"url":"https://github.com/owner/repo/pull/24","baseRefName":"main"}]',
        ]

        pr = find_existing_pr(
            repo="owner/repo",
            base_branch="issue-fix/26-some-other-branch",
            branch_name="issue-fix/23-pr-review-comments",
        )

        self.assertIsNotNone(pr)
        self.assertEqual(pr["number"], 24)
        self.assertEqual(pr["baseRefName"], "main")
        self.assertEqual(
            run_capture_mock.call_args_list,
            [
                call(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        "owner/repo",
                        "--base",
                        "issue-fix/26-some-other-branch",
                        "--head",
                        "issue-fix/23-pr-review-comments",
                        "--state",
                        "open",
                        "--limit",
                        "1",
                        "--json",
                        "number,url,baseRefName",
                    ]
                ),
                call(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        "owner/repo",
                        "--head",
                        "issue-fix/23-pr-review-comments",
                        "--state",
                        "open",
                        "--limit",
                        "2",
                        "--json",
                        "number,url,baseRefName",
                    ]
                ),
            ],
        )

    def test_main_uses_default_branch_instead_of_current_checkout(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=23,
            state="open",
            limit=10,
            runner="opencode",
            agent="build",
            model=None,
            agent_timeout_seconds=900,
            agent_idle_timeout_seconds=None,
            opencode_auto_approve=False,
            branch_prefix="issue-fix",
            include_empty=False,
            stop_on_error=False,
            fail_on_existing=False,
            force_issue_flow=False,
            sync_reused_branch=True,
            sync_strategy="rebase",
            dir=".",
            local_config="local-config.json",
            dry_run=True,
        )

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.current_branch") as current_branch_mock,
            patch(
                "scripts.run_github_issues_to_opencode.detect_default_branch",
                return_value="main",
            ),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 23,
                    "title": "PR review comments",
                    "body": "Fix reruns",
                    "url": "https://github.com/owner/repo/issues/23",
                },
            ),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.sync_reused_branch_with_base") as sync_reused_branch_with_base_mock,
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
            patch("scripts.run_github_issues_to_opencode.commit_changes"),
            patch("scripts.run_github_issues_to_opencode.push_branch"),
            patch(
                "scripts.run_github_issues_to_opencode.ensure_pr",
                return_value=("reused", "https://github.com/owner/repo/pull/24"),
            ) as ensure_pr_mock,
            patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        current_branch_mock.assert_not_called()
        prepare_issue_branch_mock.assert_called_once_with(
            base_branch="main",
            branch_name="issue-fix/23-pr-review-comments",
            dry_run=True,
            fail_on_existing=False,
        )
        ensure_pr_mock.assert_called_once_with(
            repo="owner/repo",
            base_branch="main",
            branch_name="issue-fix/23-pr-review-comments",
            issue={
                "number": 23,
                "title": "PR review comments",
                "body": "Fix reruns",
                "url": "https://github.com/owner/repo/issues/23",
            },
            dry_run=True,
            fail_on_existing=False,
            stacked_base_context=None,
        )
        sync_reused_branch_with_base_mock.assert_called_once_with(
            base_branch="main",
            branch_name="issue-fix/23-pr-review-comments",
            strategy="rebase",
            dry_run=True,
        )
        output = stdout_mock.getvalue()
        self.assertIn("[dry-run] Selected stable base branch: main", output)
        self.assertIn("[dry-run] Base mode: default (stack on current branch: no)", output)
        self.assertIn(
            "PR status for issue #23: reused (https://github.com/owner/repo/pull/24)",
            output,
        )

    def test_main_uses_current_branch_when_base_mode_current(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=23,
            state="open",
            limit=10,
            runner="opencode",
            agent="build",
            model=None,
            agent_timeout_seconds=900,
            agent_idle_timeout_seconds=None,
            opencode_auto_approve=False,
            branch_prefix="issue-fix",
            include_empty=False,
            stop_on_error=False,
            fail_on_existing=False,
            force_issue_flow=False,
            sync_reused_branch=True,
            sync_strategy="rebase",
            base_branch="current",
            dir=".",
            local_config="local-config.json",
            dry_run=True,
        )

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.current_branch", return_value="feature/stack-parent") as current_branch_mock,
            patch("scripts.run_github_issues_to_opencode.current_branch_stack_warnings", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch") as detect_default_branch_mock,
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue",
                return_value={
                    "number": 23,
                    "title": "PR review comments",
                    "body": "Fix reruns",
                    "url": "https://github.com/owner/repo/issues/23",
                },
            ),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.sync_reused_branch_with_base"),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
            patch("scripts.run_github_issues_to_opencode.commit_changes"),
            patch("scripts.run_github_issues_to_opencode.push_branch"),
            patch(
                "scripts.run_github_issues_to_opencode.ensure_pr",
                return_value=("reused", "https://github.com/owner/repo/pull/24"),
            ) as ensure_pr_mock,
            patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        current_branch_mock.assert_called_once_with()
        detect_default_branch_mock.assert_not_called()
        prepare_issue_branch_mock.assert_called_once_with(
            base_branch="feature/stack-parent",
            branch_name="issue-fix/23-pr-review-comments",
            dry_run=True,
            fail_on_existing=False,
        )
        ensure_pr_mock.assert_called_once_with(
            repo="owner/repo",
            base_branch="feature/stack-parent",
            branch_name="issue-fix/23-pr-review-comments",
            issue={
                "number": 23,
                "title": "PR review comments",
                "body": "Fix reruns",
                "url": "https://github.com/owner/repo/issues/23",
            },
            dry_run=True,
            fail_on_existing=False,
            stacked_base_context="feature/stack-parent",
        )
        output = stdout_mock.getvalue()
        self.assertIn("[dry-run] Selected current base branch: feature/stack-parent", output)
        self.assertIn("[dry-run] Base mode: current (stack on current branch: yes)", output)

    @patch("scripts.run_github_issues_to_opencode.run_capture")
    def test_open_pr_body_mentions_stacked_base_context(self, run_capture_mock) -> None:
        run_capture_mock.return_value = "https://github.com/owner/repo/pull/999\n"

        pr_url = open_pr(
            repo="owner/repo",
            base_branch="feature/stack-parent",
            branch_name="issue-fix/23-pr-review-comments",
            issue={
                "number": 23,
                "title": "PR review comments",
                "url": "https://github.com/owner/repo/issues/23",
            },
            dry_run=False,
            stacked_base_context="feature/stack-parent",
        )

        self.assertEqual(pr_url, "https://github.com/owner/repo/pull/999")
        called_command = run_capture_mock.call_args.args[0]
        body_value = called_command[called_command.index("--body") + 1]
        self.assertIn("## Stack Context", body_value)
        self.assertIn("Stacked on current branch: `feature/stack-parent`", body_value)

    @patch("scripts.run_github_issues_to_opencode.run_capture")
    @patch("scripts.run_github_issues_to_opencode.has_changes", return_value=True)
    def test_current_branch_stack_warnings_include_dirty_and_missing_upstream(
        self,
        _has_changes_mock,
        run_capture_mock,
    ) -> None:
        run_capture_mock.side_effect = RuntimeError("no upstream")

        warnings = current_branch_stack_warnings()

        self.assertIn("current branch has uncommitted changes", warnings[0])
        self.assertIn("current branch has no upstream tracking branch", warnings[1])


if __name__ == "__main__":
    unittest.main()
