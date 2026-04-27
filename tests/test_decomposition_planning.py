import json
import os
import tempfile
import unittest

from scripts.run_github_issues_to_opencode import (
    BUILTIN_DEFAULTS,
    DECOMPOSITION_PLAN_MARKER,
    _decomposition_plan_has_missing_children,
    _normalize_created_children,
    assess_issue_decomposition_need,
    build_decomposition_plan_payload,
    format_decomposition_plan_comment,
    is_decomposition_plan_approved,
    merge_created_children_into_plan_payload,
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



if __name__ == "__main__":
    unittest.main()
