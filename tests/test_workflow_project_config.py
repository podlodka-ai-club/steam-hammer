import json
import os
import tempfile
import unittest

from scripts.run_github_issues_to_opencode import (
    count_current_pr_approvals,
    evaluate_pr_readiness,
    load_project_config,
    workflow_merge_policy,
)


class WorkflowProjectConfigTests(unittest.TestCase):
    def test_project_config_accepts_extended_workflow_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "project-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        "workflow": {
                            "commands": {
                                "setup": "make setup",
                                "test": "make test",
                                "lint": "make lint",
                                "build": "make build",
                                "e2e": "make e2e",
                            },
                            "hooks": {
                                "before_agent": ["./scripts/pre-agent.sh"],
                                "after_agent": "./scripts/post-agent.sh",
                                "before_pr_update": [],
                                "after_pr_update": [],
                            },
                            "readiness": {
                                "required_checks": ["ci/test", "ci/lint"],
                                "required_approvals": 1,
                                "require_review_approval": True,
                                "require_required_file_evidence": False,
                                "require_green_checks": True,
                                "require_local_workflow_checks": True,
                            },
                            "merge": {"auto_merge": True, "method": "squash"},
                        }
                    },
                    config_file,
                )

            config = load_project_config(config_path)

        self.assertIn("workflow", config)
        self.assertEqual(config["workflow"]["commands"]["e2e"], "make e2e")

    def test_project_config_rejects_unknown_workflow_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "project-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"workflow": {"hooks": {"before_merge": ["./hook.sh"]}}}, config_file)

            with self.assertRaisesRegex(RuntimeError, "workflow.hooks"):
                load_project_config(config_path)

    def test_workflow_merge_policy_normalizes_auto_merge_to_auto(self) -> None:
        merge_policy = workflow_merge_policy(
            {"workflow": {"merge": {"auto_merge": True, "method": "squash"}}}
        )

        self.assertEqual(merge_policy, {"auto": True, "method": "squash"})

    def test_current_pr_approvals_use_latest_review_per_author(self) -> None:
        reviews = [
            {
                "state": "APPROVED",
                "submittedAt": "2026-04-24T11:00:00Z",
                "author": {"login": "alice"},
            },
            {
                "state": "COMMENTED",
                "submittedAt": "2026-04-24T12:00:00Z",
                "author": {"login": "alice"},
            },
            {
                "state": "APPROVED",
                "submittedAt": "2026-04-24T12:30:00Z",
                "author": {"login": "bob"},
            },
            {
                "state": "APPROVED",
                "submittedAt": "2026-04-24T12:45:00Z",
                "author": {"login": "author"},
            },
        ]

        approvals = count_current_pr_approvals(reviews, pr_author_login="author")

        self.assertEqual(approvals, 1)

    def test_readiness_waits_for_required_approval(self) -> None:
        project_config = {
            "workflow": {
                "readiness": {
                    "required_checks": ["ci/test"],
                    "required_approvals": 1,
                    "require_required_file_evidence": False,
                }
            }
        }
        pull_request = {
            "reviews": [],
            "author": {"login": "author"},
            "files": [{"path": "README.md"}],
        }
        ci_status = {
            "overall": "success",
            "checks": [{"name": "ci/test", "state": "success", "url": "https://example/check/1"}],
            "failing_checks": [],
        }

        readiness = evaluate_pr_readiness(project_config, pull_request, ci_status, linked_issues=[])

        self.assertEqual(readiness["status"], "ready-for-review")
        self.assertEqual(readiness["next_action"], "await_required_approval")

    def test_readiness_waits_for_named_required_checks(self) -> None:
        project_config = {
            "workflow": {
                "readiness": {
                    "required_checks": ["ci/test", "ci/lint"],
                    "require_required_file_evidence": False,
                }
            }
        }
        pull_request = {
            "reviews": [],
            "author": {"login": "author"},
            "files": [{"path": "README.md"}],
        }
        ci_status = {
            "overall": "success",
            "checks": [{"name": "ci/test", "state": "success"}],
            "failing_checks": [],
        }

        readiness = evaluate_pr_readiness(project_config, pull_request, ci_status, linked_issues=[])

        self.assertEqual(readiness["status"], "waiting-for-ci")
        self.assertIn("ci/lint", str(readiness["error"]))

    def test_readiness_blocks_when_green_checks_are_required_and_ci_is_failing(self) -> None:
        project_config = {
            "workflow": {
                "readiness": {
                    "require_green_checks": True,
                    "require_required_file_evidence": False,
                }
            }
        }
        pull_request = {
            "reviews": [],
            "author": {"login": "author"},
            "files": [{"path": "README.md"}],
        }
        ci_status = {
            "overall": "failure",
            "checks": [{"name": "ci/test", "state": "failure", "url": "https://example/check/1"}],
            "failing_checks": [{"name": "ci/test", "state": "failure", "url": "https://example/check/1"}],
        }

        readiness = evaluate_pr_readiness(project_config, pull_request, ci_status, linked_issues=[])

        self.assertEqual(readiness["status"], "blocked")
        self.assertEqual(readiness["next_action"], "inspect_failing_ci_checks")

    def test_readiness_waits_when_green_checks_are_required_but_not_started(self) -> None:
        project_config = {
            "workflow": {
                "readiness": {
                    "require_green_checks": True,
                    "require_required_file_evidence": False,
                }
            }
        }
        pull_request = {
            "reviews": [],
            "author": {"login": "author"},
            "files": [{"path": "README.md"}],
        }
        ci_status = {
            "overall": "success",
            "checks": [],
            "failing_checks": [],
        }

        readiness = evaluate_pr_readiness(project_config, pull_request, ci_status, linked_issues=[])

        self.assertEqual(readiness["status"], "waiting-for-ci")
        self.assertEqual(readiness["next_action"], "wait_for_ci")


if __name__ == "__main__":
    unittest.main()
