import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest
from unittest import mock


def load_script_module() -> types.ModuleType:
    script_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_github_issues_to_opencode.py"
    spec = importlib.util.spec_from_file_location("runner_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load script module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrReviewModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_script_module()

    def test_normalize_review_items_filters_non_actionable(self) -> None:
        threads = [
            {
                "isResolved": True,
                "comments": {"nodes": [{"body": "resolved", "path": "a.py", "line": 1}]},
            },
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "body": "",
                            "path": "b.py",
                            "line": 2,
                            "outdated": False,
                            "author": {"login": "alice"},
                        },
                        {
                            "body": "please rename var",
                            "path": "b.py",
                            "line": 3,
                            "outdated": False,
                            "author": {"login": "bob"},
                            "url": "https://example/review/1",
                        },
                        {
                            "body": "stale",
                            "path": "b.py",
                            "line": 4,
                            "outdated": True,
                            "author": {"login": "carol"},
                        },
                    ]
                },
            },
        ]
        reviews = [
            {"state": "APPROVED", "body": "looks good"},
            {
                "state": "CHANGES_REQUESTED",
                "body": "also update docs",
                "author": {"login": "dave"},
                "url": "https://example/review/2",
            },
        ]

        items, stats = self.mod.normalize_review_items(threads=threads, reviews=reviews)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "review_comment")
        self.assertEqual(items[0]["path"], "b.py")
        self.assertEqual(items[0]["line"], 3)
        self.assertEqual(items[1]["type"], "review_summary")
        self.assertEqual(stats["threads_resolved"], 1)
        self.assertEqual(stats["threads_outdated"], 0)
        self.assertEqual(stats["comments_outdated"], 1)
        self.assertEqual(stats["reviews_non_actionable"], 1)

    def test_normalize_review_items_skips_outdated_threads(self) -> None:
        threads = [
            {
                "isResolved": False,
                "isOutdated": True,
                "comments": {
                    "nodes": [
                        {
                            "body": "obsolete thread",
                            "path": "a.py",
                            "line": 1,
                            "outdated": False,
                            "author": {"login": "alice"},
                        }
                    ]
                },
            }
        ]

        items, stats = self.mod.normalize_review_items(threads=threads, reviews=[])

        self.assertEqual(items, [])
        self.assertEqual(stats["threads_outdated"], 1)

    def test_normalize_review_items_includes_pr_author_replies(self) -> None:
        threads = [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "body": "done, fixed",
                            "path": "scripts/tool.py",
                            "line": 10,
                            "outdated": False,
                            "author": {"login": "pr-owner"},
                        },
                        {
                            "body": "please add a test",
                            "path": "scripts/tool.py",
                            "line": 12,
                            "outdated": False,
                            "author": {"login": "reviewer"},
                        },
                    ]
                },
            }
        ]

        items, stats = self.mod.normalize_review_items(
            threads=threads,
            reviews=[],
            pr_author_login="pr-owner",
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["author"], "pr-owner")
        self.assertEqual(items[1]["author"], "reviewer")
        self.assertEqual(stats["comments_pr_author"], 1)

    def test_normalize_review_items_uses_latest_review_state_per_author(self) -> None:
        reviews = [
            {
                "state": "CHANGES_REQUESTED",
                "body": "Please split this function",
                "author": {"login": "alice"},
                "submittedAt": "2026-04-24T10:00:00Z",
                "url": "https://example/review/11",
            },
            {
                "state": "APPROVED",
                "body": "Looks good now",
                "author": {"login": "alice"},
                "submittedAt": "2026-04-24T11:00:00Z",
                "url": "https://example/review/12",
            },
            {
                "state": "COMMENTED",
                "body": "Please also update README",
                "author": {"login": "bob"},
                "submittedAt": "2026-04-24T12:00:00Z",
                "url": "https://example/review/13",
            },
        ]

        items, stats = self.mod.normalize_review_items(threads=[], reviews=reviews)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "review_summary")
        self.assertEqual(items[0]["author"], "bob")
        self.assertEqual(stats["reviews_used"], 1)
        self.assertEqual(stats["reviews_superseded"], 1)

    def test_normalize_review_items_includes_actionable_approved_review_summary(self) -> None:
        reviews = [
            {
                "state": "APPROVED",
                "body": "Approved after you add docs for this endpoint",
                "author": {"login": "alice"},
                "submittedAt": "2026-04-24T12:00:00Z",
            }
        ]

        items, stats = self.mod.normalize_review_items(threads=[], reviews=reviews)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "review_summary")
        self.assertEqual(items[0]["state"], "APPROVED")
        self.assertEqual(stats["reviews_used"], 1)

    def test_normalize_review_items_filters_non_actionable_conversation_comments(self) -> None:
        comments = [
            {
                "author": "alice",
                "body": "thanks",
                "url": "https://example/comment/1",
            },
            {
                "author": "bob",
                "body": "Please update README and add a short migration note",
                "url": "https://example/comment/2",
            },
            {
                "author": "carol",
                "body": "",
                "url": "https://example/comment/3",
            },
        ]

        items, stats = self.mod.normalize_review_items(
            threads=[],
            reviews=[],
            conversation_comments=comments,
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "conversation_comment")
        self.assertEqual(items[0]["author"], "bob")
        self.assertEqual(stats["conversation_total"], 3)
        self.assertEqual(stats["conversation_non_actionable"], 1)
        self.assertEqual(stats["conversation_empty"], 1)

    def test_normalize_review_items_mixed_sources_preserve_priority_and_deduplicate(self) -> None:
        threads = [
            {
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {
                            "body": "Please rename this variable",
                            "path": "scripts/tool.py",
                            "line": 7,
                            "outdated": False,
                            "author": {"login": "alice"},
                            "url": "https://example/review-comment/1",
                        }
                    ]
                },
            }
        ]
        reviews = [
            {
                "state": "COMMENTED",
                "body": "Please rename this variable",
                "author": {"login": "alice"},
                "submittedAt": "2026-04-24T10:00:00Z",
            },
            {
                "state": "CHANGES_REQUESTED",
                "body": "Add tests for this behavior",
                "author": {"login": "bob"},
                "submittedAt": "2026-04-24T11:00:00Z",
            },
        ]
        comments = [
            {
                "author": "dave",
                "body": "Please update changelog",
                "url": "https://example/comment/10",
            }
        ]

        items, stats = self.mod.normalize_review_items(
            threads=threads,
            reviews=reviews,
            conversation_comments=comments,
        )

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["type"], "review_comment")
        self.assertEqual(items[1]["type"], "review_summary")
        self.assertEqual(items[2]["type"], "conversation_comment")
        self.assertEqual(stats["reviews_duplicates"], 1)

    def test_format_review_filtering_stats_contains_source_breakdown(self) -> None:
        stats = {
            "threads_total": 2,
            "threads_resolved": 1,
            "threads_outdated": 0,
            "comments_total": 3,
            "comments_used": 1,
            "comments_outdated": 1,
            "comments_empty": 1,
            "comments_pr_author": 0,
            "comments_duplicates": 0,
            "reviews_total": 2,
            "reviews_used": 1,
            "reviews_superseded": 1,
            "reviews_empty": 0,
            "reviews_pr_author": 0,
            "reviews_non_actionable": 0,
            "reviews_duplicates": 0,
            "conversation_total": 1,
            "conversation_used": 1,
            "conversation_empty": 0,
            "conversation_pr_author": 0,
            "conversation_non_actionable": 0,
            "conversation_duplicates": 0,
        }

        text = self.mod.format_review_filtering_stats(stats)

        self.assertIn("threads=total:2", text)
        self.assertIn("inline=total:3", text)
        self.assertIn("review_summaries=total:2", text)
        self.assertIn("conversation=total:1", text)

    def test_normalize_review_items_includes_pr_author_review_summaries(self) -> None:
        reviews = [
            {
                "state": "COMMENTED",
                "body": "I pushed a fix",
                "author": {"login": "pr-owner"},
                "submittedAt": "2026-04-24T10:00:00Z",
            },
            {
                "state": "CHANGES_REQUESTED",
                "body": "Still needs test coverage",
                "author": {"login": "reviewer"},
                "submittedAt": "2026-04-24T10:01:00Z",
            },
        ]

        items, stats = self.mod.normalize_review_items(
            threads=[],
            reviews=reviews,
            pr_author_login="pr-owner",
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["author"], "pr-owner")
        self.assertEqual(items[1]["author"], "reviewer")
        self.assertEqual(stats["reviews_pr_author"], 1)

    def test_build_pr_review_prompt_contains_locations_and_links(self) -> None:
        pull_request = {
            "number": 23,
            "title": "Improve parser",
            "url": "https://example/pr/23",
            "body": "PR description",
        }
        review_items = [
            {
                "type": "review_comment",
                "author": "alice",
                "body": "Fix this",
                "path": "scripts/tool.py",
                "line": 42,
                "url": "https://example/comment/1",
            }
        ]

        prompt = self.mod.build_pr_review_prompt(
            pull_request=pull_request,
            review_items=review_items,
        )

        self.assertIn("Pull Request: #23 - Improve parser", prompt)
        self.assertIn("Location: scripts/tool.py:42", prompt)
        self.assertIn("Link: https://example/comment/1", prompt)

    def test_format_orchestration_state_comment_contains_marker_and_parseable_json(self) -> None:
        state = self.mod.build_orchestration_state(
            status="in-progress",
            task_type="issue",
            issue_number=43,
            pr_number=None,
            branch="issue-fix/43-state-comments",
            base_branch="main",
            runner="opencode",
            agent="build",
            model=None,
            attempt=1,
            stage="agent_run",
            next_action="wait_for_agent_result",
            error=None,
        )

        body = self.mod.format_orchestration_state_comment(state)

        self.assertIn("<!-- orchestration-state:v1 -->", body)
        json_match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
        self.assertIsNotNone(json_match)
        payload = json.loads(json_match.group(1))
        self.assertEqual(payload["status"], "in-progress")
        self.assertEqual(payload["issue"], 43)
        self.assertEqual(payload["branch"], "issue-fix/43-state-comments")

    def test_post_orchestration_state_comment_dry_run_does_not_call_gh(self) -> None:
        state = self.mod.build_orchestration_state(
            status="ready-for-review",
            task_type="issue",
            issue_number=43,
            pr_number=120,
            branch="issue-fix/43-state-comments",
            base_branch="main",
            runner="opencode",
            agent="build",
            model="openai/gpt-5.3-codex",
            attempt=1,
            stage="pr_ready",
            next_action="wait_for_review",
            error=None,
        )

        with (
            mock.patch.object(self.mod, "run_command") as run_command_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            self.mod.post_orchestration_state_comment(
                repo="owner/repo",
                target_type="issue",
                target_number=43,
                state=state,
                dry_run=True,
            )

        run_command_mock.assert_not_called()
        self.assertIn("[dry-run] Would post orchestration state to issue #43", stdout_mock.getvalue())

    def test_load_linked_issue_context_fetches_missing_issue_body(self) -> None:
        pull_request = {
            "closingIssuesReferences": [
                {
                    "number": 17,
                    "title": "",
                    "body": "",
                    "url": "",
                }
            ]
        }

        with mock.patch.object(
            self.mod,
            "fetch_issue",
            return_value={
                "number": 17,
                "title": "Improve docs",
                "body": "Issue body context",
                "url": "https://example/issues/17",
            },
        ) as fetch_issue_mock:
            linked = self.mod.load_linked_issue_context(
                repo="owner/repo",
                pull_request=pull_request,
            )

        fetch_issue_mock.assert_called_once_with(repo="owner/repo", number=17)
        self.assertEqual(linked[0]["number"], 17)
        self.assertEqual(linked[0]["body"], "Issue body context")

    def test_fetch_pr_review_threads_raises_when_pr_missing(self) -> None:
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequest": None,
                }
            }
        }

        with mock.patch.object(
            self.mod,
            "run_capture",
            return_value=self.mod.json.dumps(graphql_response),
        ):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                self.mod.fetch_pr_review_threads(repo="owner/repo", number=23)

    def test_main_pr_mode_dry_run_handles_empty_actionable_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pathlib.Path(tmpdir, ".git").mkdir()
            argv = [
                "runner",
                "--pr",
                "23",
                "--from-review-comments",
                "--dry-run",
                "--dir",
                tmpdir,
            ]
            pull_request = {
                "number": 23,
                "title": "PR title",
                "url": "https://example/pr/23",
                "state": "OPEN",
                "headRefName": "feature/pr23",
                "reviews": [],
            }

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(self.mod, "ensure_clean_worktree"),
                mock.patch.object(self.mod, "current_branch", return_value="feature/pr23"),
                mock.patch.object(self.mod, "detect_repo", return_value="owner/repo"),
                mock.patch.object(self.mod, "fetch_pull_request", return_value=pull_request),
                mock.patch.object(self.mod, "local_branch_exists", return_value=True),
                mock.patch.object(self.mod, "fetch_pr_review_threads", return_value=[]),
                mock.patch.object(self.mod, "fetch_pr_conversation_comments", return_value=[]),
                mock.patch.object(
                    self.mod,
                    "normalize_review_items",
                    return_value=([], {"threads_total": 0, "threads_resolved": 0, "comments_total": 0, "comments_outdated": 0, "comments_empty": 0, "reviews_used": 0}),
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
            ):
                previous_cwd = os.getcwd()
                try:
                    exit_code = self.mod.main()
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        self.assertIn("[dry-run] Would post orchestration state to pr #23", stdout_mock.getvalue())

    def test_main_pr_mode_branch_mismatch_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pathlib.Path(tmpdir, ".git").mkdir()
            argv = [
                "runner",
                "--pr",
                "23",
                "--from-review-comments",
                "--dry-run",
                "--dir",
                tmpdir,
            ]
            pull_request = {
                "number": 23,
                "title": "PR title",
                "url": "https://example/pr/23",
                "state": "OPEN",
                "headRefName": "issue-fix/23",
                "reviews": [],
            }

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(self.mod, "ensure_clean_worktree"),
                mock.patch.object(self.mod, "current_branch", return_value="issue-fix/25-issue"),
                mock.patch.object(self.mod, "detect_repo", return_value="owner/repo"),
                mock.patch.object(self.mod, "fetch_pull_request", return_value=pull_request),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr_mock,
            ):
                previous_cwd = os.getcwd()
                try:
                    exit_code = self.mod.main()
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(exit_code, 1)
        self.assertIn("--allow-pr-branch-switch", stderr_mock.getvalue())

    def test_main_pr_mode_dry_run_isolate_worktree_reports_execution_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pathlib.Path(tmpdir, ".git").mkdir()
            argv = [
                "runner",
                "--pr",
                "23",
                "--from-review-comments",
                "--dry-run",
                "--isolate-worktree",
                "--dir",
                tmpdir,
            ]
            pull_request = {
                "number": 23,
                "title": "PR title",
                "url": "https://example/pr/23",
                "state": "OPEN",
                "headRefName": "issue-fix/23",
                "reviews": [],
            }

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(self.mod, "ensure_clean_worktree"),
                mock.patch.object(self.mod, "current_branch", return_value="issue-fix/25-issue"),
                mock.patch.object(self.mod, "detect_repo", return_value="owner/repo"),
                mock.patch.object(self.mod, "fetch_pull_request", return_value=pull_request),
                mock.patch.object(self.mod, "fetch_pr_review_threads", return_value=[]),
                mock.patch.object(self.mod, "fetch_pr_conversation_comments", return_value=[]),
                mock.patch.object(
                    self.mod,
                    "normalize_review_items",
                    return_value=([], {"threads_total": 0}),
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
            ):
                previous_cwd = os.getcwd()
                try:
                    exit_code = self.mod.main()
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        output = stdout_mock.getvalue()
        self.assertIn("[dry-run] PR mode target branch: issue-fix/23", output)
        self.assertIn("[dry-run] PR mode execution: isolated worktree", output)

    def test_issue_mode_auto_switch_uses_actionable_conversation_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pathlib.Path(tmpdir, ".git").mkdir()
            argv = [
                "runner",
                "--issue",
                "37",
                "--no-skip-if-pr-exists",
                "--dry-run",
                "--dir",
                tmpdir,
            ]
            issue = {
                "number": 37,
                "title": "Consider all PR comments in pr-review mode",
                "body": "Issue body",
                "url": "https://example/issues/37",
            }
            linked_pr = {
                "number": 101,
                "headRefName": "issue-fix/37-comments",
                "baseRefName": "main",
            }
            pull_request = {
                "number": 101,
                "title": "Fix PR-review comment aggregation",
                "url": "https://example/pr/101",
                "body": "PR description",
                "author": {"login": "author"},
                "reviews": [],
            }

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(self.mod, "ensure_clean_worktree"),
                mock.patch.object(self.mod, "detect_repo", return_value="owner/repo"),
                mock.patch.object(self.mod, "detect_default_branch", return_value="main"),
                mock.patch.object(self.mod, "fetch_issue", return_value=issue),
                mock.patch.object(self.mod, "find_open_pr_for_issue", return_value=linked_pr),
                mock.patch.object(self.mod, "fetch_pull_request", return_value=pull_request),
                mock.patch.object(self.mod, "fetch_pr_review_threads", return_value=[]),
                mock.patch.object(
                    self.mod,
                    "fetch_pr_conversation_comments",
                    return_value=[
                        {
                            "author": "maintainer",
                            "body": "Please include review summaries and issue comments",
                            "url": "https://example/comment/777",
                        }
                    ],
                ),
                mock.patch.object(self.mod, "prepare_issue_branch", return_value="reused"),
                mock.patch.object(self.mod, "sync_reused_branch_with_base", return_value=False),
                mock.patch.object(self.mod, "run_agent", return_value=0) as run_agent_mock,
                mock.patch.object(self.mod, "ensure_pr", return_value=("reused", "")),
                mock.patch.object(self.mod, "push_branch"),
                mock.patch.object(self.mod, "commit_changes"),
            ):
                previous_cwd = os.getcwd()
                try:
                    exit_code = self.mod.main()
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        self.assertTrue(run_agent_mock.called)
        prompt_override = run_agent_mock.call_args.kwargs["prompt_override"]
        self.assertIn("conversation_comment", prompt_override)
        self.assertIn("review summaries", prompt_override)


if __name__ == "__main__":
    unittest.main()
