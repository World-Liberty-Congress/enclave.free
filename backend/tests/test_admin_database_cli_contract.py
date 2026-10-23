"""Check live-harness assertions without a model, Docker or credentials."""

import importlib.util
from pathlib import Path
import unittest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts/tests/TOOLS/check_admin_database_questions.py"
)
spec = importlib.util.spec_from_file_location("admin_database_cli", SCRIPT)
assert spec is not None and spec.loader is not None
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class RankingAssertionsTest(unittest.TestCase):
    def test_requires_ordered_ids_and_both_usage_metrics_without_names(self) -> None:
        rows = [
            f"User ID: {cli.NAMES.index(name) + 1}; recorded turns: {turns}; conversations: {conversations}"
            for name, turns, conversations in cli.EXPECTED
        ]
        cli.validate_ranking_answer("\n".join(rows))
        for wrong in [
            "Recorded turns are the metric.",
            "\n".join(reversed(rows)),
            "\n".join(rows[:-1]),
            "\n".join(rows).replace("turns: 17", "turns: 18"),
            "\n".join(rows).replace("conversations: 2", "conversations: 3"),
            "\n".join(rows).replace("ID: 1", "ID: 99"),
            "\n".join(rows) + " aVeRy",
        ]:
            with self.subTest(answer=wrong), self.assertRaises(AssertionError):
                cli.validate_ranking_answer(wrong)
