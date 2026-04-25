import unittest

from scripts.run_github_issues_to_opencode import choose_execution_mode


class ModeSelectionTests(unittest.TestCase):
    def test_issue_flow_when_no_open_pr(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=31,
            linked_open_pr=None,
            force_issue_flow=False,
        )

        self.assertEqual(mode, "issue-flow")
        self.assertIn("no open PR linked", reason)

    def test_pr_review_when_open_pr_exists(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=31,
            linked_open_pr={"number": 120},
            force_issue_flow=False,
        )

        self.assertEqual(mode, "pr-review")
        self.assertIn("found linked open PR #120", reason)

    def test_force_issue_flow_overrides_auto_switch(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=31,
            linked_open_pr={"number": 120},
            force_issue_flow=True,
        )

        self.assertEqual(mode, "issue-flow")
        self.assertIn("--force-issue-flow", reason)


if __name__ == "__main__":
    unittest.main()
