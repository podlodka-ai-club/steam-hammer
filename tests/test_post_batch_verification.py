import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    TRACKER_GITHUB,
    format_autonomous_session_status_summary,
    run_post_batch_verification,
    update_autonomous_session_checkpoint,
)


class PostBatchVerificationTests(unittest.TestCase):
    def test_run_post_batch_verification_passes(self) -> None:
        with (
            patch(
                "scripts.run_github_issues_to_opencode.detect_post_batch_verification_commands",
                return_value=[("python-tests", "python3 -m unittest discover -s tests -q"), ("go-test", "go test ./...")],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.run_check_command",
                side_effect=[
                    (True, "ok", "", 0),
                    (True, "ok", "", 0),
                ],
            ),
        ):
            verification = run_post_batch_verification(
                repo="owner/repo",
                tracker=TRACKER_GITHUB,
                cwd="/repo",
                dry_run=False,
                create_followup_issue=False,
            )

        self.assertEqual(verification["status"], "passed")
        self.assertEqual(verification["next_action"], "none")
        self.assertEqual(len(verification["commands"]), 2)
        self.assertEqual(verification["follow_up_issue"]["status"], "not-needed")

    def test_run_post_batch_verification_recommends_follow_up_issue_on_failure(self) -> None:
        with (
            patch(
                "scripts.run_github_issues_to_opencode.detect_post_batch_verification_commands",
                return_value=[("python-tests", "python3 -m unittest discover -s tests -q"), ("go-test", "go test ./...")],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.run_check_command",
                side_effect=[
                    (True, "ok", "", 0),
                    (False, "", "go test failed", 1),
                ],
            ),
        ):
            verification = run_post_batch_verification(
                repo="owner/repo",
                tracker=TRACKER_GITHUB,
                cwd="/repo",
                dry_run=False,
                create_followup_issue=False,
                touched_prs=["https://github.com/owner/repo/pull/12"],
            )

        self.assertEqual(verification["status"], "failed")
        self.assertEqual(verification["next_action"], "create_follow_up_issue_and_fix_regression")
        self.assertEqual(verification["follow_up_issue"]["status"], "recommended")
        self.assertIn("Post-batch verification failed: go-test", verification["follow_up_issue"]["title"])

    def test_run_post_batch_verification_creates_follow_up_issue_when_requested(self) -> None:
        with (
            patch(
                "scripts.run_github_issues_to_opencode.detect_post_batch_verification_commands",
                return_value=[("go-test", "go test ./...")],
            ),
            patch(
                "scripts.run_github_issues_to_opencode.run_check_command",
                return_value=(False, "", "go test failed", 1),
            ),
            patch(
                "scripts.run_github_issues_to_opencode.create_post_batch_follow_up_issue",
                return_value={
                    "status": "created",
                    "title": "Post-batch verification failed: go-test",
                    "issue_number": 164,
                    "issue_url": "https://github.com/owner/repo/issues/164",
                },
            ) as create_issue_mock,
        ):
            verification = run_post_batch_verification(
                repo="owner/repo",
                tracker=TRACKER_GITHUB,
                cwd="/repo",
                dry_run=False,
                create_followup_issue=True,
            )

        self.assertEqual(verification["status"], "failed")
        self.assertEqual(verification["next_action"], "fix_regression_from_follow_up_issue")
        self.assertEqual(verification["follow_up_issue"]["status"], "created")
        create_issue_mock.assert_called_once()

    def test_autonomous_session_summary_includes_verification_status(self) -> None:
        state = {}
        update_autonomous_session_checkpoint(
            state,
            run_id="run-1",
            phase="completed",
            batch_index=2,
            total_batches=2,
            counts={"processed": 2, "failures": 0},
            done=["Autonomous batch loop finished across 2 issue(s)"],
            current="Idle between autonomous runs",
            next_items=[],
            issue_pr_actions=["Touched 1 PR(s)", "Recommended a verification follow-up issue"],
            in_progress=[],
            blockers=["failed (1/2 passed; failed: go-test)"],
            next_checkpoint="when the next autonomous invocation starts",
            verification={
                "status": "failed",
                "summary": "failed (1/2 passed; failed: go-test)",
                "follow_up_issue": {"status": "recommended"},
            },
        )

        summary = format_autonomous_session_status_summary(state)

        self.assertIn("Verification: failed (1/2 passed; failed: go-test); follow-up=recommended", summary)

    def test_autonomous_session_summary_formats_non_numeric_follow_up_issue_refs(self) -> None:
        state = {}
        update_autonomous_session_checkpoint(
            state,
            run_id="run-1",
            phase="completed",
            batch_index=1,
            total_batches=1,
            counts={"processed": 1, "failures": 1},
            done=["Autonomous batch loop finished across 1 issue(s)"],
            current="Idle between autonomous runs",
            next_items=[],
            issue_pr_actions=[],
            in_progress=[],
            blockers=["failed (1/1 passed; failed: verify)"],
            next_checkpoint="when the next autonomous invocation starts",
            verification={
                "status": "failed",
                "summary": "failed (1/1 passed; failed: verify)",
                "follow_up_issue": {"status": "created", "issue_number": "PROJ-164"},
            },
        )

        summary = format_autonomous_session_status_summary(state)

        self.assertIn("Verification: failed (1/1 passed; failed: verify); follow-up issue PROJ-164 created", summary)


if __name__ == "__main__":
    unittest.main()
