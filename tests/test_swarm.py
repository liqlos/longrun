from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from longrun.controller import _child_env, launch_session
from longrun.prompts import builder_prompt
from longrun.store import RunStore
from longrun.swarm import (DONE_MARKER, analyze, config_from_contract,
                           corrective_note, recovery_reason, research_dispatch_stalled)


def contract(*, enabled: bool = True) -> dict:
    return {
        "outcome_id": "o", "contract_version": 1, "adapter": "vr_visual",
        "observable_end_state": "A reviewable Skyline result exists.",
        "criteria": [{"id": "C1-result", "statement": "result", "kind": "functional",
                      "evidence_requirements": ["check"], "deterministic_checks": [],
                      "evaluator_policy": "llm_required"}],
        "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
        "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800,
                    "max_rounds": 3, "max_repairs": 1, "max_fresh_restarts": 1},
        "adapter_config": {"builder_swarm": {
            "enabled": enabled, "researchers": 12, "workers": 5,
            "task_retries": 3, "manager_retries": 3,
        }},
    }


def task(shard: str, kind: str, *, background: bool = True, error: bool = False) -> dict:
    return {
        "tool": "task", "input": {"description": f"{shard} shard", "prompt": f"Do {shard}",
                                    "command": shard, "subagent_type": kind,
                                    "background": background},
        "is_error": error, "child_session_id": f"ses-{shard}",
    }


def all_tasks() -> list[dict]:
    return ([task(f"R{i:02d}", "swarm-researcher") for i in range(1, 13)] +
            [task(f"W{i:02d}", "swarm-worker") for i in range(1, 6)])


def test_swarm_prompt_is_project_opt_in_and_keeps_manager_as_orchestrator(tmp_path):
    state = {"criteria": {}, "isolation": "none"}
    common = dict(state=state, round_no=1, is_repair=False, findings=[], capsule=None,
                  adapter_fragment="", workspace=tmp_path, run_id="r" * 8,
                  changed_strategy_required=False)
    enabled = builder_prompt(contract=contract(), **common)
    disabled = builder_prompt(contract=contract(enabled=False), **common)
    assert "[longrun-swarm target=12 writers=5 background=true]" in enabled
    assert "You are the swarm manager and integrator" in enabled
    assert "R01..R12" in enabled and "W01..W05" in enabled
    assert "with `background=true`" in enabled
    assert DONE_MARKER in enabled
    assert "longrun-swarm" not in disabled


def test_swarm_audit_requires_all_dispatches_and_final_handoff():
    cfg = config_from_contract(contract())
    complete = analyze(all_tasks(), cfg, DONE_MARKER)
    assert not recovery_reason(complete, {"terminal": True})

    underfilled = analyze(all_tasks()[:-1], cfg, "ordinary stop")
    assert underfilled["missing_workers"] == ["W05"]
    assert recovery_reason(underfilled, {"terminal": True}) == "swarm_underfilled"
    assert "W05" in corrective_note(underfilled)


def test_background_launch_is_not_completion_and_bad_calls_are_repaired():
    cfg = config_from_contract(contract())
    no_marker = analyze(all_tasks(), cfg, "all launched")
    assert recovery_reason(no_marker, {"terminal": True}) == "clean_stop_without_swarm_handoff"

    bad = all_tasks()
    bad[-1] = task("W05", "swarm-researcher", background=False)
    report = analyze(bad, cfg, DONE_MARKER)
    assert report["wrong_mode"] == ["W05"]
    assert report["wrong_type"] == ["W05"]
    assert recovery_reason(report, {"terminal": True}) == "swarm_underfilled"

    retried = bad + [task("W05", "swarm-worker", background=True)]
    repaired = analyze(retried, cfg, DONE_MARKER)
    assert not repaired["wrong_mode"] and not repaired["wrong_type"]
    assert not recovery_reason(repaired, {"terminal": True})


def test_live_watchdog_nudges_only_a_stalled_underfilled_research_wave():
    cfg = config_from_contract(contract())
    partial = [task(f"R{i:02d}", "swarm-researcher") for i in range(1, 8)]
    assert not research_dispatch_stalled([], cfg, manager_turns=5, last_research_turn=0)
    assert not research_dispatch_stalled(partial, cfg, manager_turns=9, last_research_turn=4)
    assert research_dispatch_stalled(partial, cfg, manager_turns=10, last_research_turn=4)
    complete_research = [task(f"R{i:02d}", "swarm-researcher") for i in range(1, 13)]
    assert not research_dispatch_stalled(complete_research, cfg, manager_turns=99,
                                         last_research_turn=4)


def test_swarm_child_profiles_are_scoped_and_experimental_background_is_enabled():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            root = Path(td) / "project"
            root.mkdir()
            store = RunStore.create(root, "vr_visual", None, {}, driver="opencode")
            store.contract_path().write_text(json.dumps(contract()))
            env = _child_env(store, "session", "builder", 60, driver_name="opencode")
            cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            assert env["OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS"] == "true"
            assert set(cfg["agent"]) == {"swarm-researcher", "swarm-worker"}
            assert cfg["agent"]["swarm-researcher"]["permission"]["*"] == "deny"
            assert "edit" not in cfg["agent"]["swarm-researcher"]["permission"]
            assert cfg["agent"]["swarm-worker"]["permission"]["task"] == "deny"
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


def test_swarm_clean_stop_reconnects_same_manager_on_same_server():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            root = Path(td) / "project"
            root.mkdir()
            doc = contract()
            doc["adapter_config"]["builder_swarm"].update({"researchers": 1, "workers": 1})
            store = RunStore.create(root, "vr_visual", None, doc["budgets"], driver="opencode")
            store.contract_path().write_text(json.dumps(doc))
            with store.transaction() as state:
                state["deadline_epoch"] = __import__("time").time() + 120

            class FakeRunner:
                on_child_start = None

                def __init__(self):
                    self.commands = []

                def run(self, cmd, **kwargs):
                    self.commands.append(cmd)
                    first = len(self.commands) == 1
                    shard = "R01" if first else "W01"
                    kind = "swarm-researcher" if first else "swarm-worker"
                    external = "OC-MANAGER"
                    lines = [
                        json.dumps({"type": "step_start", "sessionID": external, "part": {}}),
                        json.dumps({"type": "tool_use", "sessionID": external, "part": {
                            "tool": "task", "state": {"status": "completed", "input": {
                                "description": f"{shard} shard", "prompt": shard,
                                "subagent_type": kind, "background": True,
                            }, "metadata": {"sessionId": f"child-{shard}"}}}}),
                        json.dumps({"type": "text", "sessionID": external, "part": {
                            "text": "stopped early" if first else DONE_MARKER}}),
                        json.dumps({"type": "step_finish", "sessionID": external, "part": {
                            "reason": "stop", "tokens": {}}}),
                    ]
                    kwargs["stdout_path"].write_text("\n".join(lines) + "\n")
                    kwargs["stderr_path"].write_text("")
                    for line in lines:
                        kwargs["on_stdout_line"](line)
                    return SimpleNamespace(exit_code=0, duration_s=0.1, timed_out=False,
                                           interrupted=False, idle_timed_out=False,
                                           initial_progress_timed_out=False)

            runner = FakeRunner()
            fake_server = {"url": "http://127.0.0.1:43210"}
            with patch("longrun.controller._start_opencode_server", return_value=fake_server), \
                    patch("longrun.controller._stop_opencode_server"), \
                    patch("longrun.controller.time.sleep"):
                launch_session(store, runner, role="builder", prompt="run swarm", max_turns=20)
            assert len(runner.commands) == 2
            assert "--attach" in runner.commands[0]
            assert runner.commands[0][runner.commands[0].index("--attach") + 1] == fake_server["url"]
            assert runner.commands[1][runner.commands[1].index("--session") + 1] == "OC-MANAGER"
            recovery = [e for e in store.events() if e["kind"] == "session.opencode_recovery"]
            assert recovery and recovery[0]["data"]["mode"] == "resume"
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


# ---------------------------------------------------------------- recovery integration

def swarm_contract(researchers: int = 2, workers: int = 1, manager_retries: int = 3) -> dict:
    doc = contract()
    doc["adapter_config"]["builder_swarm"].update({
        "researchers": researchers, "workers": workers,
        "manager_retries": manager_retries,
    })
    return doc


def make_store(td: str, doc: dict) -> RunStore:
    root = Path(td) / "project"
    root.mkdir(exist_ok=True)
    store = RunStore.create(root, "vr_visual", None, doc["budgets"], driver="opencode")
    store.contract_path().write_text(json.dumps(doc))
    with store.transaction() as state:
        state["deadline_epoch"] = __import__("time").time() + 300
    return store


def task_event(shard: str, kind: str) -> str:
    return json.dumps({"type": "tool_use", "sessionID": "OC-MANAGER", "part": {
        "tool": "task", "state": {"status": "completed", "input": {
            "description": f"{shard} shard", "prompt": f"Do {shard}",
            "subagent_type": kind, "background": True,
        }, "metadata": {"sessionId": f"child-{shard}"}}}})


def stream_lines(*, turns_before: int = 0, tasks: list[tuple[str, str]] | None = None,
                 turns_after: int = 0, final_text: str | None = None) -> list[str]:
    lines = [json.dumps({"type": "step_start", "sessionID": "OC-MANAGER", "part": {}})
             for _ in range(turns_before)]
    lines += [task_event(shard, kind) for shard, kind in (tasks or [])]
    lines += [json.dumps({"type": "step_start", "sessionID": "OC-MANAGER", "part": {}})
              for _ in range(turns_after)]
    if final_text is not None:
        lines.append(json.dumps({"type": "text", "sessionID": "OC-MANAGER",
                                 "part": {"text": final_text}}))
        lines.append(json.dumps({"type": "step_finish", "sessionID": "OC-MANAGER",
                                 "part": {"reason": "stop", "tokens": {}}}))
    return lines


class GuardedFakeRunner:
    """Feeds scripted streams; stops emitting once should_stop() fires, like a killed child."""

    on_child_start = None

    def __init__(self, scripts: list[list[str]]):
        self.scripts = list(scripts)
        self.commands = []

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        emitted: list[str] = []
        killed = False
        for line in self.scripts.pop(0):
            kwargs["on_stdout_line"](line)
            emitted.append(line)
            if kwargs["should_stop"]():
                killed = True
                break
        kwargs["stdout_path"].write_text("\n".join(emitted) + "\n")
        return SimpleNamespace(exit_code=(-15 if killed else 0), duration_s=0.1,
                               timed_out=False, interrupted=False, idle_timed_out=False,
                               initial_progress_timed_out=False)


def _launch_with_fakes(store, runner):
    fake_server = {"url": "http://127.0.0.1:43210"}
    calls = {"marker_cleanup": 0}

    def fake_marker_cleanup(marker):
        calls["marker_cleanup"] += 1
        return []

    runner.marker_cleanup_calls = calls

    with patch("longrun.controller._start_opencode_server", return_value=fake_server), \
            patch("longrun.controller._stop_opencode_server"), \
            patch("longrun.controller.cleanup_processes_with_env_marker", fake_marker_cleanup), \
            patch("longrun.controller.time.sleep"):
        launch_session(store, runner, role="builder", prompt="run swarm", max_turns=60)


def events_of(store, kind):
    return [e for e in store.events() if e["kind"] == kind]


def test_underfilled_wave_reconnects_inside_same_round_and_completes():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            store = make_store(td, swarm_contract(researchers=2, workers=1))
            # Attempt 1: six manager turns, zero research dispatches -> live guard fires.
            # Attempt 2: resumed stream opens with plain turns while researchers are still
            # missing (must NOT be instantly re-killed), then fills the wave and finishes.
            runner = GuardedFakeRunner([
                stream_lines(turns_after=6),
                stream_lines(turns_before=2,
                             tasks=[("R01", "swarm-researcher"), ("R02", "swarm-researcher"),
                                    ("W01", "swarm-worker")],
                             final_text=DONE_MARKER),
            ])
            _launch_with_fakes(store, runner)
            assert len(runner.commands) == 2, "reconnect must happen inside the same round"
            nudges = events_of(store, "session.swarm_nudge")
            assert len(nudges) == 1 and nudges[0]["data"]["reason"] == "research_dispatch_stalled"
            recoveries = events_of(store, "session.opencode_recovery")
            assert [r["data"]["reason"] for r in recoveries] == ["swarm_research_dispatch_stalled"]
            assert not events_of(store, "session.swarm_recovery_exhausted")
            assert runner.marker_cleanup_calls["marker_cleanup"] == 1, "children must survive reconnects, die at terminal"
            assert events_of(store, "session.opencode_server_stopped")
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


def test_resumed_manager_launches_missing_researchers_and_reaches_done():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            store = make_store(td, swarm_contract(researchers=5, workers=1))
            # Attempt 1: R01..R03 dispatched by turn 3, then three idle turns -> stall.
            runner = GuardedFakeRunner([
                stream_lines(turns_before=3, tasks=[("R01", "swarm-researcher"),
                                                    ("R02", "swarm-researcher"),
                                                    ("R03", "swarm-researcher")],
                             turns_after=7),
                stream_lines(turns_before=1,
                             tasks=[("R04", "swarm-researcher"), ("R05", "swarm-researcher"),
                                    ("W01", "swarm-worker")],
                             final_text=DONE_MARKER),
            ])
            _launch_with_fakes(store, runner)
            assert len(runner.commands) == 2
            # The resume instruction names the already-dispatched task ids...
            resume_prompt = runner.commands[1][-1]
            assert "task_ids=R01:child-R01" in resume_prompt
            assert "R03" in resume_prompt and "R04" in resume_prompt
            # ...and the final audit counts every shard exactly once.
            progress = events_of(store, "session.swarm_progress")[-1]["data"]
            assert progress["missing_researchers"] == []
            assert progress["launched"] == ["R01", "R02", "R03", "R04", "R05", "W01"]
            assert progress["done_marker"] is True
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


def test_two_consecutive_recoverable_endings_then_success():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            store = make_store(td, swarm_contract(researchers=2, workers=1))
            runner = GuardedFakeRunner([
                stream_lines(turns_after=6),                       # attempt 1: stall
                stream_lines(turns_after=12),                      # resumed attempt: stalls after its doubled window
                stream_lines(tasks=[("R01", "swarm-researcher"),
                                    ("R02", "swarm-researcher"),
                                    ("W01", "swarm-worker")], final_text=DONE_MARKER),
            ])
            _launch_with_fakes(store, runner)
            assert len(runner.commands) == 3
            assert len(events_of(store, "session.swarm_nudge")) == 2
            reasons = [e["data"]["reason"] for e in events_of(store, "session.opencode_recovery")]
            assert reasons.count("swarm_research_dispatch_stalled") == 2
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


def test_exhausted_manager_budget_is_explicit_not_a_masked_builder_failure():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            import pytest
            from longrun.controller import ControllerError

            doc = swarm_contract(researchers=2, workers=1, manager_retries=1)
            store = make_store(td, doc)
            runner = GuardedFakeRunner([
                stream_lines(turns_after=6),
                stream_lines(turns_after=12),   # resumed window is doubled
            ])
            # Exhaustion now surfaces as a terminal ControllerError so the outer
            # loop ends the outcome honestly instead of buying more rounds.
            with pytest.raises(ControllerError, match="recovery budget exhausted"):
                _launch_with_fakes(store, runner)
            assert len(runner.commands) == 2          # initial + one retry, then stop
            exhausted = events_of(store, "session.swarm_recovery_exhausted")
            assert len(exhausted) == 1
            data = exhausted[0]["data"]
            assert data["reason"] == "swarm_research_dispatch_stalled"
            assert data["tries"] == 1 and data["budget"] == 1
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


def test_terminal_provider_error_stops_server_and_background_children():
    import pytest
    from longrun.controller import NonRetryableProviderRequestError

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            store = make_store(td, swarm_contract())
            fatal = json.dumps({"type": "error", "sessionID": "OC-MANAGER",
                                "error": {"data": {"message": "invalid_request_error: bad schema"}}})
            runner = GuardedFakeRunner([[fatal]])
            with pytest.raises(NonRetryableProviderRequestError):
                _launch_with_fakes(store, runner)
            assert runner.marker_cleanup_calls["marker_cleanup"] == 1, \
                "terminal outcome must clean server-owned children"
            assert events_of(store, "session.opencode_server_stopped")
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old


def test_swarm_fanout_is_clamped_to_a_cost_ceiling():
    doc = contract()
    doc["adapter_config"]["builder_swarm"].update({"researchers": 500, "workers": 99})
    cfg = config_from_contract(doc)
    assert cfg["researchers"] == 32 and cfg["workers"] == 16


def test_worker_profile_denies_the_detached_watchdog_exception():
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = td
        try:
            root = Path(td) / "project"
            root.mkdir()
            store = RunStore.create(root, "vr_visual", None, {}, driver="opencode")
            store.contract_path().write_text(json.dumps(contract()))
            env = _child_env(store, "session", "builder", 60, driver_name="opencode")
            cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            parent_bash = cfg["permission"]["bash"]
            worker_bash = cfg["agent"]["swarm-worker"]["permission"]["bash"]
            watchdog = "env -u LONGRUN_SESSION_MARKER nohup setsid *"
            assert parent_bash.get(watchdog) == "allow"
            assert watchdog not in worker_bash
            assert worker_bash["*LONGRUN_SESSION_MARKER*"] == "deny"
        finally:
            if old is None:
                os.environ.pop("LONGRUN_HOME", None)
            else:
                os.environ["LONGRUN_HOME"] = old
