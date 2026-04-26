import unittest
from unittest.mock import call, patch

from scripts.run_github_issues_to_opencode import commit_changes, stage_worktree_changes


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

    def test_commit_changes_stages_baseline_and_commits(self) -> None:
        pre_run_untracked = {"scratch.log"}
        with (
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
            patch("scripts.run_github_issues_to_opencode.stage_worktree_changes") as stage_mock,
        ):
            commit_changes(
                issue={"number": 12, "title": "Add Jira template"},
                dry_run=False,
                pre_run_untracked_files=pre_run_untracked,
            )

        stage_mock.assert_called_once_with(pre_run_untracked)
        run_command_mock.assert_called_once_with(["git", "commit", "-m", "Fix issue #12: Add Jira template"])
