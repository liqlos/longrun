"""The strategic fallback must actually fire, not merely resolve.

Measured on the night of 17-18 Aug: strategic roles ran on fable/medium until 21:58, when fable hit its own
usage limit and burned eight planner attempts in run edc64db5. Everything from 21:59 onward ran on opus — but
no automatic mechanism did that; the model table was changed by hand. The failover added afterwards could not
have fired anyway, because the provider's per-model message ("You've reached your Fable 5 limit…") matched
neither "usage limit" nor a reset time in the detector. Resolution was tested; firing was not.
"""
from __future__ import annotations
from helpers import LongrunTestCase


class FailoverFiresTest(LongrunTestCase):
    def test_a_per_model_limit_moves_the_strategic_role_to_the_fallback(self):
        repo = self.repo()
        # the planner/evaluator resolve to fable/medium; fable refuses, opus must take over
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"], "limit_model": "fable"})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        from longrun.store import RunStore
        from longrun.paths import runs_root
        run = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()][0]
        st = RunStore(run)
        fo = [e for e in st.events() if e["kind"] == "session.model_failover"]
        self.assertTrue(fo, "no failover event; the run gave up on the limited model instead of switching\n"
                            + r.stdout + r.stderr)
        self.assertEqual(fo[0]["data"]["from"], "fable")
        self.assertEqual(fo[0]["data"]["to"], "opus")
        self.assertEqual(fo[0]["data"]["effort"], "medium")
        # and the run still reaches a terminal state rather than dying of the limit
        self.assertEqual(st.read()["status"], "PASSED", r.stdout + r.stderr)

    def test_the_launch_after_the_failover_uses_the_fallback_model(self):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"], "limit_model": "fable"})
        self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        from longrun.store import RunStore
        from longrun.paths import runs_root
        run = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()][0]
        st = RunStore(run)
        strategic = [e["data"] for e in st.events() if e["kind"] == "session.launch"
                     and e["data"].get("role") in ("planner", "evaluator", "restart_manager")]
        self.assertTrue(strategic)
        # nothing strategic may have completed on the limited model
        self.assertTrue(any(d.get("model") == "opus" for d in strategic),
                        f"strategic roles never moved to opus: {strategic}")
        # and the journal must say what actually ran: the failover raises effort too, and a launch event that
        # kept reporting the pre-failover effort is how a night's real model history became unreadable
        after = [d for d in strategic if d.get("model") == "opus"]
        self.assertTrue(all(d.get("effort") == "medium" for d in after),
                        f"launch events misreport the effort the fallback ran at: {after}")
