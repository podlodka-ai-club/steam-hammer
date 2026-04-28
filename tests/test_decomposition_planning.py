import json
import os
import tempfile
import unittest

from scripts.run_github_issues_to_opencode import (
    BUILTIN_DEFAULTS,
    DECOMPOSITION_PLAN_MARKER,
    _build_child_issue_body,
    _classify_decomposition_child_execution_status,
    _decomposition_plan_has_missing_children,
    _normalize_created_children,
    assess_issue_decomposition_need,
    attach_decomposition_resume_context,
    build_decomposition_plan_payload,
    build_decomposition_child_execution_note,
    build_decomposition_rollup_from_plan_payload,
    format_decomposition_rollup_context,
    format_decomposition_plan_comment,
    is_decomposition_plan_approved,
    merge_created_children_into_plan_payload,
    parse_args,
    parse_decomposition_plan_comment_body,
    refresh_decomposition_plan_payload_from_child_states,
    select_latest_parseable_decomposition_plan,
    should_issue_decompose,
)


class DecompositionPlanningTests(unittest.TestCase):
    def test_small_issue_does_not_need_decomposition(self) -> None:
        issue = {
            "number": 1,
            "title": "Fix typo",
            "body": "Correct the README spelling mistake.",
        }

        assessment = assess_issue_decomposition_need(issue)

        self.assertFalse(assessment["needs_decomposition"])
        self.assertEqual(assessment["reasons"], [])

    def test_large_epic_issue_needs_decomposition(self) -> None:
        issue = {
            "number": 99,
            "title": "Epic: Task decomposition and linked subtask management",
            "body": "\n".join(
                [
                    "## Goal",
                    "Allow the orchestrator to split large work into smaller linked tracker tasks.",
                    "## Scope",
                    "- Add a planning/decomposition phase.",
                    "- Create child/linked issues.",
                    "- Record dependencies.",
                    "- Execute subtasks in dependency order.",
                    "- Roll progress up to the parent task.",
                ]
            ),
        }

        assessment = assess_issue_decomposition_need(issue)

        self.assertTrue(assessment["needs_decomposition"])
        self.assertIn("explicit_epic_title", assessment["reasons"])
        self.assertIn("many_implementation_areas", assessment["reasons"])

    def test_child_issue_keywords_do_not_trigger_decomposition_on_their_own(self) -> None:
        issue = {
            "number": 103,
            "title": "Refine decomposition auto-gate",
            "body": "\n".join(
                [
                    "Parent epic: #99",
                    "## Goal",
                    "Avoid triggering decomposition for child implementation tasks.",
                    "## Scope",
                    "- Tighten the auto gate for child tasks.",
                    "- Keep --decompose always working.",
                    "## Success criteria",
                    "- Child tasks under the decomposition epic proceed normally.",
                    "- Manual decomposition still works.",
                ]
            ),
        }

        assessment = assess_issue_decomposition_need(issue)

        self.assertFalse(assessment["needs_decomposition"])
        self.assertEqual(assessment["reasons"], [])
        self.assertIn("decomposition", assessment["matched_keywords"])

    def test_many_implementation_areas_do_not_trigger_decomposition_on_their_own(self) -> None:
        issue = {
            "number": 120,
            "title": "Roll out orchestration recovery improvements",
            "body": "\n".join(
                [
                    "## Scope",
                    "- Harden state recovery across reruns.",
                    "- Add guardrails for malformed state markers.",
                    "- Improve status reporting in CLI output.",
                    "- Capture recovery warnings in tracker comments.",
                    "- Expand regression coverage for resumed runs.",
                ]
            ),
        }

        assessment = assess_issue_decomposition_need(issue)

        self.assertFalse(assessment["needs_decomposition"])
        self.assertEqual(assessment["reasons"], [])
        self.assertIn("many_implementation_areas", assessment["soft_hints"])

    def test_issue_90_shape_does_not_auto_decompose(self) -> None:
        issue = {
            "number": 90,
            "title": "Add per-run statistics: elapsed time and token/cost usage",
            "body": "\n".join(
                [
                    "## Feature Request",
                    "After each issue or PR-review run, the orchestrator should print (and optionally include in the state comment) how long the agent took and how many tokens / how much it cost.",
                    "## Why",
                    "Right now there is no feedback on run cost or speed. Over time, teams want to know:",
                    "- Which issues are expensive to fix automatically",
                    "- Whether token usage is trending up as prompts grow",
                    "- How long to expect a run to take for planning purposes",
                    "## Proposed behaviour",
                    "After a successful (or failed) agent run, print a summary line and include the same data in the orchestration state comment.",
                    "## Implementation notes",
                    "Elapsed time is already tracked internally and token tracking should not block the issue.",
                ]
            ),
        }

        assessment = assess_issue_decomposition_need(issue)

        self.assertFalse(assessment["needs_decomposition"])
        self.assertTrue(assessment["concrete_implementation"])

    def test_issue_105_shape_does_not_auto_decompose(self) -> None:
        issue = {
            "number": 105,
            "title": "Decomposition MVP: execute child tasks in dependency order",
            "body": "\n".join(
                [
                    "Parent epic: #99",
                    "## Goal",
                    "Allow the orchestrator to advance decomposed work one child task at a time in a safe order.",
                    "## Scope",
                    "- Select the next unblocked child issue from parent state.",
                    "- Respect dependency order.",
                    "- Run the existing issue flow for the selected child.",
                    "- Update parent roll-up after each child result.",
                    "## Success criteria",
                    "- A decomposed parent task can be progressed through child issues without losing ordering or context.",
                ]
            ),
        }

        assessment = assess_issue_decomposition_need(issue)

        self.assertFalse(assessment["needs_decomposition"])
        self.assertEqual(assessment["reasons"], [])

    def test_decompose_always_overrides_auto_assessment(self) -> None:
        issue = {
            "number": 90,
            "title": "Add per-run statistics: elapsed time and token/cost usage",
            "body": "Implement elapsed time reporting after each run.",
        }

        should_plan, assessment = should_issue_decompose(issue, decompose_mode="always")

        self.assertTrue(should_plan)
        self.assertFalse(assessment["needs_decomposition"])

    def test_plan_comment_round_trips_machine_payload(self) -> None:
        issue = {
            "number": 99,
            "title": "Epic: Decompose work",
            "body": "- First slice\n- Second slice",
        }
        assessment = {
            "reasons": ["large_scope_keywords"],
            "matched_keywords": ["epic"],
        }

        payload = build_decomposition_plan_payload(issue=issue, assessment=assessment)
        body = format_decomposition_plan_comment(payload)
        parsed, error = parse_decomposition_plan_comment_body(body)

        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["status"], "proposed")
        self.assertEqual(parsed["parent_issue"], 99)
        self.assertIn(DECOMPOSITION_PLAN_MARKER, body)

    def test_plan_comment_includes_resume_execution_context(self) -> None:
        issue = {
            "number": 99,
            "title": "Epic: Decompose work",
            "body": "- First slice\n- Second slice",
        }
        payload = attach_decomposition_resume_context(
            plan_payload=build_decomposition_plan_payload(issue=issue, assessment={}),
            parent_issue=issue,
            parent_branch="issue-fix/99-decompose-work",
            base_branch="main",
            next_action="execute_next_child",
            selected_child={"order": 2, "title": "Second slice", "issue_number": 120},
        )

        body = format_decomposition_plan_comment(payload)

        self.assertIn("Execution context:", body)
        self.assertIn("branch: `issue-fix/99-decompose-work`", body)
        self.assertIn("base branch: `main`", body)
        self.assertIn("selected child: `2: Second slice (#120)`", body)

    def test_plan_payload_uses_scope_bullets_and_skips_success_criteria(self) -> None:
        issue = {
            "number": 106,
            "title": "Epic: Improve decomposition plan quality",
            "body": "\n".join(
                [
                    "## Scope",
                    "- Tighten the auto gate for child issues.",
                    "- Generate child tasks from implementation scope only.",
                    "## Success criteria",
                    "- Decomposition is validated on a real large task.",
                    "- Child tasks are completed and validated.",
                ]
            ),
        }
        assessment = {
            "reasons": ["explicit_epic_title"],
            "matched_keywords": ["epic", "decomposition"],
        }

        payload = build_decomposition_plan_payload(issue=issue, assessment=assessment)

        child_titles = [child["title"] for child in payload["proposed_children"]]
        self.assertEqual(
            child_titles,
            [
                "Tighten the auto gate for child issues",
                "Generate child tasks from implementation scope only",
            ],
        )
        self.assertNotIn(
            "Decomposition is validated on a real large task",
            child_titles,
        )
        self.assertEqual(
            payload["proposed_children"][0]["acceptance"],
            [
                "Required changes for 'Tighten the auto gate for child issues' are implemented.",
                "Relevant validation or follow-up checks for 'Tighten the auto gate for child issues' are recorded.",
            ],
        )

    def test_latest_decomposition_plan_is_selected(self) -> None:
        first = {
            "status": "proposed",
            "parent_issue": 99,
            "proposed_children": [],
        }
        second = {
            "status": "proposed",
            "parent_issue": 99,
            "proposed_children": [{"title": "Latest", "order": 1}],
        }
        comments = [
            {
                "created_at": "2026-01-01T00:00:00Z",
                "html_url": "https://example.test/old",
                "body": f"{DECOMPOSITION_PLAN_MARKER}\n```json\n{json.dumps(first)}\n```",
            },
            {
                "created_at": "2026-01-02T00:00:00Z",
                "html_url": "https://example.test/new",
                "body": f"{DECOMPOSITION_PLAN_MARKER}\n```json\n{json.dumps(second)}\n```",
            },
        ]

        latest, warnings = select_latest_parseable_decomposition_plan(
            comments=comments,
            source_label="issue #99",
        )

        self.assertEqual(warnings, [])
        self.assertIsNotNone(latest)
        self.assertEqual(latest["url"], "https://example.test/new")
        self.assertEqual(latest["payload"]["proposed_children"][0]["title"], "Latest")

    def test_decompose_cli_and_local_config_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            args = parse_args(["--dir", tmpdir])
            self.assertEqual(args.decompose, BUILTIN_DEFAULTS["decompose"])

            config_path = os.path.join(tmpdir, "local-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"decompose": "never"}, config_file)

            configured_args = parse_args(["--dir", tmpdir])
            cli_args = parse_args(["--dir", tmpdir, "--decompose", "always"])

        self.assertEqual(configured_args.decompose, "never")
        self.assertEqual(cli_args.decompose, "always")

    def test_approved_decomposition_plan_is_recognized(self) -> None:
        self.assertTrue(is_decomposition_plan_approved({"status": "approved"}))
        self.assertTrue(is_decomposition_plan_approved({"status": "execution_plan"}))
        self.assertFalse(is_decomposition_plan_approved({"status": "proposed"}))

    def test_missing_children_is_computed_from_created_children(self) -> None:
        payload = {
            "proposed_children": [
                {"title": "Parent first", "order": 1},
                {"title": "Parent second", "order": 2},
                {"title": "Parent third", "order": 3},
            ],
            "created_children": [
                {"title": "Parent first", "order": 1, "issue_number": 101, "issue_url": "https://x/101"},
            ],
        }

        missing = _decomposition_plan_has_missing_children(payload)

        self.assertEqual(len(missing), 2)
        self.assertEqual(missing[0]["order"], 2)
        self.assertEqual(missing[1]["order"], 3)

    def test_merge_created_children_is_idempotent(self) -> None:
        payload = {
            "proposed_children": [
                {"title": "Alpha", "order": 1},
                {"title": "Beta", "order": 2},
            ],
            "created_children": [
                {"title": "Alpha", "order": 1, "issue_number": 10, "issue_url": "https://example/10"}
            ],
        }
        merged = merge_created_children_into_plan_payload(payload, [
            {"title": "Beta", "order": 2, "issue_number": 20, "issue_url": "https://example/20", "created": True}
        ])

        created_children = _normalize_created_children(merged.get("created_children"))

        self.assertEqual(len(created_children), 2)
        self.assertEqual(created_children[0]["order"], 1)
        self.assertEqual(created_children[1]["order"], 2)
        self.assertEqual(merged["created_children"][1]["issue_number"], 20)

    def test_rollup_build_includes_counts_next_child_and_progress(self) -> None:
        payload = {
            "parent_issue": 150,
            "status": "children_created",
            "proposed_children": [
                {"order": 1, "title": "Collect context", "status": "done", "issue_number": 301},
                {"order": 2, "title": "Build plan", "issue_number": 302},
                {"order": 3, "title": "Validate", "status": "blocked"},
            ],
            "created_children": [
                {"order": 2, "issue_number": 302, "status": "created", "title": "Build plan"},
            ],
            "blockers": ["waiting on API token"],
        }
        rollup = build_decomposition_rollup_from_plan_payload(payload)

        self.assertEqual(rollup["parent_issue"], 150)
        self.assertEqual(rollup["counts"]["done"], 1)
        self.assertEqual(rollup["counts"]["created"], 1)
        self.assertEqual(rollup["counts"]["blocked"], 1)
        self.assertEqual(rollup["total_children"], 3)
        self.assertEqual(rollup["next_child"]["order"], 2)
        self.assertEqual(rollup["next_child"]["issue_number"], 302)
        self.assertEqual(rollup["progress"]["percent"], 33)

        summary = format_decomposition_rollup_context(rollup)
        self.assertIn("decomposition(parent=#150", summary)
        self.assertIn("next=2:Build plan", summary)
        self.assertIn("blockers=waiting on API token", summary)

    def test_rollup_selects_first_dependency_unblocked_child(self) -> None:
        payload = {
            "parent_issue": 151,
            "status": "children_created",
            "proposed_children": [
                {"order": 1, "title": "First", "status": "done"},
                {"order": 2, "title": "Second", "depends_on": [1], "status": "blocked", "issue_number": 402},
                {"order": 3, "title": "Third", "depends_on": [2], "status": "created", "issue_number": 403},
                {"order": 4, "title": "Fourth", "depends_on": [1], "status": "created", "issue_number": 404},
            ],
        }

        rollup = build_decomposition_rollup_from_plan_payload(payload)

        self.assertIsNotNone(rollup["next_child"])
        assert rollup["next_child"] is not None
        self.assertEqual(rollup["next_child"]["order"], 4)
        self.assertEqual(rollup["next_child"]["issue_number"], 404)

    def test_classify_decomposition_child_execution_status(self) -> None:
        status, blocker = _classify_decomposition_child_execution_status(
            child_issue={"state": "open"},
            recovered_state={"status": "waiting-for-author", "payload": {"error": "needs product answer"}},
        )

        self.assertEqual(status, "blocked")
        self.assertEqual(blocker, "needs product answer")

        done_status, done_blocker = _classify_decomposition_child_execution_status(
            child_issue={"state": "closed"},
            recovered_state=None,
        )

        self.assertEqual(done_status, "done")
        self.assertIsNone(done_blocker)

    def test_refresh_parent_payload_from_child_states_updates_rollup(self) -> None:
        payload = {
            "parent_issue": 160,
            "status": "children_created",
            "proposed_children": [
                {"order": 1, "title": "First", "depends_on": [], "status": "created"},
                {"order": 2, "title": "Second", "depends_on": [1], "status": "created"},
            ],
            "created_children": [
                {"order": 1, "title": "First", "issue_number": 501, "issue_url": "https://example/501"},
                {"order": 2, "title": "Second", "issue_number": 502, "issue_url": "https://example/502"},
            ],
        }

        child_issue_responses = {
            501: {"number": 501, "title": "First", "url": "https://example/501", "state": "closed"},
            502: {"number": 502, "title": "Second", "url": "https://example/502", "state": "open"},
        }
        child_comments = {
            501: [],
            502: [
                {
                    "created_at": "2026-04-27T12:00:00Z",
                    "html_url": "https://example/502#issuecomment-1",
                    "body": "<!-- orchestration-state:v1 -->\n```json\n{\"status\":\"waiting-for-author\",\"error\":\"awaiting QA\"}\n```",
                }
            ],
        }

        from unittest.mock import patch

        with (
            patch("scripts.run_github_issues_to_opencode.fetch_issue", side_effect=lambda repo, number: child_issue_responses[number]),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", side_effect=lambda repo, issue_number: child_comments[issue_number]),
        ):
            refreshed = refresh_decomposition_plan_payload_from_child_states(
                repo="owner/repo",
                plan_payload=payload,
            )

        created_children = _normalize_created_children(refreshed["created_children"])
        self.assertEqual(created_children[0]["status"], "done")
        self.assertEqual(created_children[1]["status"], "blocked")
        self.assertIn("step 2: awaiting QA", refreshed["blockers"])

        rollup = build_decomposition_rollup_from_plan_payload(refreshed)
        self.assertIsNone(rollup["next_child"])

    def test_child_execution_note_includes_parent_context(self) -> None:
        rollup = build_decomposition_rollup_from_plan_payload(
            {
                "parent_issue": 170,
                "proposed_children": [{"order": 1, "title": "Child", "status": "created", "issue_number": 601}],
                "created_children": [{"order": 1, "title": "Child", "status": "created", "issue_number": 601}],
            }
        )

        note = build_decomposition_child_execution_note(
            parent_issue={"number": 170, "title": "Parent tracker"},
            decomposition_rollup=rollup,
            selected_child={"order": 1, "title": "Child", "issue_number": 601},
        )

        self.assertIn("Parent issue: #170 - Parent tracker", note)
        self.assertIn("Selected child step: 1: Child", note)

    def test_child_issue_body_includes_dependency_branch_and_resume_instructions(self) -> None:
        body = _build_child_issue_body(
            parent_issue={"number": 170, "title": "Parent tracker"},
            child={
                "order": 2,
                "title": "Child",
                "depends_on": [1],
                "acceptance": ["Complete the child safely."],
            },
            created_dependencies={1: {"issue_number": 601, "issue_url": "https://example/issues/601"}},
            parent_branch="issue-fix/170-parent-tracker",
            base_branch="main",
        )

        self.assertIn("Depends on: [Step 1: #601](https://example/issues/601)", body)
        self.assertIn("Parent orchestration branch: `issue-fix/170-parent-tracker`", body)
        self.assertIn("Preferred resume path: rerun the orchestrator for parent issue #170", body)
        self.assertIn("Do not start this task until the listed dependencies are completed.", body)



if __name__ == "__main__":
    unittest.main()
