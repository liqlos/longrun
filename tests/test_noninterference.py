"""Mechanical non-interference proof (release-blocking)."""
from __future__ import annotations
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from helpers import LongrunTestCase, LONGRUN_BIN


class TestNonInterference(LongrunTestCase):
    def _hook(self, event, payload, env=None):
        return self.cli("hook", event, env=env, stdin=json.dumps(payload))

    def test_01_ordinary_session_outside_any_run(self):
        for ev in ("stop", "session-start", "pre-tool-use", "task-completed"):
            r = self._hook(ev, {"session_id": "abc", "cwd": str(self.tmp), "hook_event_name": ev, "transcript_path": "/nonexistent"})
            self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout, ""); self.assertEqual(r.stderr, "")

    def test_02_codex_config_untouched_by_install(self):
        # longrun never writes ~/.codex; the doctor check verifies no longrun reference in hooks.json
        r = self.cli("doctor")
        self.assertIn("no global longrun hooks in ~/.codex/hooks.json: ok", r.stdout)

    def test_03_ordinary_session_inside_active_run_repo(self):
        st, repo = self.make_run()
        from longrun.controller import freeze_run
        freeze_run(st)
        with st.transaction() as s:
            s["status"] = "RUNNING"
        for ev in ("stop", "session-start"):
            r = self._hook(ev, {"session_id": "owner-chat", "cwd": str(repo / "sub"), "hook_event_name": ev})
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        # even a session that mentions longrun in its transcript is not affected without a token
        tx = self.tmp / "tx.jsonl"; tx.write_text("longrun run --run abc\n")
        r = self._hook("stop", {"session_id": "owner-chat", "cwd": str(repo), "transcript_path": str(tx)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_04_fail_open_on_bad_identity(self):
        st, repo = self.make_run()
        from longrun.controller import freeze_run
        from longrun.token import mint
        freeze_run(st)
        with st.transaction() as s:
            s["status"] = "RUNNING"
            s["children"].append({"session_id": "S1", "role": "builder", "pid": 1, "pgid": 1, "started_at": "x", "ended_at": None, "exit": None})
        good = mint(st.secret(), run_id=st.run_id, session_id="S1", role="builder", controller_pid=os.getpid(), ttl_seconds=60)
        cases = {
            "missing token": ({}, {"session_id": "S1"}),
            "malformed token": ({"LONGRUN_TOKEN": "lr1.garbage.x"}, {"session_id": "S1"}),
            "forged token (wrong secret)": ({"LONGRUN_TOKEN": mint("wrong", run_id=st.run_id, session_id="S1", role="builder", controller_pid=os.getpid(), ttl_seconds=60)}, {"session_id": "S1"}),
            "stale token": ({"LONGRUN_TOKEN": mint(st.secret(), run_id=st.run_id, session_id="S1", role="builder", controller_pid=os.getpid(), ttl_seconds=-5)}, {"session_id": "S1"}),
            "wrong run id": ({"LONGRUN_TOKEN": mint(st.secret(), run_id="00000000-0000-0000-0000-000000000000", session_id="S1", role="builder", controller_pid=os.getpid(), ttl_seconds=60)}, {"session_id": "S1"}),
            "dead controller pid": ({"LONGRUN_TOKEN": mint(st.secret(), run_id=st.run_id, session_id="S1", role="builder", controller_pid=2**22 - 7, ttl_seconds=60)}, {"session_id": "S1"}),
            "unknown session id": ({"LONGRUN_TOKEN": good}, {"session_id": "S-other"}),
            "missing session id": ({"LONGRUN_TOKEN": good}, {}),
            "no transcript, no session": ({"LONGRUN_TOKEN": good}, {"cwd": str(repo)}),
        }
        for name, (env, payload) in cases.items():
            r = self._hook("stop", payload, env=env)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), name)
        # control: the genuine binding does produce a bounded block for a builder with no evidence
        r = self._hook("stop", {"session_id": "S1", "cwd": str(repo)}, env={"LONGRUN_TOKEN": good})
        self.assertIn('"decision": "block"', r.stdout)

    def test_05_env_var_alone_cannot_mutate_run(self):
        st, repo = self.make_run()
        from longrun.controller import freeze_run
        freeze_run(st)
        before = st.read()["version"]
        r = self.cli("evidence", "submit", "--criterion", "C1", "--kind", "check", "--summary", "spoofed evidence entry",
                     env={"LONGRUN_TOKEN": "lr1.eyJydW5faWQiOiJ4In0.YWJj", "LONGRUN_RUN_ID": st.run_id})
        self.assertNotEqual(r.returncode, 0)
        r = self.cli("observe", "--note", "spoof", env={"LONGRUN_RUN_ID": st.run_id})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(st.read()["version"], before)
        self.assertFalse(any(e["kind"] == "evidence.recorded" and e["data"]["by"] != "controller:baseline" for e in st.events()))

    def test_06_two_runs_two_repos_isolated(self):
        a, ra = self.make_run(); b, rb = self.make_run(repo=self.repo("proj2"))
        self.assertNotEqual(a.run_id, b.run_id)
        self.assertNotEqual(a.secret(), b.secret())
        self.assertNotEqual(a.dir, b.dir)
        with a.transaction() as s:
            s["counters"]["rounds"] = 7
        self.assertEqual(b.read()["counters"]["rounds"], 0)

    def test_07_two_runs_same_repo_always_reject(self):
        from longrun.controller import create_run, ControllerError, freeze_run
        st, repo = self.make_run()
        freeze_run(st)
        with self.assertRaises(ControllerError):
            create_run(repo, "software", "claude", isolation="none", allow_dirty=True)
        with self.assertRaisesRegex(ControllerError, "one live run"):
            create_run(repo, "software", "claude", isolation="worktree")

    def test_08_settings_untouched_and_uninstall_restores_hash(self):
        settings = Path.home() / ".claude" / "settings.json"
        h0 = hashlib.sha256(settings.read_bytes()).hexdigest() if settings.is_file() else None
        st, repo = self.make_run()
        from longrun.controller import freeze_run
        freeze_run(st)
        h1 = hashlib.sha256(settings.read_bytes()).hexdigest() if settings.is_file() else None
        self.assertEqual(h0, h1)
        # uninstall dry-run against a temp HOME with a snapshot must report hash_ok
        fake_home = self.tmp / "fakehome"; (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "settings.json").write_text('{"a":1}')
        env = {"HOME": str(fake_home), "LONGRUN_HOME": str(fake_home / "lr")}
        code = ("from longrun.migrate import install_snapshot, uninstall\nimport json\ninstall_snapshot()\n"
                "open(str(__import__('pathlib').Path.home()/'.claude'/'settings.json'),'w').write('{\"a\":2}')\n"
                "print(json.dumps(uninstall(dry_run=False)))\n")
        script = self.tmp / "u.py"; script.write_text(code)
        r = subprocess.run([os.environ.get("PYTHON_FOR_TESTS", "python3"), str(script)], capture_output=True, text=True,
                           env={**os.environ, **env, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        rep = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(all(rep["hash_ok"].values()), rep)
        self.assertEqual((fake_home / ".claude" / "settings.json").read_text(), '{"a":1}')

    def test_09_crashed_controller_leaves_ordinary_sessions_unaffected(self):
        st, repo = self.make_run()
        from longrun.controller import freeze_run
        from longrun.token import mint
        freeze_run(st)
        with st.transaction() as s:
            s["status"] = "RUNNING"; s["controller_pid"] = 2**22 - 9
            s["children"].append({"session_id": "S1", "role": "builder", "pid": 2**22 - 9, "pgid": None, "started_at": "x", "ended_at": None, "exit": None})
        tok = mint(st.secret(), run_id=st.run_id, session_id="S1", role="builder", controller_pid=2**22 - 9, ttl_seconds=600)
        r = self._hook("stop", {"session_id": "S1", "cwd": str(repo)}, env={"LONGRUN_TOKEN": tok})
        self.assertEqual((r.returncode, r.stdout), (0, ""))   # dead controller -> fail open even for a real token
        r = self._hook("stop", {"session_id": "owner", "cwd": str(repo)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_10_no_global_hook_and_constant_time_dispatch(self):
        settings = Path.home() / ".claude" / "settings.json"
        if settings.is_file():
            s = json.loads(settings.read_text())
            for ev, groups in (s.get("hooks") or {}).items():
                for g in groups:
                    for h in g.get("hooks", []):
                        self.assertNotIn("longrun", h.get("command", "")); self.assertNotIn("autopilot", h.get("command", ""))
        big = {"session_id": "x", "cwd": str(self.tmp), "transcript_path": "/none", "junk": "y" * 200000}
        t0 = time.monotonic(); r = self._hook("stop", big); dt = time.monotonic() - t0
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertLess(dt, 3.0)

    def test_11_old_bundle_archived_has_no_effect(self):
        repo = self.repo()
        (repo / ".claude" / "autopilot").mkdir(parents=True)
        (repo / ".claude" / "autopilot" / "state.json").write_text(json.dumps({"schema": "autopilot-gate/v2", "active": True, "batch": {"id": 1}}))
        r = self._hook("stop", {"session_id": "s", "cwd": str(repo)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self._hook("session-start", {"session_id": "s", "cwd": str(repo)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    import unittest
    unittest.main()
