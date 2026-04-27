import json
import os
import tempfile
import unittest

from scripts.run_github_issues_to_opencode import BUILTIN_DEFAULTS, parse_args


class LocalConfigPrecedenceTests(unittest.TestCase):
    def test_defaults_used_when_local_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            args = parse_args(["--dir", tmpdir])

        self.assertEqual(args.runner, BUILTIN_DEFAULTS["runner"])
        self.assertEqual(args.limit, BUILTIN_DEFAULTS["limit"])
        self.assertEqual(args.branch_prefix, BUILTIN_DEFAULTS["branch_prefix"])
        self.assertEqual(args.fail_on_existing, BUILTIN_DEFAULTS["fail_on_existing"])
        self.assertEqual(args.skip_if_pr_exists, BUILTIN_DEFAULTS["skip_if_pr_exists"])
        self.assertEqual(args.skip_if_branch_exists, BUILTIN_DEFAULTS["skip_if_branch_exists"])
        self.assertEqual(args.force_reprocess, BUILTIN_DEFAULTS["force_reprocess"])
        self.assertEqual(args.sync_reused_branch, BUILTIN_DEFAULTS["sync_reused_branch"])
        self.assertEqual(args.sync_strategy, BUILTIN_DEFAULTS["sync_strategy"])
        self.assertEqual(args.base_branch, BUILTIN_DEFAULTS["base_branch"])
        self.assertEqual(args.decompose, BUILTIN_DEFAULTS["decompose"])
        self.assertEqual(args.create_child_issues, BUILTIN_DEFAULTS["create_child_issues"])
        self.assertEqual(args.track_tokens, BUILTIN_DEFAULTS["track_tokens"])
        self.assertEqual(args.token_budget, BUILTIN_DEFAULTS["token_budget"])

    def test_local_config_overrides_built_in_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            config_path = os.path.join(tmpdir, "local-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                {
                    "runner": "opencode",
                    "limit": 3,
                    "branch_prefix": "my-fixes",
                    "fail_on_existing": True,
                        "skip_if_pr_exists": False,
                        "skip_if_branch_exists": False,
                        "force_reprocess": True,
                        "sync_reused_branch": False,
                        "sync_strategy": "merge",
                    "base_branch": "current",
                    "decompose": "never",
                    "track_tokens": True,
                    "token_budget": 20000,
                    "create_child_issues": True,
                },
                config_file,
                )

            args = parse_args(["--dir", tmpdir])

        self.assertEqual(args.runner, "opencode")
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.branch_prefix, "my-fixes")
        self.assertTrue(args.fail_on_existing)
        self.assertFalse(args.skip_if_pr_exists)
        self.assertFalse(args.skip_if_branch_exists)
        self.assertTrue(args.force_reprocess)
        self.assertFalse(args.sync_reused_branch)
        self.assertTrue(args.track_tokens)
        self.assertEqual(args.token_budget, 20000)
        self.assertEqual(args.sync_strategy, "merge")
        self.assertEqual(args.base_branch, "current")
        self.assertEqual(args.decompose, "never")
        self.assertTrue(args.create_child_issues)

    def test_cli_flags_override_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            config_path = os.path.join(tmpdir, "local-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                {
                    "runner": "opencode",
                    "limit": 2,
                    "branch_prefix": "my-fixes",
                    "track_tokens": False,
                    "token_budget": 15000,
                },
                config_file,
                )

            args = parse_args(
                [
                    "--dir",
                    tmpdir,
                    "--runner",
                    "claude",
                    "--limit",
                    "7",
                    "--track-tokens",
                    "--token-budget",
                    "25000",
                ]
            )

        self.assertEqual(args.runner, "claude")
        self.assertEqual(args.limit, 7)
        self.assertEqual(args.branch_prefix, "my-fixes")
        self.assertTrue(args.track_tokens)
        self.assertEqual(args.token_budget, 25000)

    def test_project_config_track_tokens_default_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            with open(project_config_path, "w", encoding="utf-8") as config_file:
                json.dump({"defaults": {"track_tokens": True}}, config_file)

            args = parse_args(["--dir", tmpdir, "--project-config", project_config_path])

        self.assertTrue(args.track_tokens)

    def test_project_config_token_budget_default_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            with open(project_config_path, "w", encoding="utf-8") as config_file:
                json.dump({"defaults": {"token_budget": 18000}}, config_file)

            args = parse_args(["--dir", tmpdir, "--project-config", project_config_path])

        self.assertEqual(args.token_budget, 18000)

    def test_local_config_overrides_project_config_track_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            local_config_path = os.path.join(tmpdir, "local-config.json")
            with open(project_config_path, "w", encoding="utf-8") as project_config_file:
                json.dump({"defaults": {"track_tokens": True}}, project_config_file)
            with open(local_config_path, "w", encoding="utf-8") as local_config_file:
                json.dump({"track_tokens": False}, local_config_file)

            args = parse_args(
                ["--dir", tmpdir, "--project-config", project_config_path, "--local-config", local_config_path]
            )

        self.assertFalse(args.track_tokens)

    def test_local_config_overrides_project_config_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            local_config_path = os.path.join(tmpdir, "local-config.json")
            with open(project_config_path, "w", encoding="utf-8") as project_config_file:
                json.dump({"defaults": {"token_budget": 18000}}, project_config_file)
            with open(local_config_path, "w", encoding="utf-8") as local_config_file:
                json.dump({"token_budget": 12000}, local_config_file)

            args = parse_args(
                ["--dir", tmpdir, "--project-config", project_config_path, "--local-config", local_config_path]
            )

        self.assertEqual(args.token_budget, 12000)

    def test_cli_track_tokens_overrides_project_and_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            local_config_path = os.path.join(tmpdir, "local-config.json")
            with open(project_config_path, "w", encoding="utf-8") as project_config_file:
                json.dump({"defaults": {"track_tokens": False}}, project_config_file)
            with open(local_config_path, "w", encoding="utf-8") as local_config_file:
                json.dump({"track_tokens": False}, local_config_file)

            args = parse_args(
                [
                    "--dir",
                    tmpdir,
                    "--project-config",
                    project_config_path,
                    "--local-config",
                    local_config_path,
                    "--track-tokens",
                ]
            )

        self.assertTrue(args.track_tokens)

    def test_cli_token_budget_overrides_project_and_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            project_config_path = os.path.join(tmpdir, "project-config.json")
            local_config_path = os.path.join(tmpdir, "local-config.json")
            with open(project_config_path, "w", encoding="utf-8") as project_config_file:
                json.dump({"defaults": {"token_budget": 9000}}, project_config_file)
            with open(local_config_path, "w", encoding="utf-8") as local_config_file:
                json.dump({"token_budget": 12000}, local_config_file)

            args = parse_args(
                [
                    "--dir",
                    tmpdir,
                    "--project-config",
                    project_config_path,
                    "--local-config",
                    local_config_path,
                    "--token-budget",
                    "25000",
                ]
            )

        self.assertEqual(args.token_budget, 25000)

    def test_create_child_issues_flag_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))

            args = parse_args(["--dir", tmpdir, "--create-child-issues"])

        self.assertTrue(args.create_child_issues)

    def test_explicit_local_config_path_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            config_dir = os.path.join(tmpdir, "configs")
            os.makedirs(config_dir, exist_ok=True)
            custom_config = os.path.join(config_dir, "dev.json")
            with open(custom_config, "w", encoding="utf-8") as config_file:
                json.dump({"limit": 5}, config_file)

            args = parse_args(["--dir", tmpdir, "--local-config", custom_config])

        self.assertEqual(args.limit, 5)


if __name__ == "__main__":
    unittest.main()
