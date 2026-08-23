"""Runs must leave a trace the next planner can read.

Every planner is a fresh context, so without this each one starts cold. Measured consequence on one
autonomous night: of ten outcomes that landed, seven were repairs of damage earlier autonomous batches had
left, and nothing anywhere noticed the pattern — failure signatures and loop counters live inside a single
run's store and die with it.

The ledger is deliberately data, not a verdict: no model is called and nothing is scored.
"""
from __future__ import annotations
import json
from pathlib import Path

from helpers import LongrunTestCase
from longrun.planner import recent_history


class HistoryTest(LongrunTestCase):
    def test_a_finished_run_appends_one_row_to_the_project_ledger(self):
        repo = self.repo()
        (repo / ".longrun").mkdir(exist_ok=True)
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"]})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        p = repo / ".longrun/history.jsonl"
        self.assertTrue(p.is_file(), r.stdout + r.stderr)
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "PASSED")
        for k in ("run_id", "outcome", "criteria", "cost_usd", "rounds", "wall_minutes"):
            self.assertIn(k, row)

    def test_the_ledger_is_never_a_reason_to_fail_a_run(self):
        """A convenience must not be able to break the thing it serves: if the ledger cannot be written,
        the run still reaches its terminal status normally."""
        repo = self.repo()
        d = repo / ".longrun"
        d.mkdir(exist_ok=True)
        (d / "history.jsonl").mkdir()          # a directory where the file should be: any write will fail
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"]})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("-> PASSED", r.stdout)


class RecentHistoryRenderTest(LongrunTestCase):
    def test_it_renders_nothing_when_there_is_no_ledger(self):
        self.assertEqual(recent_history(self.tmp), "")

    def test_it_summarises_rows_and_says_how_to_use_them(self):
        d = self.tmp / ".longrun"
        d.mkdir(parents=True, exist_ok=True)
        (d / "history.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"ended_at": "2026-08-18T02:00:00Z", "status": "PASSED", "outcome": "the city moves",
             "criteria": {"C1": "PASS"}, "cost_usd": 12.0, "rounds": 2, "repairs": 0, "wall_minutes": 40},
            {"ended_at": "2026-08-18T03:00:00Z", "status": "RESET_RECOMMENDED", "outcome": "prove motion",
             "criteria": {"C1": "FAIL"}, "cost_usd": 43.7, "rounds": 4, "repairs": 2, "wall_minutes": 106,
             "reason": "stills cannot show motion", "surviving_facts": ["two stills of one pose prove nothing"]},
        ]) + "\n")
        out = recent_history(self.tmp)
        self.assertIn("the city moves", out)
        self.assertIn("RESET_RECOMMENDED", out)
        self.assertIn("stills cannot show motion", out)
        self.assertIn("two stills of one pose prove nothing", out)
        self.assertIn("do not re-propose an outcome that just failed the same way", out)
        self.assertIn("Nothing here overrides the owner goal", out)

    def test_it_keeps_only_the_most_recent_entries(self):
        d = self.tmp / ".longrun"
        d.mkdir(parents=True, exist_ok=True)
        rows = [{"ended_at": f"2026-08-18T0{i}:00:00Z", "status": "PASSED", "outcome": f"outcome number {i}",
                 "criteria": {"C1": "PASS"}, "cost_usd": 1.0, "rounds": 1, "repairs": 0, "wall_minutes": 5}
                for i in range(9)]
        (d / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = recent_history(self.tmp, limit=3)
        self.assertIn("outcome number 8", out)
        self.assertNotIn("outcome number 5", out)
