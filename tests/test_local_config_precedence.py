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

    def test_local_config_overrides_built_in_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            os.makedirs(os.path.join(tmpdir, ".opencode"), exist_ok=True)
            config_path = os.path.join(tmpdir, ".opencode", "local-config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        "runner": "opencode",
                        "limit": 3,
                        "branch_prefix": "my-fixes",
                    },
                    config_file,
                )

            args = parse_args(["--dir", tmpdir])

        self.assertEqual(args.runner, "opencode")
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.branch_prefix, "my-fixes")

    def test_cli_flags_override_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, ".git"))
            os.makedirs(os.path.join(tmpdir, ".opencode"), exist_ok=True)
            config_path = os.path.join(tmpdir, ".opencode", "local-config.json")
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


if __name__ == "__main__":
    unittest.main()
