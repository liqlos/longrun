"""A rejected verdict is discarded whole. Two of fifteen went that way in one measured night — $10.35 of
judgement thrown away on a citation technicality, and each rejection also bought a repair round because a
missing verdict falls into the repair branch. The model has already done the reading, so one corrected turn
on a warm cache is far cheaper than re-running the round. The retry must not be able to argue a FAIL into a
PASS, which is the only way this could become a way of buying verdicts."""
from __future__ import annotations
from helpers import LongrunTestCase


class EvaluatorRetryTest(LongrunTestCase):
    def _run(self, mode: str):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": mode, "criteria": ["C1", "C2"]})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        from longrun.store import RunStore
        from longrun.paths import runs_root
        run = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()][0]
        st = RunStore(run)
        return r, st, st.read(), [e["kind"] for e in st.events()]

    def test_a_citation_mistake_is_retried_and_the_verdict_survives(self):
        r, st, s, kinds = self._run("cite_badly_once")
        self.assertIn("evaluation.retry", kinds)
        self.assertIn("evaluation.applied", kinds)
        self.assertEqual(s["status"], "PASSED", r.stdout + r.stderr)

    def test_the_retry_may_not_upgrade_a_verdict_it_first_refused(self):
        r, st, s, kinds = self._run("upgrade_on_retry")
        self.assertIn("evaluation.retry", kinds)
        self.assertIn("evaluation.retry_rejected", kinds)
        # The retry's PASS must be thrown away rather than applied: that evaluation yields no verdict at all,
        # so the round falls into repair. (A later, honestly-earned PASS in the repair round is fine — what
        # must not happen is the upgraded verdict itself closing the run.)
        rejected = [e for e in st.events() if e["kind"] == "evaluation.retry_rejected"]
        applied_ids = {e["data"]["id"] for e in st.events() if e["kind"] == "evaluation.applied"}
        self.assertTrue(rejected)
        self.assertNotIn(rejected[0]["data"]["id"], applied_ids)
        self.assertIn("repair.scheduled", kinds)

    def test_a_clean_verdict_is_not_retried(self):
        r, st, s, kinds = self._run("pass")
        self.assertNotIn("evaluation.retry", kinds)
        self.assertEqual(s["status"], "PASSED", r.stdout + r.stderr)
