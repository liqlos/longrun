"""`longrun go`: one sentence in -> auto-planned (planner retries once on a rejected contract) -> bounded run."""
from __future__ import annotations
import json
from helpers import LongrunTestCase, LONGRUN_BIN


class TestGo(LongrunTestCase):
    def test_weekly_quota_from_plain_run_finishes_state(self):
        repo = self.repo()
        spec = self.tmp / "contract.json"
        spec.write_text(json.dumps({"goal": "make feature.txt exist", **self.contract_spec()}))
        planned = self.cli("plan", "--project", str(repo), "--contract", str(spec))
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        run_id = planned.stdout.split("run ", 1)[1].split(" ", 1)[0]
        self.set_mode({"weekly_limit_role": "builder"})
        result = self.cli("run", "--run", run_id)
        self.assertNotEqual(result.returncode, 0)
        from longrun.store import RunStore
        st = RunStore(run_id)
        self.assertEqual(st.read()["status"], "FAILED")
        self.assertEqual([e["kind"] for e in st.events()].count("run.finished"), 1)

    def test_weekly_quota_stops_session_retries_and_the_outer_chain(self):
        repo = self.repo()
        self.set_mode({"weekly_limit": True})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist", "--chain", "3")
        self.assertNotEqual(r.returncode, 0)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        runs = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()]
        self.assertEqual(len(runs), 1, r.stdout + r.stderr)
        st = RunStore(runs[0])
        kinds = [e["kind"] for e in st.events()]
        self.assertEqual(kinds.count("session.launch"), 1, kinds)
        self.assertNotIn("session.infra_wait", kinds)
        self.assertIn("session.terminal_quota", kinds)
        self.assertEqual(st.read()["status"], "FAILED")
        self.assertNotIn("[go 2/3]", r.stdout)

    def test_weekly_quota_from_intent_review_is_terminal(self):
        repo = self.repo()
        self.set_mode({"weekly_limit_role": "intent_reviewer"})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist", "--chain", "3")
        self.assertNotEqual(r.returncode, 0)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        runs = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()]
        self.assertEqual(len(runs), 1, r.stdout + r.stderr)
        st = RunStore(runs[0])
        launches = [e["data"]["role"] for e in st.events() if e["kind"] == "session.launch"]
        self.assertEqual(launches, ["planner", "intent_reviewer"])
        self.assertEqual(st.read()["status"], "FAILED")
        self.assertNotIn("[go 2/3]", r.stdout)

    def test_weekly_quota_from_restart_manager_is_terminal(self):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": "fail", "weekly_limit_role": "restart_manager",
                       "max_fresh_restarts": 1})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist", "--chain", "3")
        self.assertNotEqual(r.returncode, 0)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        runs = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()]
        self.assertEqual(len(runs), 1, r.stdout + r.stderr)
        st = RunStore(runs[0])
        launches = [e["data"]["role"] for e in st.events() if e["kind"] == "session.launch"]
        self.assertIn("restart_manager", launches)
        self.assertEqual(st.read()["status"], "FAILED")
        self.assertNotIn("[go 2/3]", r.stdout)

    def test_go_autoplans_and_passes(self):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"], "planner": "bad_first"})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("-> PASSED", r.stdout)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        run = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()][0]
        st = RunStore(run); s = st.read(); ev = [e["kind"] for e in st.events()]
        self.assertIn("plan.auto.rejected", ev); self.assertIn("plan.auto.accepted", ev)   # user_facing+check-only was refused, then fixed
        self.assertEqual(s["status"], "PASSED"); self.assertEqual(s["permission_mode"], "bypassPermissions")
        launches = [e for e in st.events() if e["kind"] == "session.launch"]
        self.assertEqual({l["data"]["role"] for l in launches}, {"planner", "intent_reviewer", "builder", "evaluator"})
        # the once-per-outcome directional call and the per-round judgement are deliberately on different
        # models now; asserting one model for both is what the routing change exists to stop
        by_role = {l["data"]["role"]: l["data"] for l in launches}
        self.assertEqual(by_role["planner"]["model"], "fable")
        self.assertEqual(by_role["evaluator"]["model"], "opus")
        self.assertTrue(all(l["data"]["effort"] == "medium" for l in launches), [l["data"] for l in launches])
        cmd = next(st.sessions_dir.glob("*.builder.cmd.txt")).read_text()
        self.assertIn("bypassPermissions", cmd)
        ecmd = next(st.sessions_dir.glob("*.evaluator.cmd.txt")).read_text()
        self.assertNotIn("bypassPermissions", ecmd); self.assertIn("dontAsk", ecmd)

    def test_intent_reviewer_blocks_diluted_contract_before_builder_then_planner_retries(self):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"],
                       "intent_review": "reject_first"})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist and keep the readme")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        run = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()][0]
        st = RunStore(run)
        kinds = [e["kind"] for e in st.events()]
        self.assertIn("plan.intent_review.rejected", kinds)
        self.assertIn("plan.intent_review.accepted", kinds)
        launches = [e["data"]["role"] for e in st.events() if e["kind"] == "session.launch"]
        self.assertEqual(launches.count("planner"), 1)
        self.assertEqual(launches.count("contract_repair"), 1)
        self.assertEqual(launches.count("intent_reviewer"), 2)
        self.assertLess(launches.index("intent_reviewer"), launches.index("builder"))
        self.assertEqual(json.loads((st.dir / "contract.spec.json").read_text())["constraints"], ["KEEP_SENTINEL"])

    def test_exhausted_contract_repairs_stop_outer_chain(self):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"],
                       "intent_review": "reject_always"})
        r = self.cli("go", "--project", str(repo), "--goal", "make feature.txt exist", "--chain", "3")
        self.assertNotEqual(r.returncode, 0)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        runs = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()]
        self.assertEqual(len(runs), 1, r.stdout + r.stderr)
        st = RunStore(runs[0])
        self.assertEqual(st.read()["status"], "FAILED")
        launches = [e["data"]["role"] for e in st.events() if e["kind"] == "session.launch"]
        self.assertEqual(launches.count("planner"), 1)
        self.assertEqual(launches.count("contract_repair"), 2)
        self.assertEqual(launches.count("intent_reviewer"), 3)
        self.assertNotIn("builder", launches)
        self.assertNotIn("[go 2/3]", r.stdout)

    def test_sourced_owner_objection_pauses_before_builder(self):
        repo = self.repo()
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"],
                       "intent_review": "owner_conflict"})
        r = self.cli("go", "--project", str(repo), "--goal", "use the unsafe route", "--chain", "3")
        self.assertNotEqual(r.returncode, 0)
        from longrun.store import RunStore
        from longrun.paths import runs_root
        runs = [d.name for d in runs_root().iterdir() if (d / "state.json").is_file()]
        self.assertEqual(len(runs), 1, r.stdout + r.stderr)
        self.assertNotIn("[go 2/3]", r.stdout)
        run = runs[0]
        st = RunStore(run)
        self.assertEqual(st.read()["status"], "OWNER_JUDGMENT_REQUIRED")
        self.assertTrue((st.dir / "owner-confirmation-required.json").is_file())
        kinds = [e["kind"] for e in st.events()]
        self.assertIn("plan.intent_review.owner_confirmation_required", kinds)
        launches = [e["data"]["role"] for e in st.events() if e["kind"] == "session.launch"]
        self.assertNotIn("builder", launches)


if __name__ == "__main__":
    import unittest
    unittest.main()
