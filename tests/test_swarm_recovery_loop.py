"""Full-controller-loop regression: an exhausted swarm manager budget must end the outcome
FAILED with the exact reason, not fall through to gate/evaluator/repair."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from longrun.swarm import DONE_MARKER


def swarm_contract() -> dict:
    return {
        "outcome_id": "o", "contract_version": 1, "adapter": "software",
        "observable_end_state": "A reviewable result exists.",
        "criteria": [{"id": "C1", "statement": "result", "kind": "functional",
                      "evidence_requirements": ["check"], "deterministic_checks": [],
                      "evaluator_policy": "llm_required"}],
        "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
        "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800,
                    "max_rounds": 3, "max_repairs": 2, "max_fresh_restarts": 1,
                    "max_turns_without_progress": 24},
        "adapter_config": {"builder_swarm": {
            "enabled": True, "researchers": 2, "workers": 1,
            "task_retries": 3, "manager_retries": 2,
        }},
    }


def stall_stream() -> list[str]:
    """Idle manager turns, zero research dispatches: the live guard fires on the first
    attempt after six turns and on resumed attempts after their doubled twelve-turn
    window — an always-idle manager must still land in the swarm budget."""
    return [json.dumps({"type": "step_start", "sessionID": "OC-MANAGER", "part": {}})
            for _ in range(14)]


class AlwaysStallRunner:
    on_child_start = None

    def __init__(self):
        self.commands = []

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        for line in stall_stream():
            kwargs["on_stdout_line"](line)
            if kwargs["should_stop"]():
                break
        kwargs["stdout_path"].write_text("")
        return SimpleNamespace(exit_code=-15, duration_s=0.1, timed_out=False,
                               interrupted=False, idle_timed_out=False,
                               initial_progress_timed_out=False)


class SwarmRecoveryExhaustionLoopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        old = os.environ.get("LONGRUN_HOME")
        self._old_home = old
        os.environ["LONGRUN_HOME"] = str(self.tmp / "lr")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LONGRUN_HOME", None)
        else:
            os.environ["LONGRUN_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_exhausted_manager_budget_finishes_run_failed_before_any_evaluation(self):
        from longrun.controller import _run_loop
        from longrun.store import RunStore

        proj = self.tmp / "proj"
        proj.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=proj)
        subprocess.run(["git", "config", "user.name", "t"], cwd=proj)

        store = RunStore.create(proj, "software", None, {}, driver="opencode")
        store.contract_path().write_text(json.dumps(swarm_contract()))
        with store.transaction() as s:
            s["status"] = "RUNNING"           # skip planning/freeze; loop under test starts here
            s["workspace"] = str(proj)
            s["budgets"] = dict(swarm_contract()["budgets"])
            s["deadline_epoch"] = time.time() + 300

        runner = AlwaysStallRunner()
        fake_server = {"url": "http://127.0.0.1:43210"}
        with patch("longrun.controller._start_opencode_server", return_value=fake_server), \
                patch("longrun.controller._stop_opencode_server"), \
                patch("longrun.controller.cleanup_processes_with_env_marker", return_value=[]), \
                patch("longrun.controller.time.sleep"):
            status = _run_loop(store, runner, None, None)

        # The outcome ended honestly at the builder stage...
        self.assertEqual(status, "FAILED")
        state = store.read(verify=False)
        self.assertEqual(state["status"], "FAILED")
        self.assertIn("swarm manager recovery budget exhausted", state["terminal_reason"])

        # ...after exactly initial + manager_retries transport attempts in ONE round.
        self.assertEqual(len(runner.commands), 3)
        self.assertEqual(state["counters"]["rounds"], 1)
        self.assertEqual(state["counters"].get("repairs", 0), 0)

        events = [e["kind"] for e in store.events()]
        self.assertIn("session.swarm_recovery_exhausted", events)
        self.assertNotIn("evaluation.start", events)      # no evaluator was ever bought
        self.assertNotIn("round.gate_failed", events)     # no gate ran on an unstarted swarm
        self.assertIn("session.opencode_server_stopped", events)  # guaranteed cleanup


if __name__ == "__main__":
    unittest.main()
