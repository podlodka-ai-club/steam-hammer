import unittest
from unittest.mock import patch

from scripts.run_github_issues_to_opencode import (
    _build_agent_run_stats,
    _parse_cost_value,
    _parse_int_value,
    _record_agent_run_stats,
    _update_agent_run_stats,
)


class AgentRunStatsTests(unittest.TestCase):
    def test_parse_int_value_strips_separators(self) -> None:
        self.assertEqual(_parse_int_value("1,234"), 1234)
        self.assertEqual(_parse_int_value("12 345"), 12345)
        self.assertEqual(_parse_int_value("9_876_543"), 9876543)

    def test_parse_int_value_returns_none_for_invalid(self) -> None:
        self.assertIsNone(_parse_int_value("abc"))
        self.assertIsNone(_parse_int_value("1,2x4"))

    def test_parse_cost_value_parses_currency_values(self) -> None:
        self.assertEqual(_parse_cost_value("$1.23"), 1.23)
        self.assertEqual(_parse_cost_value("~$0.0421"), 0.0421)
        self.assertEqual(_parse_cost_value("$1,234.50"), 1234.5)

    def test_parse_cost_value_returns_none_for_invalid(self) -> None:
        self.assertIsNone(_parse_cost_value("not-a-number"))

    def test_update_agent_run_stats_parses_combined_in_out_tokens(self) -> None:
        tokens_in = None
        tokens_out = None
        cost = None

        tokens_in, tokens_out, cost = _update_agent_run_stats(
            "1,234 in / 5_678 out",
            track_tokens=True,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )

        self.assertEqual(tokens_in, 1234)
        self.assertEqual(tokens_out, 5678)
        self.assertIsNone(cost)

    def test_update_agent_run_stats_parses_named_token_lines_and_cost(self) -> None:
        tokens_in = None
        tokens_out = None
        cost = None

        tokens_in, tokens_out, cost = _update_agent_run_stats(
            "Input tokens: 2,000",
            track_tokens=True,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )
        tokens_in, tokens_out, cost = _update_agent_run_stats(
            "Output tokens: 3,000",
            track_tokens=True,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )
        tokens_in, tokens_out, cost = _update_agent_run_stats(
            "Estimated cost: $0.0421",
            track_tokens=True,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )

        self.assertEqual(tokens_in, 2000)
        self.assertEqual(tokens_out, 3000)
        self.assertEqual(cost, 0.0421)

    def test_update_agent_run_stats_ignores_non_matching_lines(self) -> None:
        tokens_in = 10
        tokens_out = 11
        cost = 0.5

        updated = _update_agent_run_stats(
            "No telemetry in this line",
            track_tokens=True,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )

        self.assertEqual(updated, (10, 11, 0.5))

    def test_update_agent_run_stats_disabled_returns_input_values(self) -> None:
        values = _update_agent_run_stats(
            "123 in / 456 out",
            track_tokens=False,
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
        )

        self.assertEqual(values, (None, None, None))

    def test_build_agent_run_stats_only_includes_present_metrics(self) -> None:
        stats = _build_agent_run_stats(elapsed_seconds=130.2, tokens_in=1024, tokens_out=None, cost_usd=None)

        self.assertEqual(stats["elapsed_seconds"], 130)
        self.assertEqual(stats["elapsed"], "2m 10s")
        self.assertEqual(stats["tokens_in"], 1024)
        self.assertNotIn("tokens_out", stats)
        self.assertNotIn("cost_usd", stats)

    def test_record_agent_run_stats_writes_to_existing_dict(self) -> None:
        run_stats = {"old": "value"}

        with patch("scripts.run_github_issues_to_opencode.time.monotonic", return_value=150.0):
            recorded = _record_agent_run_stats(
                run_stats=run_stats,
                start=95.0,
                tokens_in=123,
                tokens_out=456,
                cost_usd=0.12,
            )

        self.assertIs(recorded, run_stats)
        self.assertEqual(recorded, {
            "elapsed_seconds": 55,
            "elapsed": "55s",
            "tokens_in": 123,
            "tokens_out": 456,
            "cost_usd": 0.12,
        })


if __name__ == "__main__":
    unittest.main()
