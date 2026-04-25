import argparse
import unittest
from unittest.mock import call, patch

from scripts.run_github_issues_to_opencode import detect_default_branch, find_existing_pr, main


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
                "--repo",
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
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
            patch("scripts.run_github_issues_to_opencode.commit_changes"),
            patch("scripts.run_github_issues_to_opencode.push_branch"),
            patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("reused", "")),
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


if __name__ == "__main__":
    unittest.main()
