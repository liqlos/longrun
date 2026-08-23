"""Security and correctness regression suite. Each case maps to a reproduced old-bundle failure or a required property."""
from __future__ import annotations
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from helpers import LongrunTestCase, LONGRUN_BIN, SRC


def _worker(run_id: str, home: str, n: int):
    os.environ["LONGRUN_HOME"] = home
    sys.path.insert(0, str(SRC))
    from longrun.store import RunStore
    st = RunStore(run_id)
    for _ in range(n):
        with st.transaction() as s:
            s["counters"]["rounds"] += 1


class TestSecurity(LongrunTestCase):
    def test_child_runner_cleans_observed_descendant_that_clears_marker(self):
        from longrun.process import ChildRunner, pid_alive

        pid_file = self.tmp / "escaped.pid"
        parent_code = (
            "import os,subprocess,sys,time; "
            "env=dict(os.environ); env.pop('LONGRUN_SESSION_MARKER',None); "
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "env=env,start_new_session=True); "
            f"open({str(pid_file)!r},'w').write(str(c.pid)); "
            "time.sleep(1)"
        )
        runner = ChildRunner()
        result = runner.run(
            [sys.executable, "-c", parent_code], cwd=self.tmp, env=dict(os.environ),
            timeout_s=5, stdout_path=self.tmp / "out.log", stderr_path=self.tmp / "err.log")
        self.assertEqual(result.exit_code, 0)
        escaped_pid = int(pid_file.read_text())
        deadline = time.time() + 3
        while pid_alive(escaped_pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(escaped_pid))

    def test_environment_marker_cleans_setsid_escape(self):
        from longrun.process import cleanup_processes_with_env_marker, processes_with_env_marker

        marker = f"test:{os.getpid()}:{time.time_ns()}"
        env = dict(os.environ); env["LONGRUN_SESSION_MARKER"] = marker
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=env, start_new_session=True)
        try:
            deadline = time.time() + 3
            while child.pid not in processes_with_env_marker(marker) and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn(child.pid, processes_with_env_marker(marker))
            cleaned = cleanup_processes_with_env_marker(marker, grace=0.2)
            self.assertIn(child.pid, cleaned)
            child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=3)

    def _frozen(self, spec=None, **kw):
        from longrun.controller import freeze_run
        st, repo = self.make_run(spec, **kw)
        freeze_run(st)
        with st.transaction() as s:
            s["status"] = "RUNNING"
        return st, repo

    def _valid_verdict(self, st, overrides=None, crit_overrides=None):
        from longrun.evidence import evidence_manifest
        from longrun import gitutil as G
        s = st.read(); ws = Path(s["workspace"]); rev = G.content_revision(ws)
        man = evidence_manifest(st, revision=rev)
        c = json.loads(st.contract_path().read_text())
        crits = []
        for x in c["criteria"]:
            eids = [m["id"] for m in man if x["id"] in m["criterion_ids"]]
            crits.append({"id": x["id"], "verdict": "PASS" if eids else "FAIL", "evidence_ids": eids, "reason": "because evidence"})
        obj = {"run_id": st.run_id, "contract_hash": s["contract_hash"], "evaluated_revision": rev, "criteria": crits,
               "overall": "PASS" if all(k["verdict"] == "PASS" for k in crits) else "NEEDS_REWORK",
               "failure_signature": "", "recommended_next_strategy": "n/a"}
        obj.update(overrides or {})
        if crit_overrides:
            for k in obj["criteria"]:
                k.update(crit_overrides)
        return obj, man, rev, c, s

    def test_fresh_restart_never_resets_a_dirty_in_place_baseline(self):
        from longrun.controller import fresh_restart
        from longrun.process import ChildRunner
        repo = self.repo()
        (repo / "owner-work.txt").write_text("preserve me\n")
        st, _ = self.make_run(isolation="none", repo=repo)
        contract = json.loads(st.contract_path().read_text())
        self.assertEqual(fresh_restart(st, ChildRunner(), contract, {}, None), "APPLY")
        self.assertEqual((repo / "owner-work.txt").read_text(), "preserve me\n")
        self.assertFalse(list(st.artifacts_dir.glob("interrupted-diff-*.patch")))
        launches = [e for e in st.events() if e["kind"] == "session.launch"]
        self.assertEqual(launches, [])

    def test_fresh_restart_is_non_destructive_when_patch_capture_fails(self):
        from unittest.mock import patch
        from longrun.controller import fresh_restart
        from longrun.process import ChildRunner
        st, _ = self._frozen()
        ws = Path(st.read()["workspace"])
        (ws / "work.txt").write_text("keep\n")
        contract = json.loads(st.contract_path().read_text())
        with patch("longrun.controller.G.save_patch", return_value=False):
            self.assertEqual(fresh_restart(st, ChildRunner(), contract, {}, None), "APPLY")
        self.assertEqual((ws / "work.txt").read_text(), "keep\n")
        launches = [e for e in st.events() if e["kind"] == "session.launch"]
        self.assertEqual(launches, [])

    # 1. structured field disagrees with text -> rejected (no --verdict flag exists; extra keys rejected)
    def test_verdict_text_vs_field_rejected(self):
        from longrun.evaluator import extract_json_object, validate_verdict, EvaluatorError
        st, _ = self._frozen()
        obj, man, rev, c, s = self._valid_verdict(st)
        with self.assertRaises(EvaluatorError):
            extract_json_object("VERDICT: worse\n" + json.dumps(obj))
        with self.assertRaises(EvaluatorError):
            validate_verdict({**obj, "verdict": "better"}, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        r = self.cli("evaluate", "--run", st.run_id, "--verdict", "better")
        self.assertNotEqual(r.returncode, 0); self.assertIn("unrecognized arguments", r.stderr)

    # 2/aggregate: one aggregate verdict cannot close unrelated criteria
    def test_aggregate_verdict_rejected(self):
        from longrun.evaluator import validate_verdict, EvaluatorError
        from longrun.evidence import record_evidence
        st, _ = self._frozen(self.contract_spec(n=2))
        s = st.read(); ws = Path(s["workspace"])
        from longrun import gitutil as G
        rev = G.content_revision(ws)
        e1 = record_evidence(st, kind="check", criterion_ids=["C1"], summary="check for C1 only", revision=rev, submitted_by="S1", command="true", exit_code=0)
        obj, man, rev, c, s = self._valid_verdict(st)
        for k in obj["criteria"]:
            k["verdict"] = "PASS"; k["evidence_ids"] = [e1["id"]]
        obj["overall"] = "PASS"
        with self.assertRaises(EvaluatorError) as cm:
            validate_verdict(obj, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        self.assertIn("aggregate", str(cm.exception))

    # builder cannot set PASS / mutate state / call evaluate; tamper is detected
    def test_builder_cannot_certify_or_mutate(self):
        from longrun.controller import run_loop
        st, repo = self._frozen()
        self.set_mode({"builder": "self_pass", "evaluator": "rework"})
        with st.transaction() as s:
            s["status"] = "FROZEN"
        run_loop(st.run_id)
        s = st.read()
        self.assertEqual(s["criteria"]["C1"]["status"], "FAIL")
        # the builder's `longrun evaluate` was refused (exit 3) and evidence with another run id rejected
        streams = list(st.sessions_dir.glob("*.builder.stream.jsonl"))
        txt = "\n".join(p.read_text() for p in streams)
        self.assertIn("cannot invoke it", txt); self.assertIn("does not match run", txt)
        st2, _ = self._frozen()
        self.set_mode({"builder": "tamper", "evaluator": "pass"})
        with st2.transaction() as s:
            s["status"] = "FROZEN"
        status = run_loop(st2.run_id)
        self.assertEqual(status, "FAILED")
        self.assertTrue(any(e["kind"] == "state.tamper_detected" for e in st2.events()))
        self.assertFalse(any(e["kind"] == "evaluation.applied" for e in st2.events()))

    def test_no_mark_done_command(self):
        r = self.cli("mark-done"); self.assertNotEqual(r.returncode, 0)
        r = self.cli("--help"); self.assertNotIn("mark-done", r.stdout)

    # criterion without required evidence stays false; user-facing cannot pass on tests only
    def test_evidence_requirements_enforced(self):
        from longrun.evaluator import validate_verdict, EvaluatorError
        from longrun.evidence import record_evidence
        from longrun import gitutil as G
        from longrun.contract import ContractError, new_contract
        with self.assertRaises(ContractError):
            new_contract(run_id="x", project_root="/", adapter="ui_visual", observable_end_state="a visible thing is visible to a stranger",
                         criteria=[{"id": "C1", "statement": "button is visibly red on screen", "kind": "user_facing", "evidence_requirements": ["test"]}])
        spec = self.contract_spec(kind="visual", checks=False)
        st, _ = self._frozen(spec)
        s = st.read(); rev = G.content_revision(Path(s["workspace"]))
        e = record_evidence(st, kind="test", criterion_ids=["C1"], summary="unit tests pass", revision=rev, submitted_by="S1", command="pytest", exit_code=0)
        obj, man, rev, c, s = self._valid_verdict(st)
        obj["criteria"][0].update({"verdict": "PASS", "evidence_ids": [e["id"]]}); obj["overall"] = "PASS"
        with self.assertRaises(EvaluatorError) as cm:
            validate_verdict(obj, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        self.assertIn("requires evidence of kind", str(cm.exception))
        obj["criteria"][0].update({"verdict": "PASS", "evidence_ids": []})
        with self.assertRaises(EvaluatorError):
            validate_verdict(obj, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        self.assertEqual(st.read()["criteria"]["C1"]["status"], "FAIL")

    def test_stale_and_foreign_evidence_rejected(self):
        from longrun.evidence import record_evidence, EvidenceError, evidence_manifest
        from longrun import gitutil as G
        st, _ = self._frozen()
        s = st.read(); ws = Path(s["workspace"]); rev = G.content_revision(ws)
        with self.assertRaises(EvidenceError):
            record_evidence(st, kind="check", criterion_ids=["C1"], summary="stale evidence", revision="deadbeef+clean", submitted_by="S1", current_revision=rev)
        with self.assertRaises(EvidenceError):
            record_evidence(st, kind="check", criterion_ids=["C1"], summary="foreign run evidence", revision=rev, submitted_by="S1", expected_run_id="00000000-0000-0000-0000-000000000000")
        with self.assertRaises(EvidenceError):
            record_evidence(st, kind="check", criterion_ids=["C1"], summary="stale contract", revision=rev, submitted_by="S1", expected_contract_hash="0" * 64)
        with self.assertRaises(EvidenceError):
            record_evidence(st, kind="bogus", criterion_ids=["C1"], summary="unsupported kind", revision=rev, submitted_by="S1")
        with self.assertRaises(EvidenceError):
            record_evidence(st, kind="check", criterion_ids=["NOPE"], summary="unknown criterion", revision=rev, submitted_by="S1")
        ok = record_evidence(st, kind="check", criterion_ids=["C1"], summary="good one at rev", revision=rev, submitted_by="S1")
        (ws / "x.txt").write_text("edit after evidence")
        rev2 = G.content_revision(ws)
        self.assertNotEqual(rev, rev2)
        self.assertNotIn(ok["id"], [m["id"] for m in evidence_manifest(st, revision=rev2)])

    def test_evaluator_output_strictness(self):
        from longrun.evaluator import validate_verdict, EvaluatorError
        st, _ = self._frozen(self.contract_spec(n=2))
        obj, man, rev, c, s = self._valid_verdict(st)
        def bad(**kw):
            o = json.loads(json.dumps(obj)); o.update(kw); return o
        with self.assertRaises(EvaluatorError): validate_verdict(bad(contract_hash="0" * 64), run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        with self.assertRaises(EvaluatorError): validate_verdict(bad(run_id="other"), run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        with self.assertRaises(EvaluatorError): validate_verdict(bad(evaluated_revision="old"), run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        o = bad(); o["criteria"].append({"id": "ZZ", "verdict": "PASS", "evidence_ids": [], "reason": "made up"})
        with self.assertRaises(EvaluatorError): validate_verdict(o, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        o = bad(); o["criteria"].append(dict(o["criteria"][0]))
        with self.assertRaises(EvaluatorError): validate_verdict(o, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        o = bad(); o["criteria"] = o["criteria"][:1]
        with self.assertRaises(EvaluatorError): validate_verdict(o, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        with self.assertRaises(EvaluatorError): validate_verdict(bad(override="PASS"), run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)
        o = bad(); o["criteria"][0]["evidence_ids"] = ["Enotinmanifest"]; o["criteria"][0]["verdict"] = "PASS"
        with self.assertRaises(EvaluatorError): validate_verdict(o, run_id=st.run_id, contract_hash=s["contract_hash"], evaluated_revision=rev, contract=c, evidence_manifest=man)

    # baseline before edits
    def test_baseline_after_edits_refused(self):
        from longrun.controller import freeze_run, ControllerError
        # A live project moves under the planner (a sidecar writing artifacts, the owner's editor). Nothing of
        # the run has executed yet, so the baseline is simply re-taken at the freeze point and both revisions
        # are recorded — the guarantee is "baseline before any BUILDER edit", not "no file may move at all".
        st, repo = self.make_run(isolation="none")
        before = st.read()["start_content_revision"]
        (repo / "README.md").write_text("edited before freeze\n")
        freeze_run(st)
        s = st.read()
        self.assertEqual(s["status"], "FROZEN")
        reb = [e for e in st.events() if e["kind"] == "baseline.rebased"]
        self.assertEqual(len(reb), 1)
        self.assertEqual(reb[0]["data"]["from"], before)
        self.assertNotEqual(reb[0]["data"]["to"], before)
        # But once a builder has run, an edit before freeze is exactly the pre-cooking this guard exists for.
        st_b, repo_b = self.make_run(isolation="none")
        st_b.append_event("session.launch", {"role": "builder", "session_id": "x"})
        (repo_b / "README.md").write_text("edited after a builder ran\n")
        with self.assertRaises(ControllerError):
            freeze_run(st_b)
        self.assertEqual(st_b.read()["status"], "PLANNED")
        st2, repo2 = self.make_run(isolation="none")
        subprocess.run(["git", "commit", "-qam", "moved"], cwd=repo2)  # nothing to commit -> HEAD same; now really move HEAD
        (repo2 / "n.txt").write_text("x"); subprocess.run(["git", "add", "-A"], cwd=repo2); subprocess.run(["git", "commit", "-qm", "moved"], cwd=repo2)
        with self.assertRaises(ControllerError):
            freeze_run(st2)
        st3, repo3 = self.make_run()
        freeze_run(st3)
        s = st3.read()
        self.assertEqual(s["status"], "FROZEN"); self.assertTrue(s["baseline"]["revision"].endswith("+clean"))
        ev = [e for e in st3.events()]
        self.assertLess([e["kind"] for e in ev].index("run.frozen"), len(ev))
        self.assertTrue(all(e["kind"] != "session.launch" for e in ev))   # no editable execution before freeze

    def test_new_run_has_fresh_identity(self):
        st, repo = self._frozen()
        with st.transaction() as s:
            s["counters"]["rounds"] = 4; s["counters"]["repairs"] = 2; s["status"] = "RESET_RECOMMENDED"
        r = self.cli("reset", "--run", st.run_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        new_id = r.stdout.split("new run ")[1].split()[0]
        from longrun.store import RunStore
        n = RunStore(new_id).read()
        self.assertNotEqual(new_id, st.run_id)
        self.assertEqual((n["counters"]["rounds"], n["counters"]["repairs"], n["counters"]["fresh_restarts"]), (0, 0, 0))
        self.assertIsNone(n["baseline"]); self.assertEqual(n["parent_run_id"], st.run_id)
        self.assertNotEqual(RunStore(new_id).secret(), st.secret())

    def test_concurrent_updates_no_lost_update(self):
        st, _ = self._frozen()
        N, K = 8, 25
        ctx = multiprocessing.get_context("fork")
        ps = [ctx.Process(target=_worker, args=(st.run_id, str(self.home), K)) for _ in range(N)]
        [p.start() for p in ps]; [p.join(60) for p in ps]
        self.assertEqual(st.read()["counters"]["rounds"], N * K)
        seqs = [e["seq"] for e in st.events()]
        self.assertEqual(seqs, sorted(seqs)); self.assertEqual(len(seqs), len(set(seqs)))

    def test_stale_write_rejected(self):
        from longrun.store import StaleWrite
        st, _ = self._frozen()
        v = st.read()["version"]
        with st.transaction() as s:
            s["counters"]["rounds"] = 1
        with self.assertRaises(StaleWrite):
            with st.transaction(expected_version=v) as s:
                s["counters"]["rounds"] = 99

    def test_child_timeout_kills_process_group(self):
        from longrun.process import ChildRunner
        script = self.tmp / "sleeper.sh"; script.write_text("#!/bin/bash\nsleep 300 &\nsleep 300\n"); script.chmod(0o755)
        runner = ChildRunner()
        r = runner.run(["bash", str(script)], cwd=self.tmp, env=dict(os.environ), timeout_s=1.5, stdout_path=self.tmp / "o", stderr_path=self.tmp / "e")
        self.assertTrue(r.timed_out)
        time.sleep(0.5)
        left = subprocess.run(["pgrep", "-g", str(r.pgid)], capture_output=True, text=True).stdout.strip() if r.pgid else ""
        self.assertEqual(left, "")

    def test_ctrl_c_marks_interrupted_and_stops_child(self):
        st, repo = self._frozen()
        with st.transaction() as s:
            s["status"] = "FROZEN"
        self.set_mode({"builder": "sleep"})
        code = f"import os,sys; os.environ['LONGRUN_HOME']={str(self.home)!r}; sys.path.insert(0,{str(SRC)!r}); from longrun.controller import run_loop; print(run_loop({st.run_id!r}))"
        script = self.tmp / "ctl.py"; script.write_text(code)
        p = subprocess.Popen([sys.executable, str(script)], cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        deadline = time.time() + 30
        while time.time() < deadline:
            s = st.read()
            if any(c["role"] == "builder" and c.get("pid") for c in s["children"]):
                break
            time.sleep(0.2)
        child_pid = next(c["pid"] for c in st.read()["children"] if c["role"] == "builder")
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.assertIn("INTERRUPTED", out + err)
        self.assertEqual(st.read()["status"], "INTERRUPTED")
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_loop_guard_and_bounds(self):
        from longrun.controller import run_loop
        st, repo = self._frozen()
        with st.transaction() as s:
            s["status"] = "FROZEN"
        self.set_mode({"builder": "loop", "evaluator": "rework"})
        status = run_loop(st.run_id)
        s = st.read()
        self.assertIn(status, ("RESET_RECOMMENDED",))
        self.assertLessEqual(s["counters"]["repairs"], 2); self.assertLessEqual(s["counters"]["fresh_restarts"], 1)
        kinds = [e["kind"] for e in st.events()]
        self.assertIn("loop.operational", kinds); self.assertIn("loop.strategic", kinds); self.assertIn("restart.decision", kinds)
        self.assertIsNotNone(s["failure_capsule"]); self.assertNotIn("transcript", json.dumps(s["failure_capsule"]).lower())
        self.assertTrue(any(e["kind"] in ("evaluation.skipped_unchanged", "evaluation.skipped_no_evidence") for e in st.events()))   # no wasteful repeat evaluation

    def test_progress_resets_stagnation(self):
        from longrun.loopguard import strategic_check
        st = {"criteria": {"C1": {"status": "FAIL"}, "C2": {"status": "FAIL"}}, "loop": {"failure_signatures": ["x", "x"], "no_delta_checkpoints": 1, "last_criteria_fingerprint": "C1:FAIL|C2:FAIL"}}
        rep = strategic_check(st, verdict={"failure_signature": "x"}, delta={"passed": [], "regressed": []}, round_summary={"new_evidence": 1})
        self.assertTrue(rep.fired)
        st["criteria"]["C1"]["status"] = "PASS"
        rep2 = strategic_check(st, verdict={"failure_signature": "x"}, delta={"passed": ["C1"], "regressed": []}, round_summary={"new_evidence": 1})
        self.assertFalse(rep2.fired); self.assertEqual(st["loop"]["no_delta_checkpoints"], 0); self.assertEqual(st["loop"]["failure_signatures"], ["x"])

    def test_readonly_evaluator_mutation_blocked_and_recorded(self):
        from longrun.token import mint
        st, repo = self._frozen()
        with st.transaction() as s:
            s["children"].append({"session_id": "EV1", "role": "evaluator", "pid": 1, "pgid": 1, "started_at": "x", "ended_at": None, "exit": None})
        tok = mint(st.secret(), run_id=st.run_id, session_id="EV1", role="evaluator", controller_pid=os.getpid(), ttl_seconds=60)
        r = self.cli("hook", "pre-tool-use", env={"LONGRUN_TOKEN": tok}, stdin=json.dumps({"session_id": "EV1", "tool_name": "Write", "tool_input": {"file_path": "/x", "content": "y"}}))
        self.assertIn('"permissionDecision": "deny"', r.stdout)
        self.assertTrue(any(e["kind"] == "evaluator.mutation_attempt" for e in st.events()))
        r = self.cli("hook", "pre-tool-use", env={"LONGRUN_TOKEN": tok}, stdin=json.dumps({"session_id": "EV1", "tool_name": "Read", "tool_input": {"file_path": "/x"}}))
        self.assertEqual(r.stdout, "")
        # evaluator command line contains no write tools and no bypass
        from longrun.drivers.claude import ClaudeDriver
        cmd = ClaudeDriver().build_command(role="evaluator", prompt="p", session_id="s", cwd=repo, max_turns=5, allowed_commands=[], deny_paths=[], model=None, json_schema={"type": "object"}, max_budget_usd=None, permission_mode="acceptEdits")
        self.assertNotIn("bypassPermissions", cmd); self.assertIn("dontAsk", cmd)
        bcmd = ClaudeDriver().build_command(role="builder", prompt="p", session_id="s", cwd=repo, max_turns=5, allowed_commands=[], deny_paths=[], model=None, json_schema=None, max_budget_usd=None, permission_mode="acceptEdits")
        self.assertNotIn("bypassPermissions", bcmd); self.assertNotIn("--dangerously-skip-permissions", bcmd)

    def test_end_to_end_pass_with_stub(self):
        from longrun.controller import run_loop
        from longrun.store import RunStore
        st, repo = self._frozen(self.contract_spec(n=2))
        with st.transaction() as s:
            s["status"] = "FROZEN"
        self.set_mode({"builder": "submit", "evaluator": "pass", "criteria": ["C1", "C2"]})
        status = run_loop(st.run_id)
        s = st.read()
        self.assertEqual(status, "PASSED", s.get("terminal_reason"))
        self.assertEqual({k: v["status"] for k, v in s["criteria"].items()}, {"C1": "PASS", "C2": "PASS"})
        self.assertEqual(s["counters"]["rounds"], 1)
        # evidence is bound to run/contract/revision, session-attributed, and controller-verified transitions carry evaluation ids
        from longrun.evidence import list_evidence
        ev = [e for e in list_evidence(st) if e["submitted_by"] not in ("controller:baseline", "controller:evaluate")]
        self.assertTrue(ev and all(e["contract_hash"] == s["contract_hash"] and e["run_id"] == st.run_id for e in ev))
        self.assertTrue(all(h.get("evaluation_id") for h in s["criteria"]["C1"]["history"]))
        # the original repo was never modified (worktree isolation)
        self.assertFalse((repo / "feature.txt").exists())
        # settings.json untouched
        # ordinary hook during/after: no output
        r = self.cli("hook", "stop", stdin=json.dumps({"session_id": "owner", "cwd": str(repo)}))
        self.assertEqual(r.stdout, "")

    def test_bad_evaluator_outputs_rejected_end_to_end(self):
        from longrun.controller import run_loop
        for mode in ("bad_hash", "unknown_crit", "aggregate", "prose_worse_field_better"):
            st, repo = self._frozen(self.contract_spec(n=2))
            with st.transaction() as s:
                s["status"] = "FROZEN"; s["budgets"]["max_rounds"] = 1; s["budgets"]["max_repairs"] = 0; s["budgets"]["max_fresh_restarts"] = 0
            c = json.loads(st.contract_path().read_text()); c["budgets"].update({"max_rounds": 1, "max_repairs": 0, "max_fresh_restarts": 0})
            from longrun.store import atomic_write_json
            atomic_write_json(st.contract_path(), c)
            self.set_mode({"builder": "submit", "evaluator": mode, "criteria": ["C1"]})
            status = run_loop(st.run_id)
            s = st.read()
            self.assertNotEqual(status, "PASSED", mode)
            self.assertTrue(all(v["status"] == "FAIL" for v in s["criteria"].values()), mode)
            self.assertTrue(any(e["kind"] == "evaluation.rejected" for e in st.events()), mode)


if __name__ == "__main__":
    import unittest
    unittest.main()
