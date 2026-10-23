"""DONE WHEN: the baseline score is recorded on the scoreboard."""
import unittest
from pathlib import Path

from relaybench import run, scoreboard
from tests.helpers import World

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay" / "day01.jsonl"


class TestDay01(unittest.TestCase):
    def test_baseline_recorded_on_scoreboard(self):
        w = World()
        try:
            summary = run.run(w.url, "replay", "day01-baseline", w.bench, FIXTURES, w.scoreboard)
            hist = scoreboard.history(w.scoreboard)
            self.assertEqual(hist[-1]["label"], "day01-baseline")
            for m in scoreboard.METRICS:
                self.assertIn(m, hist[-1])
            self.assertEqual(summary["tasks"], 250)
            self.assertEqual(summary["infra_errors"], 0)
            # The replay fixtures make the baseline exactly reproducible.
            self.assertEqual(summary["task_success"], 0.36)
            self.assertEqual(summary["claimed_but_not_done"], 60)
        finally:
            w.close()

    def test_faults_show_up_as_infra_errors_not_agent_errors(self):
        import json, urllib.request
        w = World()
        try:
            req = urllib.request.Request(w.url + "/_faults", method="POST",
                                         data=json.dumps({"mode": "500", "rate": 0.3}).encode())
            urllib.request.urlopen(req).read()
            summary = run.run(w.url, "replay", "fault", w.bench, FIXTURES, w.scoreboard)
            self.assertGreater(summary["infra_errors"], 0)
            self.assertLess(summary["task_success"], 0.36)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
