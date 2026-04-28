import unittest
from unittest import mock

from scripts import merge_result_verification as mod


class MergeResultVerificationModuleTests(unittest.TestCase):
    def test_determine_need_skips_docs_only_pr(self) -> None:
        decision = mod.determine_merge_result_verification_need(
            repo="owner/repo",
            pull_request={
                "number": 42,
                "baseRefName": "main",
                "files": [{"path": "docs/runbook.md"}, {"path": "README.md"}],
            },
            list_open_pull_requests=mock.Mock(return_value=[]),
            fetch_pull_request=mock.Mock(),
        )

        self.assertFalse(decision["required"])
        self.assertEqual(decision["reason"], "docs-only")

    def test_determine_need_requires_overlap_for_non_central_pr(self) -> None:
        decision = mod.determine_merge_result_verification_need(
            repo="owner/repo",
            pull_request={
                "number": 42,
                "baseRefName": "main",
                "files": [{"path": "pkg/service/handler.py"}],
            },
            list_open_pull_requests=mock.Mock(
                return_value=[
                    {
                        "number": 77,
                        "headRefName": "issue-fix/77-overlap",
                        "baseRefName": "main",
                    }
                ]
            ),
            fetch_pull_request=mock.Mock(return_value={"files": [{"path": "pkg/service/handler.py"}]}),
        )

        self.assertTrue(decision["required"])
        self.assertEqual(decision["reason"], "overlapping-open-prs")
        self.assertEqual(decision["overlapping_prs"][0]["number"], 77)

    def test_verify_pull_request_merge_result_uses_temp_clone(self) -> None:
        determine_need = mock.Mock(
            return_value={
                "required": True,
                "reason": "overlapping-open-prs",
                "summary": "required (overlaps with open PRs: #77)",
                "changed_files": ["pkg/service/handler.py"],
                "overlapping_prs": [{"number": 77, "files": ["pkg/service/handler.py"]}],
            }
        )
        resolve_commands = mock.Mock(return_value=[("go-test", "go test ./...")])
        run_command = mock.Mock()
        run_check_command = mock.Mock(side_effect=[(True, "", "", 0), (True, "ok", "", 0)])

        with (
            mock.patch.object(mod.tempfile, "mkdtemp", return_value="/tmp/merge-verify-pr-42"),
            mock.patch.object(mod.shutil, "rmtree") as rmtree_mock,
        ):
            verification = mod.verify_pull_request_merge_result(
                repo="owner/repo",
                pull_request={
                    "number": 42,
                    "baseRefName": "main",
                    "headRefName": "issue-fix/42-overlap",
                },
                project_config={},
                repo_dir="/repo",
                dry_run=False,
                determine_need=determine_need,
                resolve_commands=resolve_commands,
                run_command=run_command,
                run_check_command=run_check_command,
                workflow_output_excerpt=lambda text: text,
                short_error_text=lambda text: text,
            )

        self.assertEqual(verification["status"], "passed")
        self.assertEqual(verification["checkout"], "temp-clone")
        determine_need.assert_called_once_with(
            repo="owner/repo",
            pull_request={
                "number": 42,
                "baseRefName": "main",
                "headRefName": "issue-fix/42-overlap",
            },
        )
        resolve_commands.assert_called_once_with(project_config={}, cwd="/repo")
        run_command.assert_has_calls(
            [
                mock.call(["git", "clone", "--quiet", "/repo", "/tmp/merge-verify-pr-42"]),
                mock.call(["git", "-C", "/tmp/merge-verify-pr-42", "fetch", "origin", "main", "issue-fix/42-overlap"]),
                mock.call(["git", "-C", "/tmp/merge-verify-pr-42", "checkout", "--detach", "origin/main"]),
            ]
        )
        run_check_command.assert_has_calls(
            [
                mock.call(
                    ["git", "merge", "--no-ff", "--no-commit", "origin/issue-fix/42-overlap"],
                    cwd="/tmp/merge-verify-pr-42",
                ),
                mock.call(["bash", "-lc", "go test ./..."], cwd="/tmp/merge-verify-pr-42"),
            ]
        )
        rmtree_mock.assert_called_once_with("/tmp/merge-verify-pr-42", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
