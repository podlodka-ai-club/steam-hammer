import unittest
from unittest.mock import Mock

from scripts import github_lifecycle


class GitHubLifecycleHelperTests(unittest.TestCase):
    def test_pr_links_jira_issue_via_branch_name(self) -> None:
        linked = github_lifecycle.pr_links_issue(
            {
                "title": "Refactor runner",
                "body": "No direct tracker refs in text",
                "headRefName": "issue-fix/proj-42-runner-refactor",
            },
            {"number": "PROJ-42", "title": "Refactor runner"},
            issue_tracker=lambda _issue: "jira",
            tracker_github="github",
            format_issue_ref_from_issue=lambda issue: str(issue["number"]),
        )

        self.assertTrue(linked)

    def test_fetch_pr_conversation_comments_normalizes_comment_shape(self) -> None:
        comments = github_lifecycle.fetch_pr_conversation_comments(
            "owner/repo",
            17,
            fetch_issue_comments=lambda repo, number: [
                {
                    "user": {"login": "reviewer"},
                    "body": "  Please tighten this helper.  ",
                    "html_url": "https://example.test/comment/1",
                }
            ],
        )

        self.assertEqual(
            comments,
            [
                {
                    "author": "reviewer",
                    "body": "Please tighten this helper.",
                    "url": "https://example.test/comment/1",
                }
            ],
        )

    def test_ensure_pr_passes_stack_context_to_open_pr(self) -> None:
        open_pr = Mock(return_value="https://github.com/owner/repo/pull/101")

        status, url = github_lifecycle.ensure_pr(
            "owner/repo",
            "feature/stack-parent",
            "issue-fix/42-runner-refactor",
            {"number": 42, "title": "Refactor runner", "url": "https://example.test/issues/42"},
            False,
            False,
            find_existing_pr=lambda _repo, _base, _branch: None,
            open_pr=open_pr,
            stacked_base_context="feature/stack-parent",
        )

        self.assertEqual((status, url), ("created", "https://github.com/owner/repo/pull/101"))
        open_pr.assert_called_once_with(
            "owner/repo",
            "feature/stack-parent",
            "issue-fix/42-runner-refactor",
            {"number": 42, "title": "Refactor runner", "url": "https://example.test/issues/42"},
            False,
            "feature/stack-parent",
        )


if __name__ == "__main__":
    unittest.main()
