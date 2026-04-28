import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.run_github_issues_to_opencode import (
    TRACKER_GITHUB,
    TRACKER_JIRA,
    branch_name_for_issue,
    commit_changes,
    configure_active_providers,
    fetch_jira_issue,
    fetch_jira_issues,
    issue_commit_title,
    main,
    post_decomposition_plan_comment,
    parse_args,
    resolve_codehost_provider,
    resolve_tracker_provider,
)


class _FakeHttpResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class JiraTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        configure_active_providers(
            resolve_tracker_provider(TRACKER_GITHUB),
            resolve_codehost_provider("github"),
        )

    def test_parser_defaults_to_github_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            args = parse_args(["--dir", tmpdir])

        self.assertEqual(args.tracker, TRACKER_GITHUB)

    def test_parser_accepts_jira_issue_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            args = parse_args(["--dir", tmpdir, "--tracker", "jira", "--issue", "PROJ-42"])

        self.assertEqual(args.tracker, TRACKER_JIRA)
        self.assertEqual(args.issue, "PROJ-42")

    def test_missing_jira_env_fails_before_git_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(sys, "argv", ["prog", "--dir", tmpdir, "--tracker", "jira", "--issue", "PROJ-42"]),
                patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree") as ensure_clean_worktree_mock,
                patch("sys.stderr", stderr),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertIn("Missing required Jira environment variables", stderr.getvalue())
        ensure_clean_worktree_mock.assert_not_called()

    def test_pr_review_mode_accepts_jira_tracker_with_github_codehost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            stdout = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "JIRA_BASE_URL": "https://example.atlassian.net",
                        "JIRA_EMAIL": "dev@example.com",
                        "JIRA_API_TOKEN": "token-123",
                    },
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "prog",
                        "--dir",
                        tmpdir,
                        "--tracker",
                        "jira",
                        "--codehost",
                        "github",
                        "--repo",
                        "owner/repo",
                        "--pr",
                        "76",
                        "--from-review-comments",
                        "--dry-run",
                    ],
                ),
                patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree") as ensure_clean_worktree_mock,
                patch("sys.stdout", stdout),
                patch(
                    "scripts.run_github_issues_to_opencode.fetch_pull_request",
                    return_value={
                        "number": 76,
                        "state": "OPEN",
                        "headRefName": "feature/pr-76",
                        "baseRefName": "main",
                        "author": {"login": "dev"},
                        "reviews": [],
                        "headRefOid": "deadbeef",
                    },
                ),
                patch("scripts.run_github_issues_to_opencode.fetch_pr_review_threads", return_value=[]),
                patch("scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments", return_value=[]),
                patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
                patch("scripts.run_github_issues_to_opencode.current_branch", return_value="feature/pr-76"),
                patch("scripts.run_github_issues_to_opencode.checkout_pr_target_branch"),
                patch("scripts.run_github_issues_to_opencode.read_pr_ci_status_for_pull_request", return_value={"overall": "success", "checks": [], "failing_checks": [], "pending_checks": []}),
                patch("scripts.run_github_issues_to_opencode.validate_required_files_in_pr", return_value={"status": "passed"}),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        ensure_clean_worktree_mock.assert_called_once()

    def test_pr_review_mode_rejects_non_github_codehost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            stderr = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "JIRA_BASE_URL": "https://example.atlassian.net",
                        "JIRA_EMAIL": "dev@example.com",
                        "JIRA_API_TOKEN": "token-123",
                    },
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "prog",
                        "--dir",
                        tmpdir,
                        "--tracker",
                        "jira",
                        "--codehost",
                        "bitbucket",
                        "--pr",
                        "76",
                        "--from-review-comments",
                    ],
                ),
                patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree") as ensure_clean_worktree_mock,
                patch("sys.stderr", stderr),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertIn("only supports --codehost github", stderr.getvalue())
        ensure_clean_worktree_mock.assert_not_called()

    def test_fetch_jira_issue_maps_payload(self) -> None:
        request_mock = Mock()

        def fake_urlopen(request):
            request_mock.method = request.get_method()
            request_mock.url = request.full_url
            request_mock.authorization = request.get_header("Authorization")
            return _FakeHttpResponse(
                {
                    "key": "PROJ-42",
                    "fields": {
                        "summary": "Fix the failing flow",
                        "description": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "First line."}],
                                },
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Second line."}],
                                },
                            ],
                        },
                    },
                }
            )

        with (
            patch.dict(
                os.environ,
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_EMAIL": "dev@example.com",
                    "JIRA_API_TOKEN": "token-123",
                },
                clear=True,
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            issue = fetch_jira_issue("PROJ-42")

        self.assertEqual(request_mock.method, "GET")
        self.assertEqual(
            request_mock.url,
            "https://example.atlassian.net/rest/api/3/issue/PROJ-42",
        )
        self.assertTrue(str(request_mock.authorization).startswith("Basic "))
        self.assertEqual(
            issue,
            {
                "number": "PROJ-42",
                "title": "Fix the failing flow",
                "body": "First line.\nSecond line.",
                "url": "https://example.atlassian.net/browse/PROJ-42",
                "tracker": TRACKER_JIRA,
            },
        )

    def test_fetch_jira_issues_posts_search_request(self) -> None:
        request_mock = Mock()

        def fake_urlopen(request):
            request_mock.method = request.get_method()
            request_mock.url = request.full_url
            request_mock.payload = json.loads(request.data.decode("utf-8"))
            return _FakeHttpResponse(
                {
                    "issues": [
                        {
                            "key": "PROJ-42",
                            "fields": {
                                "summary": "First issue",
                                "description": {"type": "doc", "content": []},
                            },
                        },
                        {
                            "key": "PROJ-43",
                            "fields": {
                                "summary": "Second issue",
                                "description": "plain text body",
                            },
                        },
                    ]
                }
            )

        with (
            patch.dict(
                os.environ,
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_EMAIL": "dev@example.com",
                    "JIRA_API_TOKEN": "token-123",
                },
                clear=True,
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            issues = fetch_jira_issues("status != Done ORDER BY created DESC", 5)

        self.assertEqual(request_mock.method, "POST")
        self.assertEqual(
            request_mock.url,
            "https://example.atlassian.net/rest/api/3/issue/search",
        )
        self.assertEqual(
            request_mock.payload,
            {
                "jql": "status != Done ORDER BY created DESC",
                "maxResults": 5,
                "fields": ["summary", "description", "assignee"],
            },
        )
        self.assertEqual(
            issues,
            [
                {
                    "number": "PROJ-42",
                    "title": "First issue",
                    "body": "",
                    "url": "https://example.atlassian.net/browse/PROJ-42",
                    "tracker": TRACKER_JIRA,
                },
                {
                    "number": "PROJ-43",
                    "title": "Second issue",
                    "body": "plain text body",
                    "url": "https://example.atlassian.net/browse/PROJ-43",
                    "tracker": TRACKER_JIRA,
                },
            ],
        )

    def test_github_naming_behavior_is_unchanged(self) -> None:
        issue = {"number": 42, "title": "Short title", "tracker": TRACKER_GITHUB}

        self.assertEqual(branch_name_for_issue(issue, "issue-fix"), "issue-fix/42-short-title")
        self.assertEqual(issue_commit_title(issue), "Fix issue #42: Short title")

    def test_jira_naming_uses_issue_key(self) -> None:
        issue = {"number": "PROJ-42", "title": "Short title", "tracker": TRACKER_JIRA}

        self.assertEqual(branch_name_for_issue(issue, "issue-fix"), "issue-fix/proj-42-short-title")
        self.assertEqual(issue_commit_title(issue), "Fix PROJ-42: Short title")

    def test_github_tracker_still_uses_github_fetch_path(self) -> None:
        previous_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.mkdir(os.path.join(tmpdir, ".git"))
                stdout = io.StringIO()
                issue = {
                    "number": 42,
                    "title": "Title",
                    "body": "Body",
                    "url": "https://github.com/owner/repo/issues/42",
                    "tracker": TRACKER_GITHUB,
                }
                with (
                    patch.object(sys, "argv", ["prog", "--dir", tmpdir, "--tracker", "github", "--issue", "42", "--dry-run"]),
                    patch("sys.stdout", stdout),
                    patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
                    patch("scripts.run_github_issues_to_opencode.detect_repo", return_value="owner/repo"),
                    patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
                    patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue) as fetch_issue_mock,
                    patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
                    patch("scripts.run_github_issues_to_opencode.fetch_jira_issue") as fetch_jira_issue_mock,
                    patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
                    patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False),
                    patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
                    patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
                    patch("scripts.run_github_issues_to_opencode.push_branch"),
                    patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("created", "")),
                    patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
                    patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
                    patch("scripts.run_github_issues_to_opencode.run_configured_workflow_checks", return_value=[]),
                ):
                    exit_code = main()
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        fetch_issue_mock.assert_called_once_with(repo="owner/repo", number=42)
        fetch_jira_issue_mock.assert_not_called()

    def test_jira_tracker_uses_jira_list_path_for_open_issues(self) -> None:
        previous_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.mkdir(os.path.join(tmpdir, ".git"))
                stdout = io.StringIO()
                issue = {
                    "number": "PROJ-42",
                    "title": "Title",
                    "body": "Body",
                    "url": "https://example.atlassian.net/browse/PROJ-42",
                    "tracker": TRACKER_JIRA,
                }
                with (
                    patch.dict(
                        os.environ,
                        {
                            "JIRA_BASE_URL": "https://example.atlassian.net",
                            "JIRA_EMAIL": "dev@example.com",
                            "JIRA_API_TOKEN": "token-123",
                        },
                        clear=True,
                    ),
                    patch.object(sys, "argv", ["prog", "--dir", tmpdir, "--tracker", "jira", "--limit", "5", "--dry-run"]),
                    patch("sys.stdout", stdout),
                    patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
                    patch("scripts.run_github_issues_to_opencode.detect_repo", return_value="owner/repo"),
                    patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
                    patch("scripts.run_github_issues_to_opencode.fetch_jira_issues", return_value=[issue]) as fetch_jira_issues_mock,
                    patch("scripts.run_github_issues_to_opencode.fetch_issues") as fetch_issues_mock,
                    patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
                    patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False),
                    patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
                    patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
                    patch("scripts.run_github_issues_to_opencode.push_branch"),
                    patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("created", "")),
                    patch("scripts.run_github_issues_to_opencode.run_configured_workflow_checks", return_value=[]),
                    patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
                ):
                    exit_code = main()
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        fetch_jira_issues_mock.assert_called_once_with(
            jql="status != Done ORDER BY created DESC",
            limit=5,
        )
        fetch_issues_mock.assert_not_called()

    def test_jira_single_issue_still_checks_for_linked_pr_and_jira_state(self) -> None:
        previous_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.mkdir(os.path.join(tmpdir, ".git"))
                stdout = io.StringIO()
                issue = {
                    "number": "PROJ-42",
                    "title": "Title",
                    "body": "Body",
                    "url": "https://example.atlassian.net/browse/PROJ-42",
                    "tracker": TRACKER_JIRA,
                }
                linked_pr = {
                    "number": 76,
                    "url": "https://github.com/owner/repo/pull/76",
                }
                with (
                    patch.dict(
                        os.environ,
                        {
                            "JIRA_BASE_URL": "https://example.atlassian.net",
                            "JIRA_EMAIL": "dev@example.com",
                            "JIRA_API_TOKEN": "token-123",
                        },
                        clear=True,
                    ),
                    patch.object(sys, "argv", ["prog", "--dir", tmpdir, "--tracker", "jira", "--issue", "PROJ-42", "--force-issue-flow", "--dry-run"]),
                    patch("sys.stdout", stdout),
                    patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
                    patch("scripts.run_github_issues_to_opencode.detect_repo", return_value="owner/repo"),
                    patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
                    patch("scripts.run_github_issues_to_opencode.fetch_jira_issue", return_value=issue),
                    patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=linked_pr) as find_open_pr_mock,
                    patch("scripts.run_github_issues_to_opencode.fetch_jira_issue_comments", return_value=[]) as fetch_jira_comments_mock,
                    patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
                    patch("scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments", return_value=[]),
                    patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
                    patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False),
                    patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
                    patch("scripts.run_github_issues_to_opencode.push_branch"),
                    patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("created", "")),
                    patch("scripts.run_github_issues_to_opencode.run_configured_workflow_checks", return_value=[]),
                ):
                    exit_code = main()
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        find_open_pr_mock.assert_called_once_with(repo="owner/repo", issue=issue)
        fetch_jira_comments_mock.assert_called_once_with(issue_key="PROJ-42")

    def test_decomposition_plan_comment_uses_active_tracker_provider(self) -> None:
        tracker_provider = resolve_tracker_provider(TRACKER_JIRA)
        codehost_provider = resolve_codehost_provider("github")
        configure_active_providers(tracker_provider, codehost_provider)
        self.addCleanup(
            configure_active_providers,
            resolve_tracker_provider(TRACKER_GITHUB),
            resolve_codehost_provider("github"),
        )

        with (
            patch.dict(
                os.environ,
                {
                    "JIRA_BASE_URL": "https://example.atlassian.net",
                    "JIRA_EMAIL": "dev@example.com",
                    "JIRA_API_TOKEN": "token-123",
                },
                clear=True,
            ),
            patch("scripts.run_github_issues_to_opencode.post_jira_issue_comment") as post_jira_comment_mock,
            patch("scripts.run_github_issues_to_opencode.run_command") as run_command_mock,
        ):
            post_decomposition_plan_comment(
                repo="owner/repo",
                issue_number="PROJ-42",
                payload={"status": "proposed", "proposed_children": []},
                dry_run=False,
            )

        post_jira_comment_mock.assert_called_once()
        run_command_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
