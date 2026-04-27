import json
import os
import tempfile
import unittest

from scripts.run_github_issues_to_opencode import (
    BUILTIN_DEFAULTS,
    DECOMPOSITION_PLAN_MARKER,
    build_decomposition_rollup_from_plan_payload,
    build_decomposition_rollup_from_recovered_state,
    assess_issue_decomposition_need,
    build_decomposition_plan_payload,
    format_decomposition_plan_comment,
    parse_args,
    parse_decomposition_plan_comment_body,
    select_latest_parseable_decomposition_plan,
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
        self.assertIn("large_scope_keywords", assessment["reasons"])

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

    def test_rollup_builder_from_plan_payload(self) -> None:
        payload = {
            "parent_issue": 77,
            "proposed_children": [
                {"order": "1", "title": "In-progress task", "status": "in-progress", "issue": "111", "pr": "321"},
                {"order": 2, "title": "Planned task", "status": "planned", "issue": 112},
                {"title": "Blocked task", "status": "blocked", "issue": "113", "blockers": ["dependency"]},
                {"title": "Done task", "status": "done", "issue": 114},
            ],
            "blockers": ["global-blocker"],
        }

        rollup = build_decomposition_rollup_from_plan_payload(payload)

        self.assertEqual(rollup["parent_issue"], 77)
        self.assertEqual(rollup["counts"], {"planned": 1, "created": 0, "in-progress": 1, "done": 1, "blocked": 1})
        self.assertEqual(rollup["children_by_status"]["blocked"][0]["issue"], 113)
        self.assertEqual(rollup["blockers"], ["global-blocker", "dependency"])
        next_target = rollup["next_target_task"]
        self.assertIsNotNone(next_target)
        assert next_target is not None
        self.assertEqual(next_target["order"], 1)
        self.assertEqual(next_target["status"], "in-progress")

    def test_rollup_builder_from_recovered_children_by_status(self) -> None:
        recovered_state = {
            "payload": {
                "decomposition": {
                    "children_by_status": {
                        "planned": [
                            {"order": 4, "title": "Planned next", "issue": 210, "status": "planned"},
                        ],
                        "created": [
                            {"order": 3, "title": "Created now", "issue": 209, "status": "created"},
                        ],
                        "in-progress": [
                            {"order": 2, "title": "Doing now", "issue": 208, "status": "in-progress"},
                        ],
                        "done": [
                            {"order": 1, "title": "Done earlier", "issue": 207, "status": "done"},
                        ],
                        "blocked": [
                            {"order": 5, "title": "Blocked", "issue": 211, "status": "blocked", "blockers": ["network"]},
                        ],
                    },
                    "blockers": ["overall"]
                }
            }
        }

        rollup = build_decomposition_rollup_from_recovered_state(recovered_state, parent_issue=77)

        self.assertIsNotNone(rollup)
        assert rollup is not None
        self.assertEqual(rollup["parent_issue"], 77)
        self.assertEqual(rollup["counts"], {"planned": 1, "created": 1, "in-progress": 1, "done": 1, "blocked": 1})
        self.assertEqual(rollup["next_target_task"], {
            "order": 2,
            "title": "Doing now",
            "status": "in-progress",
            "issue": 208,
            "pr": None,
        })
        self.assertEqual(rollup["blockers"], ["overall", "network"])

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


if __name__ == "__main__":
    unittest.main()
