import argparse
import unittest

from scripts.run_github_issues_to_opencode import (
    build_attempt_execution_plan,
    choose_routed_preset,
    resolve_task_execution_settings,
    validate_project_config,
)


class BudgetAwareRoutingTests(unittest.TestCase):
    def test_validate_project_config_accepts_routing_and_budgets(self) -> None:
        config = validate_project_config(
            config={
                "routing": {
                    "default_preset": "default",
                    "rules": [
                        {"when": {"labels": ["docs"], "task_types": ["issue"]}, "preset": "cheap"},
                        {"when": {"needs_decomposition": True}, "preset": "hard"},
                    ],
                },
                "budgets": {
                    "max_attempts_per_task": 2,
                    "max_runtime_minutes": 10,
                    "max_cost_usd": 1.25,
                    "max_model_tier": "default",
                },
                "presets": {
                    "cheap": {"runner": "opencode"},
                    "default": {"runner": "opencode"},
                    "hard": {"runner": "claude"},
                },
            },
            config_path="project-config.json",
        )

        self.assertIn("routing", config)
        self.assertIn("budgets", config)

    def test_choose_routed_preset_prefers_matching_rule(self) -> None:
        project_config = {
            "routing": {
                "default_preset": "default",
                "rules": [
                    {"when": {"labels": ["docs"]}, "preset": "cheap"},
                    {"when": {"needs_decomposition": True}, "preset": "hard"},
                ],
            },
            "presets": {"cheap": {}, "default": {}, "hard": {}},
        }
        issue = {"labels": [{"name": "docs"}]}

        preset = choose_routed_preset(
            project_config=project_config,
            issue=issue,
            task_type="issue",
            scope_eligible=True,
            needs_decomposition=False,
        )

        self.assertEqual(preset, "cheap")

    def test_resolve_task_execution_settings_applies_routing_and_budget_caps(self) -> None:
        args = argparse.Namespace(
            runner="claude",
            agent="build",
            model=None,
            preset=None,
            track_tokens=False,
            token_budget=20000,
            agent_timeout_seconds=1800,
            agent_idle_timeout_seconds=None,
            max_attempts=4,
            escalate_to_preset="hard",
        )
        project_config = {
            "routing": {
                "default_preset": "default",
                "rules": [{"when": {"labels": ["docs"]}, "preset": "cheap"}],
            },
            "budgets": {
                "max_attempts_per_task": 2,
                "max_runtime_minutes": 5,
                "max_cost_usd": 0.5,
                "max_model_tier": "default",
            },
            "presets": {
                "cheap": {
                    "runner": "opencode",
                    "model": "openai/gpt-4o-mini",
                    "max_attempts": 3,
                    "escalate_to_preset": "hard",
                },
                "default": {"runner": "opencode", "model": "openai/gpt-4o"},
                "hard": {"runner": "claude", "model": "claude-sonnet-4-5"},
            },
        }

        settings = resolve_task_execution_settings(
            args=args,
            argv=[],
            project_config=project_config,
            issue={"labels": [{"name": "docs"}]},
            task_type="issue",
            scope_eligible=True,
            needs_decomposition=False,
        )

        self.assertEqual(settings["preset"], "cheap")
        self.assertEqual(settings["runner"], "opencode")
        self.assertEqual(settings["model"], "openai/gpt-4o-mini")
        self.assertEqual(settings["max_attempts"], 2)
        self.assertEqual(settings["agent_timeout_seconds"], 300)
        self.assertEqual(settings["cost_budget_usd"], 0.5)
        self.assertEqual(settings["max_model_tier"], "default")

    def test_build_attempt_execution_plan_stops_escalation_at_budget_tier(self) -> None:
        project_config = {
            "presets": {
                "cheap": {"runner": "opencode", "model": "mini", "escalate_to_preset": "default"},
                "default": {"runner": "opencode", "model": "base", "escalate_to_preset": "hard"},
                "hard": {"runner": "claude", "model": "strong"},
            }
        }

        plan = build_attempt_execution_plan(
            project_config=project_config,
            initial_settings={
                "preset": "cheap",
                "runner": "opencode",
                "model": "mini",
                "max_attempts": 3,
                "escalate_to_preset": "default",
                "max_model_tier": "default",
            },
        )

        self.assertEqual([attempt["preset"] for attempt in plan], ["cheap", "default", "default"])


if __name__ == "__main__":
    unittest.main()
