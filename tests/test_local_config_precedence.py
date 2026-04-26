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
        self.assertEqual(args.sync_reused_branch, BUILTIN_DEFAULTS["sync_reused_branch"])
        self.assertEqual(args.sync_strategy, BUILTIN_DEFAULTS["sync_strategy"])
        self.assertEqual(args.base_branch, BUILTIN_DEFAULTS["base_branch"])

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
                        "sync_reused_branch": False,
                        "sync_strategy": "merge",
                        "base_branch": "current",
                    },
                    config_file,
                )

            args = parse_args(["--dir", tmpdir])

        self.assertEqual(args.runner, "opencode")
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.branch_prefix, "my-fixes")
        self.assertTrue(args.fail_on_existing)
        self.assertFalse(args.sync_reused_branch)
        self.assertEqual(args.sync_strategy, "merge")
        self.assertEqual(args.base_branch, "current")

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
                    },
                    config_file,
                )

            args = parse_args(
                ["--dir", tmpdir, "--runner", "claude", "--limit", "7"]
            )

        self.assertEqual(args.runner, "claude")
        self.assertEqual(args.limit, 7)
        self.assertEqual(args.branch_prefix, "my-fixes")

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
