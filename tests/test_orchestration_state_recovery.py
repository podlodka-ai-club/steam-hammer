import argparse
import io
import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    ORCHESTRATION_STATE_MARKER,
    TokenBudgetExceededError,
    build_decomposition_rollup_from_plan_payload,
    build_decomposition_rollup_from_recovered_state,
    build_orchestration_state,
    format_recovered_state_context,
    format_decomposition_rollup_context,
    format_orchestration_state_comment,
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

    def test_formatted_state_comment_round_trips_through_current_recovery_parser(self) -> None:
        state = build_orchestration_state(
            status="in-progress",
            task_type="issue",
            issue_number=74,
            pr_number=None,
            branch="issue-fix/74-state-v1",
            base_branch="main",
            runner="opencode",
            agent="build",
            model=None,
            attempt=1,
            stage="agent_run",
            next_action="wait_for_agent_result",
            error=None,
        )

        body = format_orchestration_state_comment(state)
        payload, error = parse_orchestration_state_comment_body(body)

        self.assertIsNone(error)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertIn(ORCHESTRATION_STATE_MARKER, body)
        self.assertEqual(payload["status"], "in-progress")
        self.assertEqual(payload["task_type"], "issue")
        self.assertEqual(payload["issue"], 74)
        self.assertEqual(payload["branch"], "issue-fix/74-state-v1")

    def test_formatted_state_comment_keeps_stats_payload(self) -> None:
        state = build_orchestration_state(
            status="ready-for-review",
            task_type="pr",
            issue_number=None,
            pr_number=102,
            branch="pr-review/102",
            base_branch="main",
            runner="opencode",
            agent="build",
            model=None,
            attempt=1,
            stage="agent_run",
            next_action="wait_for_agent_result",
            error=None,
            stats={"elapsed_seconds": 125, "elapsed": "2m 5s", "tokens_in": 1000},
        )

        body = format_orchestration_state_comment(state)
        payload, error = parse_orchestration_state_comment_body(body)

        self.assertIsNone(error)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertIn("stats", payload)
        self.assertEqual(payload["stats"].get("elapsed_seconds"), 125)
        self.assertEqual(payload["stats"].get("tokens_in"), 1000)

    def test_formatted_state_comment_keeps_required_file_validation(self) -> None:
        state = build_orchestration_state(
            status="blocked",
            task_type="pr",
            issue_number=99,
            pr_number=201,
            branch="pr-review/201",
            base_branch="main",
            runner="opencode",
            agent="build",
            model=None,
            attempt=1,
            stage="ci_checks",
            next_action="update_pr_with_required_files",
            error="Missing required file evidence: docs/README.md",
            required_file_validation={
                "status": "blocked",
                "required_file_count": 2,
                "required_files": ["src/main.py", "docs/README.md"],
                "matched_files": ["src/main.py"],
                "missing_files": ["docs/README.md"],
                "changed_file_count": 1,
            },
        )

        body = format_orchestration_state_comment(state)
        payload, error = parse_orchestration_state_comment_body(body)

        self.assertIsNone(error)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertIn("required_file_validation", payload)
        self.assertEqual(payload["required_file_validation"].get("status"), "blocked")
        self.assertEqual(payload["required_file_validation"].get("missing_files"), ["docs/README.md"])

    def test_formatted_state_with_decomposition_round_trips(self) -> None:
        decomposition = build_decomposition_rollup_from_plan_payload(
            {
                "parent_issue": 74,
                "proposed_children": [
                    {
                        "order": 1,
                        "title": "Child task",
                        "status": "in-progress",
                        "issue": 75,
                    }
                ],
                "blockers": ["external"],
            }
        )
        state = build_orchestration_state(
            status="waiting-for-author",
            task_type="issue",
            issue_number=74,
            pr_number=None,
            branch="issue-fix/74-state-v1",
            base_branch="main",
            runner="opencode",
            agent="build",
            model=None,
            attempt=1,
            stage="review_feedback",
            next_action="await_new_review_comments",
            error="waiting on subtasks",
            decomposition=decomposition,
        )

        body = format_orchestration_state_comment(state)
        payload, error = parse_orchestration_state_comment_body(body)

        self.assertIsNone(error)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertIn("decomposition", payload)
        self.assertEqual(payload["decomposition"]["counts"]["in-progress"], 1)
        self.assertIn("decomposition(", format_recovered_state_context(payload))

    def test_recovered_state_context_renders_rollup_with_next_child_and_blockers(self) -> None:
        recovered_payload = {
            "status": "waiting-for-author",
            "parent_issue": 210,
            "branch": "issue-fix/210",
            "task_type": "issue",
            "stage": "decomposition_plan",
            "decomposition": {
                "parent_issue": 210,
                "proposed_children": [
                    {"order": 1, "title": "First", "status": "done", "issue_number": 301},
                    {"order": 2, "title": "Second", "status": "blocked", "issue_number": 302},
                    {"order": 3, "title": "Third", "status": "created"},
                ],
                "blockers": ["external dependency", "qa review"],
            },
        }
        recovered_state = {
            "status": "waiting-for-author",
            "payload": recovered_payload,
            "created_at": "2026-04-26T12:00:00Z",
        }

        decomposition = build_decomposition_rollup_from_recovered_state(
            recovered_state=recovered_state,
            parent_issue=210,
        )

        self.assertIsNotNone(decomposition)
        assert decomposition is not None
        self.assertEqual(decomposition["counts"]["blocked"], 1)
        self.assertEqual(decomposition["next_child"]["order"], 3)
        summary = format_decomposition_rollup_context(decomposition)
        self.assertIn("decomposition(", summary)
        self.assertIn("parent=#210", summary)
        self.assertIn("next=3:Third", summary)
        self.assertIn("blockers=external dependency, qa review", summary)

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
            isolate_worktree=True,
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
            isolate_worktree=True,
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
        self.assertIn("Skipping issue #45: recovered orchestration state is waiting-for-author", output)

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
            isolate_worktree=True,
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

    def test_main_parent_decomposition_executes_selected_child_issue(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=105,
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
            decompose="auto",
            create_child_issues=False,
            dir=".",
            local_config="local-config.json",
            project_config="project-config.json",
            dry_run=True,
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
            allow_pr_branch_switch=False,
            isolate_worktree=True,
        )
        parent_issue = {
            "number": 105,
            "title": "Decomposition parent",
            "body": "Parent tracker body",
            "url": "https://example/issues/105",
            "state": "open",
        }
        child_issue = {
            "number": 201,
            "title": "Child implementation",
            "body": "Implement child step",
            "url": "https://example/issues/201",
            "state": "open",
        }
        parent_plan_comment = {
            "created_at": "2026-04-27T12:00:00Z",
            "html_url": "https://example/issues/105#issuecomment-plan",
            "body": (
                "<!-- orchestration-decomposition:v1 -->\n"
                "```json\n"
                '{"status":"children_created","parent_issue":105,'
                '"proposed_children":[{"order":1,"title":"Child implementation","depends_on":[],"status":"created","issue_number":201}],'
                '"created_children":[{"order":1,"title":"Child implementation","issue_number":201,"issue_url":"https://example/issues/201","status":"created"}]}'
                "\n```"
            ),
        }
        rollup = build_decomposition_rollup_from_plan_payload(
            {
                "status": "children_created",
                "parent_issue": 105,
                "proposed_children": [
                    {"order": 1, "title": "Child implementation", "depends_on": [], "status": "created", "issue_number": 201}
                ],
                "created_children": [
                    {"order": 1, "title": "Child implementation", "issue_number": 201, "issue_url": "https://example/issues/201", "status": "created"}
                ],
            }
        )

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.load_project_config", return_value={}),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", side_effect=lambda repo, number: parent_issue if number == 105 else child_issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue_comments",
                side_effect=lambda repo, issue_number: [parent_plan_comment] if issue_number == 105 else [],
            ),
            patch("scripts.run_github_issues_to_opencode.refresh_decomposition_plan_payload_from_child_states", side_effect=lambda repo, plan_payload: plan_payload),
            patch("scripts.run_github_issues_to_opencode.post_parent_decomposition_rollup_update", return_value=({}, rollup)) as parent_update_mock,
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0) as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.commit_changes"),
            patch("scripts.run_github_issues_to_opencode.push_branch"),
            patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("created", "https://example/pr/201")),
            patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_agent_mock.call_args.kwargs["issue"]["number"], 201)
        self.assertTrue(parent_update_mock.called)
        self.assertIn("Executing decomposition child issue #201 for parent #105", stdout_mock.getvalue())

    def test_main_northstar_epic_issue_posts_planning_comment(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=99,
            pr=None,
            from_review_comments=False,
            state="open",
            limit=10,
            runner="opencode",
            agent="build",
            model=None,
            agent_timeout_seconds=900,
            agent_idle_timeout_seconds=None,
            token_budget=20000,
            opencode_auto_approve=False,
            branch_prefix="issue-fix",
            include_empty=False,
            stop_on_error=True,
            fail_on_existing=False,
            force_issue_flow=False,
            skip_if_pr_exists=True,
            skip_if_branch_exists=True,
            force_reprocess=False,
            sync_reused_branch=True,
            sync_strategy="rebase",
            base_branch="default",
            decompose="auto",
            create_child_issues=False,
            track_tokens=False,
            dir=".",
            local_config="local-config.json",
            project_config="project-config.json",
            dry_run=True,
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
            isolate_worktree=True,
        )

        epic_issue = {
            "number": 99,
            "title": "Epic: Northstar MVP rollout",
            "body": "\n".join(
                [
                    "## Goal",
                    "Deliver one production-ready northstar decomposition flow.",
                    "## Scope",
                    "- Add real-state markers for plan and execution",
                    "- Execute decomposition children with dependency ordering",
                    "- Track blockers and roll up child statuses",
                    "- Resume from child completion state",
                    "- Post transparent execution notes",
                    "- Keep PR validation and cleanup stable",
                ]
            ),
            "url": "https://example/issues/99",
            "state": "open",
        }

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.load_project_config", return_value={}),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=epic_issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.post_decomposition_plan_comment") as plan_comment_mock,
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment") as state_comment_mock,
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        run_agent_mock.assert_not_called()
        plan_comment_mock.assert_called_once()
        self.assertIn("needs decomposition; posted planning-only plan", stdout_mock.getvalue())

        state_payload = state_comment_mock.call_args.kwargs["state"]
        self.assertEqual(state_payload["status"], "waiting-for-author")
        self.assertEqual(state_payload["stage"], "decomposition_plan")
        self.assertIn("approve_plan_or_rerun_with_decompose_never", state_payload["next_action"])

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
            isolate_worktree=True,
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
            "headRefName": "issue-fix/12-pr",
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

    def test_single_issue_with_linked_pr_actionable_feedback_runs_pr_review_mode(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=52,
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
            dir=".",
            local_config="local-config.json",
            dry_run=True,
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
        )

        issue = {
            "number": 52,
            "title": "Prevent duplicate processing",
            "body": "Implement duplicate guards",
            "url": "https://example/issues/52",
        }
        linked_pr = {
            "number": 120,
            "url": "https://example/pull/120",
            "headRefName": "issue-fix/52-prevent-duplicate-processing",
            "baseRefName": "main",
        }
        pull_request = {
            "number": 120,
            "title": "Fix duplicate processing",
            "body": "PR body",
            "url": "https://example/pull/120",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "headRefOid": "abc123",
            "reviews": [],
            "author": {"login": "pr-owner"},
        }

        issue_state_comments = [
            {
                "id": 10,
                "created_at": "2026-04-26T12:00:00Z",
                "html_url": "https://example/issues/52#issuecomment-10",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"ready-for-review"}\n'
                    "```"
                ),
            }
        ]
        pr_state_comments = [
            {
                "id": 11,
                "created_at": "2026-04-26T13:00:00Z",
                "html_url": "https://example/pull/120#issuecomment-11",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"ready-for-review"}\n'
                    "```"
                ),
            }
        ]

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=linked_pr),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue_comments",
                side_effect=[issue_state_comments, pr_state_comments],
            ),
            patch("scripts.run_github_issues_to_opencode.fetch_pull_request", return_value=pull_request),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_review_threads", return_value=[]),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments",
                return_value=[
                    {
                        "author": "reviewer",
                        "body": "Please adjust skip logic for waiting-for-ci",
                        "url": "https://example/pull/120#issuecomment-20",
                    }
                ],
            ),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="reused"),
            patch("scripts.run_github_issues_to_opencode.sync_reused_branch_with_base", return_value=False),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0) as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("reused", "")),
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertTrue(run_agent_mock.called)
        self.assertIn("Selected mode: pr-review", stdout_mock.getvalue())

    def test_single_issue_waiting_for_ci_without_actionable_feedback_skips_cleanly(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=52,
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
            dir=".",
            local_config="local-config.json",
            dry_run=True,
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
        )

        issue = {
            "number": 52,
            "title": "Prevent duplicate processing",
            "body": "Implement duplicate guards",
            "url": "https://example/issues/52",
        }
        linked_pr = {
            "number": 120,
            "url": "https://example/pull/120",
            "headRefName": "issue-fix/52-prevent-duplicate-processing",
            "baseRefName": "main",
        }
        pull_request = {
            "number": 120,
            "title": "Fix duplicate processing",
            "body": "PR body",
            "url": "https://example/pull/120",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "reviews": [],
            "author": {"login": "pr-owner"},
        }

        issue_state_comments = []
        pr_state_comments = [
            {
                "id": 21,
                "created_at": "2026-04-26T14:00:00Z",
                "html_url": "https://example/pull/120#issuecomment-21",
                "body": (
                    "<!-- orchestration-state:v1 -->\n"
                    "```json\n"
                    '{"status":"waiting-for-ci"}\n'
                    "```"
                ),
            }
        ]

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=linked_pr),
            patch(
                "scripts.run_github_issues_to_opencode.fetch_issue_comments",
                side_effect=[issue_state_comments, pr_state_comments],
            ),
            patch("scripts.run_github_issues_to_opencode.fetch_pull_request", return_value=pull_request),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_review_threads", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.fetch_pr_conversation_comments", return_value=[]),
            patch(
                "scripts.run_github_issues_to_opencode.read_pr_ci_status_for_pull_request",
                return_value={
                    "head_sha": "abc123",
                    "overall": "pending",
                    "checks": [{"name": "ci/test", "state": "pending", "url": "https://example/checks/1"}],
                    "pending_checks": [{"name": "ci/test"}],
                    "failing_checks": [],
                },
            ),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch") as prepare_issue_branch_mock,
            patch("scripts.run_github_issues_to_opencode.run_agent") as run_agent_mock,
            patch(
                "scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"
            ) as state_post_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        prepare_issue_branch_mock.assert_not_called()
        run_agent_mock.assert_not_called()
        state_post_mock.assert_called_once()
        posted_state = state_post_mock.call_args.kwargs["state"]
        self.assertEqual(posted_state["status"], "waiting-for-ci")
        self.assertEqual(posted_state["stage"], "ci_checks")
        self.assertIn("CI checks are still pending", stdout_mock.getvalue())

    def test_main_issue_flow_posts_blocked_state_when_token_budget_exceeded(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            issue=91,
            pr=None,
            from_review_comments=False,
            state="open",
            limit=10,
            runner="opencode",
            agent="build",
            model=None,
            agent_timeout_seconds=900,
            agent_idle_timeout_seconds=None,
            token_budget=20000,
            opencode_auto_approve=False,
            branch_prefix="issue-fix",
            include_empty=False,
            stop_on_error=True,
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
            track_tokens=False,
            dir=".",
            local_config="local-config.json",
            dry_run=True,
            pr_followup_branch_prefix=None,
            post_pr_summary=False,
            isolate_worktree=True,
        )
        issue = {
            "number": 91,
            "title": "Add token budget limit",
            "body": "Stop runaway runs",
            "url": "https://example/issues/91",
        }

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
            patch(
                "scripts.run_github_issues_to_opencode.run_agent",
                side_effect=TokenBudgetExceededError(budget=20000, reached=21400, item_label="issue #91"),
            ),
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment") as state_post_mock,
            patch("scripts.run_github_issues_to_opencode.safe_report_issue_automation_failure") as failure_report_mock,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertGreaterEqual(state_post_mock.call_count, 2)
        posted_state = state_post_mock.call_args.kwargs["state"]
        self.assertEqual(posted_state["status"], "blocked")
        self.assertEqual(posted_state["stage"], "token_budget")
        self.assertEqual(posted_state["next_action"], "raise_token_budget_or_split_issue")
        self.assertIn("reached ~21 400", posted_state["error"])
        failure_report_mock.assert_called_once()
        self.assertEqual(failure_report_mock.call_args.kwargs["stage"], "token_budget")


if __name__ == "__main__":
    unittest.main()
