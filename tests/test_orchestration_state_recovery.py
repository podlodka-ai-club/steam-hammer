import argparse
import io
import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    main,
    parse_orchestration_state_comment_body,
    select_latest_parseable_orchestration_state,
)


class OrchestrationStateRecoveryTests(unittest.TestCase):
    def test_parse_orchestration_state_comment_body_parses_fenced_json(self) -> None:
        body = (
            "Runner state update\n"
            "<!-- orchestration-state:v1 -->\n"
            "```json\n"
            '{"status":"ready-for-review","summary":"done"}\n'
            "```\n"
        )

        payload, error = parse_orchestration_state_comment_body(body)

        self.assertIsNone(error)
        self.assertEqual(payload, {"status": "ready-for-review", "summary": "done"})

    def test_select_latest_parseable_state_ignores_malformed_comments(self) -> None:
        comments = [
            {
                "id": 1,
                "created_at": "2026-04-25T10:00:00Z",
                "html_url": "https://example/1",
                "body": "<!-- orchestration-state:v1 -->\n```json\n{not-json}\n```",
            },
            {
                "id": 2,
                "created_at": "2026-04-25T11:00:00Z",
                "html_url": "https://example/2",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"failed","error":"test failure"}\n'
                    "```"
                ),
            },
            {
                "id": 3,
                "created_at": "2026-04-25T12:00:00Z",
                "html_url": "https://example/3",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"waiting-for-author","reason":"need clarification"}\n'
                    "```"
                ),
            },
        ]

        latest, warnings = select_latest_parseable_orchestration_state(
            comments=comments,
            source_label="issue #45",
        )

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["status"], "waiting-for-author")
        self.assertEqual(latest["comment_id"], 3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("ignoring malformed orchestration state comment", warnings[0])

    def test_main_issue_flow_without_recovered_state_does_not_require_force_override(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=47,
            pr=None,
            from_review_comments=False,
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
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
        )
        issue = {
            "number": 47,
            "title": "Support current-branch or stacked execution mode",
            "body": "Implement stacked execution",
            "url": "https://example/issues/47",
        }

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
            patch("scripts.run_github_issues_to_opencode.has_changes", return_value=False),
            patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("created", "")),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertIn("Selected mode: issue-flow", stdout_mock.getvalue())

    def test_main_issue_flow_skips_waiting_for_author_by_default(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=45,
            pr=None,
            from_review_comments=False,
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
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
        )
        issue = {
            "number": 45,
            "title": "Recover orchestration context",
            "body": "Implement state recovery",
            "url": "https://example/issues/45",
        }

        issue_comments = [
            {
                "id": 1,
                "created_at": "2026-04-26T12:00:00Z",
                "html_url": "https://example/issues/45#issuecomment-1",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"waiting-for-author","reason":"awaiting user answer"}\n'
                    "```"
                ),
            }
        ]

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=issue_comments),
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch") as prepare_issue_branch_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        run_agent_mock.assert_not_called()
        prepare_issue_branch_mock.assert_not_called()
        output = stdout_mock.getvalue()
        self.assertIn("Recovered orchestration state context", output)
        self.assertIn("waiting-for-author", output)
        self.assertIn("Skipping issue #45 due to recovered orchestration state", output)

    def test_main_issue_flow_failed_state_passes_context_to_prompt(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=45,
            pr=None,
            from_review_comments=False,
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
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
        )
        issue = {
            "number": 45,
            "title": "Recover orchestration context",
            "body": "Implement state recovery",
            "url": "https://example/issues/45",
        }
        issue_comments = [
            {
                "id": 2,
                "created_at": "2026-04-26T13:00:00Z",
                "html_url": "https://example/issues/45#issuecomment-2",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"failed","error":"merge conflict while rebasing"}\n'
                    "```"
                ),
            }
        ]

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=issue_comments),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused"),
            patch("scripts.run_github_issues_to_opencode.sync_reused_branch_with_base", return_value=False),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0) as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.commit_changes"),
            patch("scripts.run_github_issues_to_opencode.push_branch"),
            patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("reused", "")),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertTrue(run_agent_mock.called)
        prompt_override = run_agent_mock.call_args.kwargs.get("prompt_override")
        self.assertIsNotNone(prompt_override)
        assert prompt_override is not None
        self.assertIn("Recovered previous orchestration failure context", prompt_override)
        self.assertIn("merge conflict while rebasing", prompt_override)

    def test_main_pr_mode_dry_run_prints_recovered_state_context(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=None,
            pr=12,
            from_review_comments=True,
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
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
        )

        pr_comments = [
            {
                "id": 99,
                "created_at": "2026-04-26T14:00:00Z",
                "html_url": "https://example/pull/12#issuecomment-99",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"ready-for-review","summary":"opening review"}\n'
                    "```"
                ),
            }
        ]
        pull_request = {
            "number": 12,
            "title": "PR title",
            "url": "https://example/pull/12",
            "state": "OPEN",
            "reviews": [],
        }

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=pr_comments),
            patch("scripts.run_github_issues_to_opencode.fetch_pull_request", return_value=pull_request),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_review_threads", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments", return_value=[]),
            patch(
                "scripts.run_github_issues_to_opencode.normalize_review_items",
                return_value=([], {}),
            ),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        output = stdout_mock.getvalue()
        self.assertIn("[dry-run] Recovered orchestration state context", output)
        self.assertIn("status=ready-for-review", output)


if __name__ == "__main__":
    unittest.main()
