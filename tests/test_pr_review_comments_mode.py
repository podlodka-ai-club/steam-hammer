import importlib.util
import pathlib
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
        self.assertEqual(stats["comments_outdated"], 1)

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
                "reviews": [],
            }

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(self.mod, "ensure_clean_worktree"),
                mock.patch.object(self.mod, "current_branch", return_value="feature/pr23"),
                mock.patch.object(self.mod, "detect_repo", return_value="owner/repo"),
                mock.patch.object(self.mod, "fetch_pull_request", return_value=pull_request),
                mock.patch.object(self.mod, "fetch_pr_review_threads", return_value=[]),
                mock.patch.object(
                    self.mod,
                    "normalize_review_items",
                    return_value=([], {"threads_total": 0, "threads_resolved": 0, "comments_total": 0, "comments_outdated": 0, "comments_empty": 0, "reviews_used": 0}),
                ),
            ):
                exit_code = self.mod.main()

        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
