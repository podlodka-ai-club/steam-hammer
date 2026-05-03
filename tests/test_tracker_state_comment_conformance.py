import unittest
from datetime import UTC, datetime, timedelta

from scripts.orchestration_state import ORCHESTRATION_STATE_MARKER, select_latest_parseable_orchestration_state
from scripts.provider_helpers import GitHubTrackerProvider, ProviderRuntime
from scripts.run_github_issues_to_opencode import format_orchestration_state_comment


class _InMemoryGitHubIssueComments:
    def __init__(self) -> None:
        self._comments_by_issue: dict[int | str, list[dict]] = {}
        self._next_id = 1
        self._clock = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)

    def list_issue_comments(self, *, repo: str, issue_number: int | str) -> list[dict]:
        _ = repo
        comments = self._comments_by_issue.get(issue_number, [])
        return [dict(item) for item in comments]

    def run_command(self, args: list[str]) -> None:
        if args[:3] != ["gh", "issue", "comment"]:
            raise AssertionError(f"unexpected command: {args!r}")

        issue_number = int(args[3])
        body = _arg_value(args, "--body")
        comment = {
            "id": self._next_id,
            "body": body,
            "created_at": self._clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "html_url": f"https://example.test/issues/{issue_number}#issuecomment-{self._next_id}",
            "user": {"login": "orchestrator-bot"},
        }
        self._next_id += 1
        self._clock += timedelta(minutes=1)
        self._comments_by_issue.setdefault(issue_number, []).append(comment)

    def inject_comment(self, issue_number: int, comment: dict) -> None:
        self._comments_by_issue.setdefault(issue_number, []).append(dict(comment))


def _arg_value(args: list[str], name: str) -> str:
    index = args.index(name)
    return args[index + 1]


def _provider_runtime_with_fake_comments(fake_comments: _InMemoryGitHubIssueComments) -> ProviderRuntime:
    def _unsupported(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("unexpected runtime callback usage in conformance test")

    return ProviderRuntime(
        tracker_github="github",
        tracker_jira="jira",
        codehost_github="github",
        parse_tracker=lambda value: str(value).strip().lower(),
        parse_codehost=lambda value: str(value).strip().lower(),
        normalize_issue_number=lambda value, _tracker: int(value) if str(value).isdigit() else str(value),
        jira_credentials_from_env=lambda: {"base_url": "https://example.atlassian.net"},
        jira_request_json=_unsupported,
        fetch_jira_issue=_unsupported,
        fetch_jira_issues=_unsupported,
        jira_description_to_text=lambda value: str(value or ""),
        get_fetch_issue=lambda: _unsupported,
        get_fetch_issues=lambda: _unsupported,
        get_fetch_issue_comments=lambda: fake_comments.list_issue_comments,
        get_fetch_jira_issue_comments=lambda: _unsupported,
        get_post_jira_issue_comment=lambda: _unsupported,
        get_run_command=lambda: fake_comments.run_command,
        get_run_capture=lambda: _unsupported,
        get_create_decomposition_child_issue=lambda: _unsupported,
        get_ensure_agent_failure_label=lambda: _unsupported,
        get_format_issue_ref=lambda: _unsupported,
        get_detect_repo=lambda: _unsupported,
        get_detect_default_branch=lambda: _unsupported,
        get_find_open_pr_for_issue=lambda: _unsupported,
        get_fetch_pull_request=lambda: _unsupported,
        get_fetch_pr_review_threads=lambda: _unsupported,
        get_fetch_pr_conversation_comments=lambda: _unsupported,
        get_read_pr_ci_status_for_pull_request=lambda: _unsupported,
        get_load_linked_issue_context=lambda: _unsupported,
        get_ensure_pr=lambda: _unsupported,
    )


class TrackerStateCommentConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = "owner/repo"
        self.issue_number = 356
        self.comments = _InMemoryGitHubIssueComments()
        runtime = _provider_runtime_with_fake_comments(self.comments)
        self.provider = GitHubTrackerProvider(runtime)

    def _post_state(self, *, status: str, stage: str, next_action: str) -> None:
        body = format_orchestration_state_comment(
            {
                "status": status,
                "task_type": "issue",
                "issue": self.issue_number,
                "stage": stage,
                "next_action": next_action,
            }
        )
        self.provider.post_issue_comment(self.repo, self.issue_number, body)

    def _recover_latest(self) -> tuple[dict | None, list[str]]:
        return select_latest_parseable_orchestration_state(
            comments=self.provider.list_issue_comments(self.repo, self.issue_number),
            source_label=f"issue #{self.issue_number}",
        )

    def test_missing_state_returns_none_without_warnings(self) -> None:
        latest, warnings = self._recover_latest()

        self.assertIsNone(latest)
        self.assertEqual(warnings, [])

    def test_write_and_recover_latest_state(self) -> None:
        self._post_state(status="in-progress", stage="agent_run", next_action="wait_for_agent_result")

        latest, warnings = self._recover_latest()

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(warnings, [])
        self.assertEqual(latest["status"], "in-progress")
        self.assertEqual(latest["payload"]["next_action"], "wait_for_agent_result")

    def test_latest_state_selection_prefers_newest_timestamp(self) -> None:
        self.comments.inject_comment(
            self.issue_number,
            {
                "id": 99,
                "created_at": "2026-05-01T10:02:00Z",
                "html_url": "https://example.test/issues/356#issuecomment-99",
                "body": format_orchestration_state_comment({"status": "failed", "issue": self.issue_number}),
            },
        )
        self.comments.inject_comment(
            self.issue_number,
            {
                "id": 100,
                "created_at": "2026-05-01T10:01:00Z",
                "html_url": "https://example.test/issues/356#issuecomment-100",
                "body": format_orchestration_state_comment({"status": "blocked", "issue": self.issue_number}),
            },
        )

        latest, warnings = self._recover_latest()

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(warnings, [])
        self.assertEqual(latest["comment_id"], 99)
        self.assertEqual(latest["status"], "failed")

    def test_malformed_comments_are_ignored_with_warning(self) -> None:
        self.comments.inject_comment(
            self.issue_number,
            {
                "id": 1,
                "created_at": "2026-05-01T10:00:00Z",
                "html_url": "https://example.test/issues/356#issuecomment-1",
                "body": f"{ORCHESTRATION_STATE_MARKER}\n```json\n{{not-json}}\n```",
            },
        )
        self._post_state(status="ready-for-review", stage="agent_run", next_action="await_human_review")

        latest, warnings = self._recover_latest()

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["status"], "ready-for-review")
        self.assertEqual(len(warnings), 1)
        self.assertIn("ignoring malformed orchestration state comment", warnings[0])

    def test_marker_is_preserved_and_state_updates_append_new_comment(self) -> None:
        self._post_state(status="in-progress", stage="agent_run", next_action="run_checks")
        self._post_state(status="blocked", stage="ci_checks", next_action="inspect_failing_ci_checks")

        comments = self.provider.list_issue_comments(self.repo, self.issue_number)
        self.assertEqual(len(comments), 2)
        self.assertIn(ORCHESTRATION_STATE_MARKER, comments[0]["body"])
        self.assertIn(ORCHESTRATION_STATE_MARKER, comments[1]["body"])

        latest, warnings = self._recover_latest()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(warnings, [])
        self.assertEqual(latest["status"], "blocked")
        self.assertEqual(latest["payload"]["stage"], "ci_checks")


if __name__ == "__main__":
    unittest.main()
