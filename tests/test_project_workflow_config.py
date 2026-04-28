import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    configured_setup_command,
    configured_workflow_commands,
    evaluate_pr_readiness,
    main,
    parse_args,
    run_configured_workflow_hooks,
    run_doctor,
    validate_project_config,
    workflow_hooks,
)


class ProjectWorkflowConfigTests(unittest.TestCase):
    def test_validate_project_config_accepts_extended_workflow_schema(self) -> None:
        config = {
            "workflow": {
                "commands": {
                    "setup": "make setup",
                    "test": "make test",
                    "lint": "make lint",
                    "build": "make build",
                    "e2e": "make e2e",
                },
                "hooks": {
                    "pre_agent": "scripts/pre-agent.sh",
                    "post_agent": "scripts/post-agent.sh",
                    "pre_pr_update": "scripts/pre-pr.sh",
                    "post_pr_update": "scripts/post-pr.sh",
                },
                "readiness": {
                    "required_checks": ["ci / test", "ci / lint"],
                    "required_approvals": 1,
                    "require_review": True,
                    "require_mergeable": True,
                    "require_required_file_evidence": True,
                },
                "merge": {
                    "auto": False,
                    "method": "squash",
                },
            }
        }

        validated = validate_project_config(config, "project-config.json")

        self.assertEqual(configured_setup_command(validated), "make setup")
        self.assertEqual(
            configured_workflow_commands(validated),
            [
                ("test", "make test"),
                ("lint", "make lint"),
                ("build", "make build"),
                ("e2e", "make e2e"),
            ],
        )
        self.assertEqual(
            workflow_hooks(validated),
            {
                "pre_agent": "scripts/pre-agent.sh",
                "post_agent": "scripts/post-agent.sh",
                "pre_pr_update": "scripts/pre-pr.sh",
                "post_pr_update": "scripts/post-pr.sh",
            },
        )

    def test_validate_project_config_rejects_unknown_merge_method(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "workflow.merge.method"):
            validate_project_config(
                {"workflow": {"merge": {"method": "fast-forward"}}},
                "project-config.json",
            )

    def test_pr_readiness_waits_for_required_check_presence(self) -> None:
        readiness = evaluate_pr_readiness(
            pull_request={"reviews": [], "author": {"login": "author"}, "mergeStateStatus": "CLEAN"},
            ci_status={
                "checks": [{"name": "ci / lint", "state": "success"}],
                "overall": "success",
            },
            required_file_validation={"status": "passed"},
            project_config={"workflow": {"readiness": {"required_checks": ["ci / test"]}}},
        )

        self.assertEqual(readiness["status"], "waiting-for-ci")
        self.assertEqual(readiness["next_action"], "wait_for_ci")

    def test_pr_readiness_waits_for_required_approval(self) -> None:
        readiness = evaluate_pr_readiness(
            pull_request={
                "reviews": [
                    {
                        "state": "COMMENTED",
                        "submittedAt": "2026-04-28T10:00:00Z",
                        "author": {"login": "reviewer1"},
                    }
                ],
                "author": {"login": "author"},
                "mergeStateStatus": "CLEAN",
            },
            ci_status={
                "checks": [{"name": "ci / test", "state": "success"}],
                "overall": "success",
            },
            required_file_validation={"status": "passed"},
            project_config={
                "workflow": {
                    "readiness": {
                        "required_checks": ["ci / test"],
                        "required_approvals": 1,
                    }
                }
            },
        )

        self.assertEqual(readiness["status"], "ready-for-review")
        self.assertEqual(readiness["next_action"], "wait_for_review")

    def test_run_doctor_validates_project_workflow_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            with open(project_config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        "workflow": {
                            "commands": {"setup": "make setup", "test": "make test", "e2e": "make e2e"},
                            "hooks": {"pre_agent": "scripts/pre-agent.sh"},
                            "readiness": {"required_checks": ["ci / test"], "required_approvals": 1},
                            "merge": {"method": "squash", "auto": False},
                        }
                    },
                    config_file,
                )

            args = parse_args(["--doctor", "--dir", tmpdir, "--project-config", project_config_path])

            def fake_run_check(command: list[str], cwd: str | None = None) -> tuple[bool, str, str, int]:
                if command[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                    return True, "true", "", 0
                if command[:2] == ["git", "status"]:
                    return True, "", "", 0
                if command[:3] == ["gh", "auth", "status"]:
                    return True, "", "", 0
                if command[:3] == ["gh", "repo", "view"] and "defaultBranchRef" in command:
                    return True, "main", "", 0
                if command[:3] == ["gh", "repo", "view"]:
                    return True, "owner/repo", "", 0
                return True, "", "", 0

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), patch(
                "scripts.run_github_issues_to_opencode.shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}",
            ), patch(
                "scripts.run_github_issues_to_opencode.run_check_command",
                side_effect=fake_run_check,
            ):
                exit_code = run_doctor(args, ["--doctor", "--dir", tmpdir, "--project-config", project_config_path])

        self.assertEqual(exit_code, 0)
        self.assertIn("[PASS] Project config:", stdout.getvalue())
        self.assertIn("commands=setup, test, e2e", stdout.getvalue())

    def test_run_configured_workflow_hooks_runs_all_commands_with_merged_env(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(*, kind: str, name: str, command_text: str, dry_run: bool, cwd: str | None = None, env: dict[str, str] | None = None) -> dict[str, object]:
            calls.append(
                {
                    "kind": kind,
                    "name": name,
                    "command_text": command_text,
                    "dry_run": dry_run,
                    "cwd": cwd,
                    "env": env,
                }
            )
            return {"name": name, "command": command_text, "status": "passed", "exit_code": 0}

        with patch("scripts.run_github_issues_to_opencode._run_workflow_shell_command", side_effect=fake_run):
            results = run_configured_workflow_hooks(
                hook_name="pre_pr_update",
                configured_hooks={"pre_pr_update": ["./hooks/one.sh", "./hooks/two.sh"]},
                dry_run=False,
                cwd="/repo",
                env={"ORCHESTRATOR_MODE": "issue-flow"},
                context={"hook_target": "issue", "repo_dir": "/repo"},
            )

        self.assertEqual([call["name"] for call in calls], ["pre_pr_update[1]", "pre_pr_update[2]"])
        self.assertEqual([call["command_text"] for call in calls], ["./hooks/one.sh", "./hooks/two.sh"])
        self.assertEqual(results[0]["hook"], "pre_pr_update")
        self.assertEqual(results[1]["hook"], "pre_pr_update")
        first_env = calls[0]["env"]
        self.assertIsInstance(first_env, dict)
        self.assertEqual(first_env["ORCHESTRATOR_MODE"], "issue-flow")
        self.assertEqual(first_env["hook_target"], "issue")
        self.assertEqual(first_env["repo_dir"], "/repo")

    def test_main_runs_all_configured_issue_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            with open(project_config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        "workflow": {
                            "hooks": {
                                "pre_agent": ["./hooks/pre-one.sh", "./hooks/pre-two.sh"],
                                "post_agent": ["./hooks/post-one.sh", "./hooks/post-two.sh"],
                            }
                        }
                    },
                    config_file,
                )

            issue = {
                "number": 42,
                "title": "Title",
                "body": "Body",
                "url": "https://github.com/owner/repo/issues/42",
            }
            hook_calls: list[tuple[str, str]] = []

            def fake_run(*, kind: str, name: str, command_text: str, dry_run: bool, cwd: str | None = None, env: dict[str, str] | None = None) -> dict[str, object]:
                hook_calls.append((name, command_text))
                return {"name": name, "command": command_text, "status": "passed", "exit_code": 0}

            previous_cwd = os.getcwd()
            try:
                with (
                    patch.object(
                        sys,
                        "argv",
                        ["prog", "--dir", tmpdir, "--project-config", project_config_path, "--issue", "42", "--dry-run"],
                    ),
                    patch("scripts.run_github_issues_to_opencode.ensure_clean_worktree"),
                    patch("scripts.run_github_issues_to_opencode.detect_repo", return_value="owner/repo"),
                    patch("scripts.run_github_issues_to_opencode.detect_default_branch", return_value="main"),
                    patch("scripts.run_github_issues_to_opencode.fetch_issue", return_value=issue),
                    patch("scripts.run_github_issues_to_opencode.fetch_issue_comments", return_value=[]),
                    patch("scripts.run_github_issues_to_opencode.find_open_pr_for_issue", return_value=None),
                    patch("scripts.run_github_issues_to_opencode.remote_branch_exists", return_value=False),
                    patch("scripts.run_github_issues_to_opencode.prepare_issue_branch", return_value="created"),
                    patch("scripts.run_github_issues_to_opencode.run_agent", return_value=0),
                    patch("scripts.run_github_issues_to_opencode.commit_changes"),
                    patch("scripts.run_github_issues_to_opencode.push_branch"),
                    patch("scripts.run_github_issues_to_opencode.ensure_pr", return_value=("created", "")),
                    patch("scripts.run_github_issues_to_opencode.safe_post_orchestration_state_comment"),
                    patch("scripts.run_github_issues_to_opencode.remove_agent_failure_label_from_issue"),
                    patch("scripts.run_github_issues_to_opencode.run_configured_workflow_checks", return_value=[]),
                    patch("scripts.run_github_issues_to_opencode._run_workflow_shell_command", side_effect=fake_run),
                ):
                    exit_code = main()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            hook_calls,
            [
                ("pre_agent[1]", "./hooks/pre-one.sh"),
                ("pre_agent[2]", "./hooks/pre-two.sh"),
                ("post_agent[1]", "./hooks/post-one.sh"),
                ("post_agent[2]", "./hooks/post-two.sh"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
