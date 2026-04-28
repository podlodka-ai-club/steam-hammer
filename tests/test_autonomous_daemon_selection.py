import argparse
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    autonomous_session_processed_issue_numbers,
    build_orchestration_claim,
    evaluate_issue_scope,
    filter_autonomous_issues_for_single_pass,
    format_orchestration_claim_comment,
    is_active_orchestration_claim,
    load_autonomous_session_state,
    main,
    mark_autonomous_session_issue_processed,
    parse_orchestration_claim_comment_body,
    save_autonomous_session_state,
    sort_autonomous_issues,
)


class AutonomousDaemonSelectionTests(unittest.TestCase):
    def test_scope_evaluation_supports_assignee_priority_and_freshness(self) -> None:
        issue = {
            "number": 93,
            "labels": [{"name": "bug"}, {"name": "priority:high"}],
            "assignees": [{"login": "alice"}],
            "createdAt": "2026-04-26T10:00:00Z",
            "updatedAt": "2026-04-27T10:00:00Z",
        }

        decision = evaluate_issue_scope(
            issue=issue,
            scope_defaults={
                "labels": {"allow": ["bug"]},
                "assignees": {"allow": ["alice"]},
                "priority": {"allow": ["priority:high"], "order": ["priority:high", "priority:low"]},
                "freshness": {"max_age_days": 7, "max_idle_days": 7},
            },
        )

        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["matched"]["priority_rank"], 0)

    def test_scope_evaluation_blocks_stale_issue(self) -> None:
        issue = {
            "number": 93,
            "updatedAt": "2026-04-01T10:00:00Z",
        }

        decision = evaluate_issue_scope(
            issue=issue,
            scope_defaults={"freshness": {"max_idle_days": 1}},
        )

        self.assertFalse(decision["eligible"])
        self.assertIn("too stale", decision["reason"])

    def test_sort_autonomous_issues_prefers_priority_then_freshness(self) -> None:
        issues = [
            {"number": 1, "labels": [{"name": "priority:low"}], "updatedAt": "2026-04-26T09:00:00Z"},
            {"number": 2, "labels": [{"name": "priority:high"}], "updatedAt": "2026-04-25T09:00:00Z"},
            {"number": 3, "labels": [{"name": "priority:high"}], "updatedAt": "2026-04-27T09:00:00Z"},
        ]

        ordered = sort_autonomous_issues(
            issues=issues,
            scope_defaults={"priority": {"order": ["priority:high", "priority:low"]}},
        )

        self.assertEqual([issue["number"] for issue in ordered], [3, 2, 1])

    def test_claim_comment_round_trip_and_activity_check(self) -> None:
        claim = build_orchestration_claim(issue_number=93, run_id="run-1", status="claimed", ttl_seconds=60)

        payload, error = parse_orchestration_claim_comment_body(format_orchestration_claim_comment(claim))

        self.assertIsNone(error)
        self.assertEqual(payload["issue"], 93)
        self.assertTrue(is_active_orchestration_claim(payload, run_id="other-run"))
        self.assertFalse(is_active_orchestration_claim(payload, run_id="run-1"))

    def test_autonomous_session_filters_previously_processed_issue_numbers(self) -> None:
        issues = [
            {"number": 153, "title": "First"},
            {"number": 152, "title": "Second"},
        ]
        session_state = {"processed_issues": {}}

        mark_autonomous_session_issue_processed(session_state, issue_number=153, status="ready-for-review")

        filtered, skipped = filter_autonomous_issues_for_single_pass(issues, session_state)

        self.assertEqual([issue["number"] for issue in filtered], [152])
        self.assertEqual(skipped, [153])
        self.assertEqual(autonomous_session_processed_issue_numbers(session_state), {153})

    def test_autonomous_session_persists_processed_issue_between_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = os.path.join(temp_dir, "daemon-session.json")

            first_cycle_state = load_autonomous_session_state(session_path)
            mark_autonomous_session_issue_processed(
                first_cycle_state,
                issue_number=153,
                status="ready-for-review",
            )
            save_autonomous_session_state(session_path, first_cycle_state)

            second_cycle_state = load_autonomous_session_state(session_path)
            filtered, skipped = filter_autonomous_issues_for_single_pass(
                issues=[
                    {"number": 153, "title": "First"},
                    {"number": 152, "title": "Second"},
                ],
                session_state=second_cycle_state,
            )

        self.assertEqual([issue["number"] for issue in filtered], [152])
        self.assertEqual(skipped, [153])

    def test_main_autonomous_batch_resumes_linked_pr_instead_of_skipping(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            tracker="github",
            issue=None,
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
            skip_if_pr_exists=True,
            skip_if_branch_exists=True,
            force_reprocess=False,
            sync_reused_branch=True,
            sync_strategy="rebase",
            base_branch="default",
            decompose="never",
            create_child_issues=False,
            dir=".",
            local_config="local-config.json",
            project_config="project-config.json",
            dry_run=True,
            post_pr_summary=False,
            allow_pr_branch_switch=False,
            isolate_worktree=False,
            pr_followup_branch_prefix=None,
            track_tokens=False,
            token_budget=None,
            preset=None,
            max_attempts=2,
            autonomous=True,
        )

        issue = {
            "number": 93,
            "title": "Autonomous daemon",
            "body": "non-empty",
            "url": "https://github.com/owner/repo/issues/93",
            "labels": [{"name": "bug"}],
            "assignees": [],
            "createdAt": "2026-04-26T10:00:00Z",
            "updatedAt": "2026-04-27T10:00:00Z",
        }
        linked_pr = {
            "number": 140,
            "url": "https://github.com/owner/repo/pull/140",
            "headRefName": "issue-fix/93-autonomous-daemon",
            "baseRefName": "main",
            "reviews": [],
            "author": {"login": "dev"},
            "mergeStateStatus": "CLEAN",
        }
        pr_state_comment = {
            "id": 2,
            "created_at": "2026-04-27T12:00:00Z",
            "body": '<!-- orchestration-state:v1 -->\n```json\n{"status":"waiting-for-ci","attempt":1}\n```',
        }

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.load_project_config", return_value={}),
            patch("scripts.run_github_issues_to_opencode.project_scope_defaults", return_value={}),
            patch("scripts.run_github_issues_to_opencode.configured_workflow_commands", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.fetch_issues", return_value=[issue]),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=linked_pr),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", side_effect=[[], [pr_state_comment]]),
            patch("scripts.run_github_issues_to_opencode.fetch_pull_request", return_value=linked_pr),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_review_threads", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
            patch(
                "scripts.run_github_issues_to_opencode.read_pr_ci_status_for_pull_request",
                return_value={"overall": "pending", "pending_checks": [{"name": "ci"}], "checks": []},
            ),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        prepare_issue_branch_mock.assert_not_called()
        run_agent_mock.assert_not_called()
        self.assertIn("Auto-switch to PR-review mode", stdout_mock.getvalue())
        self.assertIn("keeping waiting-for-ci state", stdout_mock.getvalue())


if __name__ == "__main__":
    unittest.main()
