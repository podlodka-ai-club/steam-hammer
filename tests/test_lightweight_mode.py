import argparse
import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.run_github_issues_to_opencode import BUILTIN_DEFAULTS, build_lightweight_prompt, main, parse_args


class LightweightModeTests(unittest.TestCase):
    def test_parse_args_accepts_lightweight_flag_and_local_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            config_path = os.path.join(tmpdir, "local-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"mode": "lightweight"}, config_file)

            configured_args = parse_args(["--dir", tmpdir])
            cli_args = parse_args(["--dir", tmpdir, "--lightweight"])

        self.assertEqual(BUILTIN_DEFAULTS["mode"], "full")
        self.assertEqual(configured_args.mode, "lightweight")
        self.assertEqual(cli_args.mode, "lightweight")

    def test_build_lightweight_prompt_adds_focus_paths(self) -> None:
        issue = {
            "number": 254,
            "title": "Add lightweight mode for fast single-file / small fixes",
            "url": "https://example.test/issues/254",
            "body": """
## Problem

The Python runner and Go CLI wrapper still do too much work.

## Proposed solution

- Add --mode lightweight.
- Keep orchestration state comments out of the fast path.

## Acceptance criteria

- scripts/run_github_issues_to_opencode.py handles lightweight mode.
- internal/cli/flags.go and internal/cli/command_run.go accept the flag.
""",
        }

        prompt = build_lightweight_prompt(issue)

        self.assertIn("Focus paths:", prompt)
        self.assertIn("`scripts/run_github_issues_to_opencode.py`", prompt)
        self.assertIn("`internal/cli/flags.go`", prompt)
        self.assertIn("`internal/cli/command_run.go`", prompt)
        self.assertIn("Compact issue context:", prompt)

    def test_build_lightweight_prompt_adds_python_compatibility_adapter_focus_path(self) -> None:
        issue = {
            "number": 296,
            "title": "Go migration: shrink Python compatibility adapter to removable boundary",
            "url": "https://example.test/issues/296",
            "body": """
## Overview

Go migration should own the critical runtime loops while the Python compatibility adapter is reduced to a removable boundary.
""",
        }

        prompt = build_lightweight_prompt(issue)

        self.assertIn("Focus paths:", prompt)
        self.assertIn("`scripts/run_github_issues_to_opencode.py`", prompt)

    def test_main_lightweight_mode_skips_scope_and_state_comments_and_posts_summary(self) -> None:
        args = argparse.Namespace(
            repo="owner/repo",
            tracker="github",
            codehost="github",
            issue=254,
            pr=None,
            from_review_comments=False,
            state="open",
            limit=10,
            runner="opencode",
            agent="build",
            model=None,
            preset=None,
            agent_timeout_seconds=900,
            agent_idle_timeout_seconds=None,
            token_budget=None,
            max_attempts=1,
            escalate_to_preset=None,
            opencode_auto_approve=False,
            branch_prefix="issue-fix",
            include_empty=False,
            stop_on_error=False,
            fail_on_existing=False,
            force_issue_flow=False,
            skip_if_pr_exists=False,
            skip_if_branch_exists=False,
            force_reprocess=False,
            conflict_recovery_only=False,
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
            isolate_worktree=False,
            mode="lightweight",
            autonomous=False,
            autonomous_session_file=None,
        )
        issue = {
            "number": 254,
            "title": "Add lightweight mode for fast single-file / small fixes",
            "body": "Python runner and Go CLI wrapper should both accept --lightweight.",
            "url": "https://example.test/issues/254",
            "state": "open",
        }
        codehost_provider = Mock()
        codehost_provider.find_open_pr_for_issue.return_value = None
        codehost_provider.ensure_pr.return_value = ("created", "https://example.test/pull/88")

        with (
            patch("scripts.run_github_issues_to_opencode.parse_args", return_value=args),
            patch("scripts.run_github_issues_to_opencode.load_project_config", return_value={}),
            patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
            patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
            patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
            patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
            patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.evaluate_issue_scope") as scope_mock,
            patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment") as state_comment_mock,
            patch("scripts.run_github_issues_to_opencode.safe_post_issue_scope_skip_comment") as scope_comment_mock,
            patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue") as remove_label_mock,
            patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
            patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0) as run_agent_mock,
            patch("scripts.run_github_issues_to_opencode.commit_changes"),
            patch("scripts.run_github_issues_to_opencode.run_configured_workflow_checks", return_value=[]),
            patch("scripts.run_github_issues_to_opencode.push_branch"),
            patch("scripts.run_github_issues_to_opencode.safe_post_lightweight_completion_comment") as summary_mock,
            patch("scripts.run_github_issues_to_opencode.current_repo_root", return_value="/tmp/repo"),
            patch("scripts.run_github_issues_to_opencode.current_codehost_provider", return_value=codehost_provider),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        scope_mock.assert_not_called()
        state_comment_mock.assert_not_called()
        scope_comment_mock.assert_not_called()
        remove_label_mock.assert_not_called()
        summary_mock.assert_called_once()
        prompt_override = run_agent_mock.call_args.kwargs.get("prompt_override")
        self.assertIsNotNone(prompt_override)
        assert prompt_override is not None
        self.assertIn("Focus paths:", prompt_override)
        self.assertIn("scripts/run_github_issues_to_opencode.py", prompt_override)


if __name__ == "__main__":
    unittest.main()
