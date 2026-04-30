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

    def test_force_issue_flow_does_not_resume_waiting_for_author_state(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=45,
            linked_open_pr={"number": 144},
            force_issue_flow=True,
            recovered_state={"status": "waiting-for-author"},
        )

        self.assertEqual(mode, "skip")
        self.assertIn("waiting-for-author", reason)
        self.assertIn("explicitly resumed", reason)

    def test_ready_for_review_state_prefers_pr_review_when_open_pr_exists(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=45,
            linked_open_pr={"number": 144},
            force_issue_flow=False,
            recovered_state={"status": "ready-for-review"},
        )

        self.assertEqual(mode, "pr-review")
        self.assertIn("ready-for-review", reason)
        self.assertIn("#144", reason)

    def test_waiting_for_author_state_skips_unless_forced(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=45,
            linked_open_pr={"number": 144},
            force_issue_flow=False,
            recovered_state={"status": "waiting-for-author"},
        )

        self.assertEqual(mode, "skip")
        self.assertIn("waiting-for-author", reason)

    def test_blocked_state_skips_unless_forced(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=45,
            linked_open_pr={"number": 144},
            force_issue_flow=False,
            recovered_state={"status": "blocked"},
        )

        self.assertEqual(mode, "skip")
        self.assertIn("blocked", reason)

    def test_waiting_for_ci_state_prefers_pr_review_when_open_pr_exists(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=45,
            linked_open_pr={"number": 144},
            force_issue_flow=False,
            recovered_state={"status": "waiting-for-ci"},
        )

        self.assertEqual(mode, "pr-review")
        self.assertIn("waiting-for-ci", reason)

    def test_ready_to_merge_state_prefers_pr_review_when_open_pr_exists(self) -> None:
        mode, reason = choose_execution_mode(
            issue_number=45,
            linked_open_pr={"number": 144},
            force_issue_flow=False,
            recovered_state={"status": "ready-to-merge"},
        )

        self.assertEqual(mode, "pr-review")
        self.assertIn("ready-to-merge", reason)


if __name__ == "__main__":
    unittest.main()
