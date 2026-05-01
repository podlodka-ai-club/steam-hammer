import signal
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    AGENT_FAILURE_LABEL_NAME,
    RECOMMENDED_OPENCODE_MODEL,
    classify_opencode_failure,
    command_succeeds,
    describe_exit_code,
    ensure_agent_failure_label,
    run_capture,
    run_check_command,
    validate_opencode_model_backend,
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


class ValidateOpenCodeModelBackendTests(unittest.TestCase):
    def test_non_ollama_models_skip_validation(self) -> None:
        validate_opencode_model_backend(runner="opencode", model="openai/gpt-4o")
        validate_opencode_model_backend(runner="claude", model="ollama/qwen3.5:2b")

    @patch("scripts.run_github_issues_to_opencode.shutil.which", return_value=None)
    def test_missing_ollama_cli_raises_clear_error(self, _which) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            validate_opencode_model_backend(runner="opencode", model="ollama/qwen3.5:2b")

        self.assertIn("`ollama` CLI", str(ctx.exception))
        self.assertIn("ollama/qwen3.5:2b", str(ctx.exception))

    @patch("scripts.run_github_issues_to_opencode.shutil.which", return_value="/usr/local/bin/ollama")
    @patch("scripts.run_github_issues_to_opencode.subprocess.run")
    def test_ollama_show_failure_raises_actionable_error(self, run_mock, _which) -> None:
        run_mock.return_value = SimpleNamespace(returncode=1, stdout="", stderr="model not found")

        with self.assertRaises(RuntimeError) as ctx:
            validate_opencode_model_backend(runner="opencode", model="ollama/qwen3.5:2b")

        self.assertIn("Unable to validate local Ollama model 'qwen3.5:2b'", str(ctx.exception))
        self.assertIn("ollama show qwen3.5:2b", str(ctx.exception))

    @patch("scripts.run_github_issues_to_opencode.shutil.which", return_value="/usr/local/bin/ollama")
    @patch("scripts.run_github_issues_to_opencode.subprocess.run")
    def test_ollama_show_timeout_raises_clear_error(self, run_mock, _which) -> None:
        run_mock.side_effect = subprocess.TimeoutExpired(cmd=["ollama", "show", "qwen3.5:2b"], timeout=30)

        with self.assertRaises(RuntimeError) as ctx:
            validate_opencode_model_backend(runner="opencode", model="ollama/qwen3.5:2b")

        self.assertIn("Timed out after 30s", str(ctx.exception))
        self.assertIn("qwen3.5:2b", str(ctx.exception))

    @patch("scripts.run_github_issues_to_opencode.shutil.which", return_value="/usr/local/bin/ollama")
    @patch("scripts.run_github_issues_to_opencode.subprocess.run")
    def test_ollama_show_uses_explicit_utf8_text_encoding(self, run_mock, _which) -> None:
        run_mock.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        validate_opencode_model_backend(runner="opencode", model="ollama/qwen3.5:2b")

        self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")
        self.assertTrue(run_mock.call_args.kwargs["text"])


class SubprocessUtf8Tests(unittest.TestCase):
    @patch("scripts.run_github_issues_to_opencode.subprocess.run")
    def test_run_capture_uses_explicit_utf8_text_encoding(self, run_mock) -> None:
        run_mock.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")

        self.assertEqual(run_capture(["gh", "status"]), "ok")

        self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")
        self.assertTrue(run_mock.call_args.kwargs["text"])

    @patch("scripts.run_github_issues_to_opencode.subprocess.run")
    def test_command_succeeds_uses_explicit_utf8_text_encoding(self, run_mock) -> None:
        run_mock.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertTrue(command_succeeds(["gh", "status"]))

        self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")
        self.assertTrue(run_mock.call_args.kwargs["text"])

    @patch("scripts.run_github_issues_to_opencode.subprocess.run")
    def test_run_check_command_uses_explicit_utf8_text_encoding(self, run_mock) -> None:
        run_mock.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        self.assertEqual(run_check_command(["gh", "status"]), (True, "ok", "", 0))

        self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")
        self.assertTrue(run_mock.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
