import unittest
from unittest.mock import call, patch

from scripts.run_github_issues_to_opencode import (
    BranchContextMismatchError,
    ResidualUntrackedFilesError,
    commit_changes,
    push_branch,
    stage_worktree_changes,
)


class StagingBehaviorTests(unittest.TestCase):
    def test_stage_worktree_changes_adds_only_new_untracked_files(self) -> None:
        pre_run_untracked = {"notes/todo.txt", "scratch.log"}
        post_run_untracked = {
            "notes/todo.txt",
            "scratch.log",
            "docs/jira-issue-template.md",
        }

        with (
            patch(
                "scripts.run_github_issues_to_opencode.list_untracked_files",
                return_value=post_run_untracked,
            ) as list_untracked_files,
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
        ):
            stage_worktree_changes(pre_run_untracked_files=pre_run_untracked)

        run_command_mock.assert_has_calls(
            [
                call(["git", "add", "-u"]),
                call(["git", "add", "--", "docs/jira-issue-template.md"]),
            ]
        )
        self.assertEqual(list_untracked_files.call_count, 1)

    def test_stage_worktree_changes_does_not_stage_preexisting_untracked_files(self) -> None:
        pre_run_untracked = {"scratch.log", "notes/todo.txt"}
        post_run_untracked = {"scratch.log", "notes/todo.txt"}

        with (
            patch(
                "scripts.run_github_issues_to_opencode.list_untracked_files",
                return_value=post_run_untracked,
            ) as list_untracked_files,
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
        ):
            stage_worktree_changes(pre_run_untracked_files=pre_run_untracked)

        run_command_mock.assert_called_once_with(["git", "add", "-u"])
        self.assertEqual(list_untracked_files.call_count, 1)

    def test_stage_worktree_changes_without_baseline_only_updates_tracked_changes(self) -> None:
        with (
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
            patch(
                "scripts.run_github_issues_to_opencode.list_untracked_files",
            ) as list_untracked_files,
        ):
            stage_worktree_changes()

        run_command_mock.assert_called_once_with(["git", "add", "-u"])
        list_untracked_files.assert_not_called()

    def test_commit_changes_raises_when_new_untracked_files_remain(self) -> None:
        pre_run_untracked = {"notes/todo.txt", "scratch.log"}

        with (
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
            patch(
                "scripts.run_github_issues_to_opencode.list_untracked_files",
                side_effect=[
                    {"notes/todo.txt", "scratch.log", "build/artifacts/new.txt"},
                    {"notes/todo.txt", "scratch.log", "build/artifacts/new.txt"},
                ],
            ) as list_untracked_files,
        ):
            with self.assertRaises(ResidualUntrackedFilesError) as exc_ctx:
                commit_changes(
                    issue={"number": 85, "title": "Add residual tracking"},
                    dry_run=False,
                    pre_run_untracked_files=pre_run_untracked,
                )

        list_untracked_files.assert_called()
        run_command_mock.assert_has_calls(
            [
                call(["git", "add", "-u"]),
                call(["git", "add", "--", "build/artifacts/new.txt"]),
                call(["git", "commit", "-m", "Fix issue #85: Add residual tracking"]),
            ]
        )
        self.assertEqual(exc_ctx.exception.files, ["build/artifacts/new.txt"])

    def test_commit_changes_does_not_fail_when_residual_files_were_preexisting(self) -> None:
        pre_run_untracked = {"notes/todo.txt", "scratch.log", "build/artifacts/new.txt"}

        with (
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
            patch(
                "scripts.run_github_issues_to_opencode.list_untracked_files",
                side_effect=[
                    {"notes/todo.txt", "scratch.log", "build/artifacts/new.txt"},
                    {"notes/todo.txt", "scratch.log", "build/artifacts/new.txt"},
                ],
            ),
        ):
            commit_changes(
                issue={"number": 85, "title": "Add residual tracking"},
                dry_run=False,
                pre_run_untracked_files=pre_run_untracked,
            )

        run_command_mock.assert_has_calls(
            [
                call(["git", "add", "-u"]),
                call(["git", "commit", "-m", "Fix issue #85: Add residual tracking"]),
            ]
        )

    def test_commit_changes_stages_baseline_and_commits(self) -> None:
        pre_run_untracked = {"scratch.log"}
        with (
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
            patch("scripts.run_github_issues_to_opencode.stage_worktree_changes") as stage_mock,
            patch(
                "scripts.run_github_issues_to_opencode.list_untracked_files",
                side_effect=[pre_run_untracked, pre_run_untracked],
            ),
        ):
            commit_changes(
                issue={"number": 12, "title": "Add Jira template"},
                dry_run=False,
                pre_run_untracked_files=pre_run_untracked,
            )

        stage_mock.assert_called_once_with(pre_run_untracked)
        run_command_mock.assert_called_once_with(["git", "commit", "-m", "Fix issue #12: Add Jira template"])

    def test_commit_changes_fails_when_branch_context_does_not_match(self) -> None:
        with (
            patch("scripts.run_github_issues_to_opencode.current_branch", return_value="issue-fix/194-child"),
            patch("scripts.run_github_issues_to_opencode.current_repo_root", return_value="/tmp/worker-194"),
            patch("scripts.run_github_issues_to_opencode.stage_worktree_changes") as stage_mock,
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
        ):
            with self.assertRaises(BranchContextMismatchError) as exc_ctx:
                commit_changes(
                    issue={"number": 192, "title": "Automate failed recovery follow-up"},
                    dry_run=False,
                    expected_branch="issue-fix/192-child",
                    expected_repo_root="/tmp/worker-192",
                )

        self.assertIn("expected branch 'issue-fix/192-child'", str(exc_ctx.exception))
        stage_mock.assert_not_called()
        run_command_mock.assert_not_called()

    def test_push_branch_fails_when_repo_context_does_not_match(self) -> None:
        with (
            patch("scripts.run_github_issues_to_opencode.current_branch", return_value="issue-fix/192-child"),
            patch("scripts.run_github_issues_to_opencode.current_repo_root", return_value="/tmp/worker-194"),
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
        ):
            with self.assertRaises(BranchContextMismatchError) as exc_ctx:
                push_branch(
                    branch_name="issue-fix/192-child",
                    dry_run=False,
                    expected_repo_root="/tmp/worker-192",
                )

        self.assertIn("repo '/tmp/worker-192'", str(exc_ctx.exception))
        run_command_mock.assert_not_called()
