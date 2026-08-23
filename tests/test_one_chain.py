"""One chain per project, and landings that must not be blocked by the harness's own bookkeeping.

Written with unittest, not pytest: `run_tests.sh` discovers with unittest and pytest is not installed here,
so as a pytest module this file imported-errored on every run and its five tests never actually ran.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class OneChainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._old_home = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = str(self.tmp / "lr")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LONGRUN_HOME", None)
        else:
            os.environ["LONGRUN_HOME"] = self._old_home
        self._tmp.cleanup()

    def _run(self, rid, project, status="RUNNING", pid=None, **extra):
        from longrun.paths import runs_root
        d = runs_root() / rid
        d.mkdir(parents=True, exist_ok=True)
        state = {"run_id": rid, "status": status, "project_root": str(project),
                 "controller_pid": pid if pid is not None else os.getpid()}
        state.update(extra)
        (d / "state.json").write_text(json.dumps(state))
        return d

    def test_a_live_chain_on_the_same_project_is_seen(self):
        from longrun.cli import _live_chain_on
        proj = self.tmp / "proj"; proj.mkdir()
        self.assertIsNone(_live_chain_on(proj))
        self._run("aaaaaaaa-0000-0000-0000-000000000000", proj)
        live = _live_chain_on(proj)
        self.assertTrue(live and live["run_id"].startswith("aaaaaaaa"))

    def test_project_lock_closes_simultaneous_start_race(self):
        from longrun.cli import _acquire_chain_lock
        proj = self.tmp / "proj"; proj.mkdir()
        first = _acquire_chain_lock(proj)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(_acquire_chain_lock(proj))
        finally:
            first.close()
        again = _acquire_chain_lock(proj)
        self.assertIsNotNone(again)
        again.close()

    def test_create_run_refuses_a_live_owner_even_in_a_worktree(self):
        from longrun import controller as C
        proj = self.tmp / "proj"; proj.mkdir()
        real_is_git = C.G.is_git_repo
        C.G.is_git_repo = lambda p: False
        try:
            C.create_run(proj, "software", "claude", isolation="none", allow_dirty=True)
            with self.assertRaisesRegex(C.ControllerError, "reattach with `longrun watch"):
                C.create_run(proj, "software", "claude", isolation="worktree")
        finally:
            C.G.is_git_repo = real_is_git

    def test_run_controller_lock_blocks_a_second_resume(self):
        from longrun.controller import ControllerError, _acquire_run_controller, run_loop
        from longrun.store import RunStore
        proj = self.tmp / "proj"; proj.mkdir()
        st = RunStore.create(proj, "software", None, {"wall_time_seconds": 60}, driver="claude")
        lease = _acquire_run_controller(st)
        try:
            with self.assertRaisesRegex(ControllerError, "already has a live controller"):
                run_loop(st.run_id)
        finally:
            lease.close()

    def test_other_projects_and_finished_runs_do_not_count(self):
        from longrun.cli import _live_chain_on
        proj, other = self.tmp / "proj", self.tmp / "other"
        proj.mkdir(); other.mkdir()
        self._run("bbbbbbbb-0000-0000-0000-000000000000", other)                  # different project
        self._run("cccccccc-0000-0000-0000-000000000000", proj, status="PASSED")
        self.assertIsNone(_live_chain_on(proj))

    def test_a_run_whose_controller_died_is_not_a_live_chain(self):
        from longrun.cli import _live_chain_on
        proj = self.tmp / "proj"; proj.mkdir()
        self._run("dddddddd-0000-0000-0000-000000000000", proj, pid=999_000_001)  # pid that cannot exist
        self.assertIsNone(_live_chain_on(proj))

    def test_an_expired_run_does_not_revive_when_its_pid_is_reused(self):
        from longrun.cli import _live_chain_on
        proj = self.tmp / "proj"; proj.mkdir()
        self._run("deadbeef-0000-0000-0000-000000000000", proj, status="CREATED",
                  pid=os.getpid(), created_at="2000-01-01T00:00:00Z",
                  budgets={"wall_time_seconds": 60})
        self.assertIsNone(_live_chain_on(proj))

    def test_a_dead_run_does_not_block_creating_a_new_one(self):
        """The same reasoning as above, at the other gate: `create_run` refuses an in-place second run while
        another is active. Counting a run whose controller is gone locked the owner out of his own project."""
        from longrun import controller as C
        proj = self.tmp / "proj"; proj.mkdir()
        self._run("eeeeeeee-0000-0000-0000-000000000000", proj, status="PLANNED", pid=999_000_001)
        real_is_git = C.G.is_git_repo
        C.G.is_git_repo = lambda p: False
        try:
            st = C.create_run(proj, "software", "claude", isolation="none", allow_dirty=True)
            self.assertEqual(st.read(verify=False)["status"], "CREATED")
        finally:
            C.G.is_git_repo = real_is_git

    def test_harness_bookkeeping_does_not_block_a_landing(self):
        from longrun import gitutil as G
        repo = self.tmp / "r"; repo.mkdir()

        def git(*a):
            return subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)
        git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
        (repo / ".longrun").mkdir(); (repo / ".longrun/history.jsonl").write_text("{}\n")
        (repo / "a.txt").write_text("one")
        git("add", "-A"); git("commit", "-qm", "base")
        git("checkout", "-q", "-b", "longrun/x"); (repo / "a.txt").write_text("two")
        git("commit", "-qam", "work"); git("checkout", "-q", "main")
        (repo / ".longrun/history.jsonl").write_text('{"run": "just finished"}\n')   # the harness dirtied this itself
        ok, out = G.fast_forward_into(repo, "longrun/x")
        self.assertTrue(ok, out)
        (repo / "a.txt").write_text("owner edit")                                   # a real dirty file still refuses
        git("checkout", "-q", "-b", "longrun/y", "longrun/x")
        ok2, out2 = G.fast_forward_into(repo, "longrun/y")
        self.assertFalse(ok2)
        self.assertIn("a.txt", out2)

    def test_a_run_still_planning_is_already_a_live_chain(self):
        """controller_pid must be visible from create_run onward, not only once run_loop starts — otherwise the
        minutes a planner spends before freeze are an undetected window for a second chain."""
        from longrun import controller as C
        from longrun.cli import _live_chain_on
        proj = self.tmp / "proj"; proj.mkdir()
        real_is_git = C.G.is_git_repo
        C.G.is_git_repo = lambda p: False
        try:
            st = C.create_run(proj, "software", "claude", isolation="none", allow_dirty=True)
            self.assertEqual(st.read(verify=False)["status"], "CREATED")
            live = _live_chain_on(proj)
            self.assertTrue(live and live["run_id"] == st.run_id)
        finally:
            C.G.is_git_repo = real_is_git


if __name__ == "__main__":
    unittest.main()
