import signal
import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    AGENT_FAILURE_LABEL_NAME,
    RECOMMENDED_OPENCODE_MODEL,
    classify_opencode_failure,
    describe_exit_code,
    ensure_agent_failure_label,
)


class ExitCodeDiagnosticsTests(unittest.TestCase):
    def test_describe_exit_code_reports_exit_status(self) -> None:
        self.assertEqual(describe_exit_code(0), "exit code 0")
        self.assertEqual(describe_exit_code(7), "exit code 7")

    def test_describe_exit_code_reports_signal_name(self) -> None:
        self.assertIn("SIGKILL", describe_exit_code(-signal.SIGKILL))

    def test_classify_opencode_sigkill_includes_recommendation(self) -> None:
        diagnosis = classify_opencode_failure(
            return_code=-signal.SIGKILL,
            model="openai/gpt-5.3-codex-spark",
        )
        self.assertIsNotNone(diagnosis)
        assert diagnosis is not None
        self.assertIn(RECOMMENDED_OPENCODE_MODEL, diagnosis)
        self.assertIn("openai/gpt-5.3-codex-spark", diagnosis)


class EnsureAgentFailureLabelTests(unittest.TestCase):
    @patch("scripts.run_github_issues_to_opencode.command_succeeds")
    @patch("scripts.run_github_issues_to_opencode.run_check_command")
    def test_ensure_label_ignores_create_race_if_label_exists(
        self,
        run_check_command,
        command_succeeds,
    ) -> None:
        # First label lookup misses (stale read), then create returns already-exists.
        command_succeeds.side_effect = [False, True]
        run_check_command.return_value = (
            False,
            "",
            "label with name \"auto:agent-failed\" already exists; use --force to update its color",
            1,
        )

        ensure_agent_failure_label(repo="owner/repo", dry_run=False)

        run_check_command.assert_called_once()
        self.assertGreaterEqual(command_succeeds.call_count, 2)

    @patch("scripts.run_github_issues_to_opencode.command_succeeds")
    @patch("scripts.run_github_issues_to_opencode.run_check_command")
    def test_ensure_label_raises_on_unknown_create_failure(
        self,
        run_check_command,
        command_succeeds,
    ) -> None:
        command_succeeds.return_value = False
        run_check_command.return_value = (
            False,
            "",
            "some other create-time failure",
            1,
        )

        with self.assertRaises(RuntimeError) as ctx:
            ensure_agent_failure_label(repo="owner/repo", dry_run=False)

        self.assertIn("Failed to create missing failure label", str(ctx.exception))
        self.assertIn(AGENT_FAILURE_LABEL_NAME, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
