"""`longrun cost` reads what the child sessions reported. It must never invent a number."""
from __future__ import annotations
import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _session(run_dir: Path, sid: str, role: str, *, cost: float | None, out: int = 0,
             cache_read: int = 0, cache_write: int = 0) -> None:
    sess = run_dir / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "assistant", "message": {"content": []}})]
    if cost is not None:
        lines.append(json.dumps({"type": "result", "total_cost_usd": cost, "duration_ms": 60000,
                                 "usage": {"output_tokens": out, "cache_read_input_tokens": cache_read,
                                           "cache_creation_input_tokens": cache_write}}))
    (sess / f"{sid}.{role}.stream.jsonl").write_text("\n".join(lines) + "\n")


class CostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); os.environ["LONGRUN_HOME"] = str(self.tmp)
        self.proj = self.tmp / "proj"; self.proj.mkdir()
        self.runs = self.tmp / "runs"; self.runs.mkdir()

    def _run(self, run_id: str, project: Path, status: str = "PASSED") -> Path:
        d = self.runs / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"run_id": run_id, "status": status,
                                                  "project_root": str(project)}))
        return d

    def test_totals_are_grouped_by_role(self):
        from longrun import cost as C
        d = self._run("aaaaaaaa-1", self.proj)
        _session(d, "s1", "builder", cost=10.0, out=100, cache_read=5000, cache_write=200)
        _session(d, "s2", "builder", cost=5.0, out=50)
        _session(d, "s3", "evaluator", cost=2.0, out=10)
        roles, per_run = C.by_role([d])
        self.assertEqual(roles["builder"]["calls"], 2)
        self.assertAlmostEqual(roles["builder"]["cost"], 15.0)
        self.assertEqual(roles["builder"]["cache_read"], 5000)
        self.assertAlmostEqual(roles["evaluator"]["cost"], 2.0)
        self.assertAlmostEqual(per_run[0][1], 17.0)

    def test_a_session_that_died_before_reporting_is_counted_as_a_call_at_zero(self):
        """Dropping it would understate how many calls a night made — which is the number the whole
        report exists to expose."""
        from longrun import cost as C
        d = self._run("bbbbbbbb-1", self.proj)
        _session(d, "s1", "evaluator", cost=None)
        roles, _ = C.by_role([d])
        self.assertEqual(roles["evaluator"]["calls"], 1)
        self.assertEqual(roles["evaluator"]["cost"], 0.0)

    def test_runs_are_filtered_by_project_and_by_id_prefix(self):
        from longrun import cost as C
        other = self.tmp / "other"; other.mkdir()
        a = self._run("aaaaaaaa-1", self.proj)
        self._run("cccccccc-1", other)
        self.assertEqual([p.name for p in C.find_runs(project=self.proj.resolve())], [a.name])
        self.assertEqual(len(C.find_runs(project=None)), 2)
        self.assertEqual([p.name for p in C.find_runs(project=None, run_prefix="cccc")], ["cccccccc-1"])

    def test_report_renders_without_sessions(self):
        from longrun import cost as C
        d = self._run("dddddddd-1", self.proj)
        self.assertIn("no child sessions", C.report([d]))

    def test_a_role_whose_driver_reports_no_cost_shows_n_a_not_zero(self):
        """The codex driver reports no cost at all. Printing those runs as "$0.00" would present an
        unavailable number as a cheap one — the one thing a report that claims never to estimate must not do."""
        from longrun import cost as C
        d = self._run("eeeeeeee-1", self.proj)
        _session(d, "s1", "evaluator", cost=None, out=100)
        text = C.report([d])
        self.assertIn("n/a", text)
        self.assertNotIn("0.00", text.splitlines()[3])

    def test_a_run_that_does_not_name_its_project_is_not_matched_by_the_current_directory(self):
        """An empty project_root resolves to the process cwd, so such a run would silently attach itself to
        whatever project the report was invoked from."""
        from longrun import cost as C
        d = self.runs / "ffffffff-1"; d.mkdir()
        (d / "state.json").write_text(json.dumps({"run_id": "ffffffff-1", "status": "PASSED", "project_root": ""}))
        self.assertEqual(C.find_runs(project=Path.cwd().resolve()), [])


if __name__ == "__main__":
    unittest.main()
