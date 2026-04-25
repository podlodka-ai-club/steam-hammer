import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import ensure_pr, prepare_issue_branch


class ExistingBranchAndPrReuseTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
