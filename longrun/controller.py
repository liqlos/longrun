"""The controller: the one outer continuation authority for a run.

create -> plan (contract) -> freeze (baseline BEFORE any editable execution) -> rounds of
[fresh builder session -> deterministic checks -> fresh evaluator -> controller applies transitions ->
loop guard -> repair | fresh restart | terminal].
"""
from __future__ import annotations
import atexit
import datetime as dt
import fcntl
import json
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid
from collections import deque
from pathlib import Path

from . import contract as C
from . import gitutil as G
from .adapters import load_adapter
from .drivers import get_driver
from .models import resolve as resolve_model
from .config import load as _cfg
from .evaluator import (EVALUATOR_JSON_SCHEMA, EvaluatorError, apply_transitions, extract_json_object,
                        validate_verdict, criteria_fingerprint)
from .evidence import EvidenceError, evidence_manifest, list_evidence, manifest_hash, record_evidence
from .loopguard import analyze_stream, build_failure_capsule, strategic_check
from .paths import data_root, keys_root, project_marker, run_admission_lock_path
from .process import ChildRunner, Interrupted, cleanup_processes_with_env_marker
from .prompts import RESTART_DECISION_SCHEMA, builder_prompt, evaluator_prompt, restart_manager_prompt
from .store import (RunStore, StaleWrite, TamperDetected, atomic_write_json, canonical_json,
                    find_active_runs, now_iso, sha256_bytes, TERMINAL_STATES)
from .swarm import (analyze as analyze_swarm, config_from_contract as swarm_config_from_contract,
                    corrective_note as swarm_corrective_note, recovery_reason as swarm_recovery_reason,
                    research_dispatch_stalled as swarm_research_dispatch_stalled)
from .token import ENV_TOKEN, mint


class ControllerError(Exception):
    pass


class TerminalQuotaError(ControllerError):
    """A provider-wide quota that cannot be repaired by retrying this run."""


class StrategicModelStalledError(ControllerError):
    """The requested strategic model produced no stream progress twice."""


class NonRetryableProviderRequestError(ControllerError):
    """The provider rejected an invariant request/configuration; retrying unchanged cannot help."""


class AutoPlanFailedError(ControllerError):
    """All bounded attempts to produce an owner-faithful contract were exhausted."""


def log(store: RunStore, msg: str) -> None:
    line = f"{now_iso()} longrun[{store.run_id[:8]}] {msg}"
    print(line, flush=True)
    with open(store.dir / "controller.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _stop_opencode_server(server: dict | None) -> None:
    if not server or server.get("stopped"):
        return
    server["stopped"] = True
    proc = server["process"]
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
    for fh in server.get("files", ()):
        fh.close()
    callback = server.get("atexit")
    if callback:
        atexit.unregister(callback)


def _start_opencode_server(store: RunStore, *, sid: str, cwd: Path, env: dict) -> dict:
    """Start a run-scoped server so background tasks survive manager-turn EOF."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    stdout_fh = (store.sessions_dir / f"{sid}.opencode-server.stdout.txt").open("w")
    stderr_fh = (store.sessions_dir / f"{sid}.opencode-server.stderr.txt").open("w")
    proc = subprocess.Popen(
        ["opencode", "serve", "--pure", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=cwd, env=env, stdout=stdout_fh, stderr=stderr_fh, start_new_session=True,
    )
    server = {"process": proc, "url": f"http://127.0.0.1:{port}",
              "files": (stdout_fh, stderr_fh), "stopped": False}
    callback = lambda: _stop_opencode_server(server)
    server["atexit"] = callback
    atexit.register(callback)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _stop_opencode_server(server)
            raise ControllerError(f"OpenCode swarm server exited during startup ({proc.returncode})")
        try:
            with urllib.request.urlopen(server["url"] + "/global/health", timeout=0.5) as response:
                if response.status == 200:
                    return server
        except OSError:
            time.sleep(0.1)
    _stop_opencode_server(server)
    raise ControllerError("OpenCode swarm server did not become healthy within 15 seconds")


def _tail_text(path: Path, max_bytes: int) -> str:
    """Read a bounded UTF-8 tail without first loading a potentially huge log."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - max_bytes))
            return fh.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return ""


def _file_matches_regex(path: Path, pattern: str) -> bool:
    """Search a file incrementally, retaining overlap for cross-chunk matches."""
    import re
    rx = re.compile(pattern)
    overlap = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    return bool(rx.search(overlap))
                text = overlap + chunk
                if rx.search(text):
                    return True
                overlap = text[-8192:]
    except OSError:
        return False


def _acquire_run_controller(store: RunStore):
    """Hold one controller lease for the full run, independently of state transactions."""
    path = store.dir / ".controller.lock"
    fh = open(path, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise ControllerError(f"run {store.run_id[:8]} already has a live controller")
    return fh


def _acquire_run_admission(project_root: Path):
    """Serialize the active-run check with creating its successor.

    A worktree prevents file conflicts; it does not make two autonomous
    outcomes on the same product coherent.  There is one owner run per project.
    """
    path = run_admission_lock_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise ControllerError(
            f"another launch is admitting a run for {project_root}; reattach with `longrun runs` instead of starting a second one"
        )
    return fh


# ---------------------------------------------------------------------------- create / plan
def create_run(project_root: Path, adapter: str, driver: str, budgets: dict | None = None,
               isolation: str = "worktree", allow_dirty: bool = False, parent_run_id: str | None = None) -> RunStore:
    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise ControllerError(f"project root {project_root} does not exist")
    try:
        driver_impl = get_driver(driver)
    except ValueError as e:
        raise ControllerError(str(e)) from e
    if not driver_impl.available():
        raise ControllerError(f"driver {driver!r} is configured but its executable is unavailable")
    if isolation == "auto":
        dirty = G.is_git_repo(project_root) and G.is_dirty(project_root)
        isolation = "none" if (dirty or not G.is_git_repo(project_root)) else "worktree"
        allow_dirty = allow_dirty or dirty
    admission = _acquire_run_admission(project_root)
    try:
        # Only a run whose controller is still breathing owns the project.  Dead active-looking records are
        # reported by doctor and do not prevent recovery, but live work is never isolated into a second run.
        from .token import controller_alive
        active = [a for a in find_active_runs(project_root)
                  if a.get("controller_pid") and controller_alive(int(a["controller_pid"]))]
        if active:
            owner = active[0]
            raise ControllerError(
                f"project already has one live run: {owner['run_id'][:8]} ({owner.get('status')}). "
                f"No second run is created; reattach with `longrun watch --run {owner['run_id'][:8]}`."
            )
        if isolation == "none" and G.is_git_repo(project_root) and G.is_dirty(project_root) and not allow_dirty:
            raise ControllerError("project has uncommitted changes; in-place runs need --allow-dirty (or use worktree isolation)")
        start_rev = G.head(project_root) if G.is_git_repo(project_root) else None
        b = dict(C.DEFAULT_BUDGETS); b.update(budgets or {})
        st = RunStore.create(project_root, adapter, start_rev, b, parent_run_id=parent_run_id, driver=driver)
        with st.transaction() as s:
            s["isolation"] = isolation
            s["start_content_revision"] = G.content_revision(project_root)
            # Set before planning: the planner itself is live project work and must be visible to all launch paths.
            s["controller_pid"] = os.getpid()
    finally:
        admission.close()
    marker = project_marker(project_root)
    try:
        marker.parent.mkdir(exist_ok=True)
        atomic_write_json(marker, {"last_run_id": st.run_id, "note": "pointer only; presence activates nothing"})
    except OSError:
        pass
    return st



def _budgets_with_adapter_floor(b: dict, adapter) -> dict:
    """Let an adapter raise the child-session floor when one verification step alone runs for minutes."""
    floor = int(getattr(adapter, "min_child_timeout_seconds", 0) or 0)
    if floor and int(b.get("child_timeout_seconds", 0)) < floor:
        b = dict(b)
        b["child_timeout_seconds"] = min(floor, int(b.get("wall_time_seconds", floor)))
    return b


def set_contract(store: RunStore, spec: dict) -> dict:
    st = store.read()
    if st["status"] not in ("CREATED", "PLANNED"):
        raise ControllerError(f"cannot set contract in state {st['status']} (contract is immutable after FROZEN; use rebase)")
    adapter = load_adapter(st["adapter"], spec.get("adapter_config"))
    crits = []
    for x in spec["criteria"]:
        x = dict(x)
        if not x.get("evidence_requirements"):
            x["evidence_requirements"] = adapter.evidence_for_kind(x.get("kind", "functional"))
        crits.append(x)
    c = C.new_contract(run_id=store.run_id, project_root=st["project_root"], adapter=st["adapter"],
                       observable_end_state=spec["observable_end_state"], criteria=crits,
                       constraints=spec.get("constraints"), non_goals=spec.get("non_goals"),
                       allowed_replace_remove=spec.get("allowed_replace_remove"), proven_blockers=spec.get("proven_blockers"),
                       budgets=_budgets_with_adapter_floor({**st["budgets"], **(spec.get("budgets") or {})}, adapter),
                       owner_judgment_policy=spec.get("owner_judgment_policy", "stop_and_ask"),
                       start_revision=st["start_revision"], outcome_id=spec.get("outcome_id"),
                       allowed_commands=(spec.get("allowed_commands") or []) + adapter.allowed_commands,
                       workspace_paths=spec.get("workspace_paths"), adapter_config=spec.get("adapter_config"),
                       batch=spec.get("batch"))
    atomic_write_json(store.contract_path(), c)
    atomic_write_json(store.contract_path(1), c)
    with store.transaction() as s:
        s["status"] = "PLANNED"; s["budgets"] = c["budgets"]; s["contract_version"] = 1
        s["criteria"] = {x["id"]: {"status": "FAIL", "evidence_ids": [], "history": []} for x in c["criteria"]}
    store.append_event("contract.planned", {"criteria": [x["id"] for x in c["criteria"]]})
    return c


# ---------------------------------------------------------------------------- freeze (baseline before edits)
def freeze_run(store: RunStore, runner: ChildRunner | None = None) -> dict:
    st = store.read()
    if st["status"] != "PLANNED":
        raise ControllerError(f"freeze requires PLANNED, got {st['status']}")
    contract = json.loads(store.contract_path().read_text())
    project_root = Path(st["project_root"])
    # 1. workspace isolation
    if st.get("isolation", "worktree") == "worktree" and G.is_git_repo(project_root) and st["start_revision"]:
        ws = store.dir / "workspace"
        ok, out = G.add_worktree(project_root, ws, f"longrun/{store.run_id[:8]}", st["start_revision"])
        if not ok:
            raise ControllerError(f"worktree creation failed: {out}")
        workspace = ws
    else:
        workspace = project_root
    # 2. verify nothing was edited between run creation and freeze
    current_rev = G.content_revision(workspace)
    if st["start_revision"] and not current_rev.startswith(st["start_revision"]):
        raise ControllerError(f"workspace HEAD {current_rev[:12]} != start revision {st['start_revision'][:12]}; refusing to freeze")
    if st["start_revision"] and not current_rev.endswith("+clean") and st.get("isolation") == "worktree":
        raise ControllerError("worktree not clean at freeze; refusing")
    if st.get("isolation") == "none" and st.get("start_content_revision") and current_rev != st["start_content_revision"]:
        # The property being protected is "the baseline is captured before any *builder* edit", not "no file in
        # the project may move while the planner thinks". On a live, dirty project (a sidecar writing artifacts,
        # the owner's own editor) the content hash drifts during the 1–3 minutes of planning, and refusing here
        # threw away a planned contract that had already been paid for — observed 2026-08-19 on runs 0f9396ed
        # and c47fd148, both of which died at exactly this line with the planning cost sunk. So: if nothing of
        # this run has executed yet, re-take the baseline at the freeze point and record both revisions.
        # Refuse only when the run really is stale — a builder has already run, or the plan is hours old and was
        # written against content that has since moved.
        launched = {e.get("data", {}).get("role") for e in store.events() if e.get("kind") == "session.launch"}
        age_s = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st["created_at"].replace("Z", "+00:00"))).total_seconds()
        if st.get("rounds", 0) == 0 and "builder" not in launched and age_s <= 1800:
            store.append_event("baseline.rebased", {"from": st["start_content_revision"], "to": current_rev,
                                                    "age_s": round(age_s, 1), "reason": "project changed while planning; nothing had executed yet"})
            with store.transaction() as s:
                s["start_content_revision"] = current_rev
        else:
            raise ControllerError(f"project content changed since the run was created ({st['start_content_revision']} -> {current_rev}); "
                                  f"baseline must be frozen before any edit. Create a new run.")
    # 3. contract hash (final, includes baseline evidence ids assigned ahead of time)
    adapter = load_adapter(st["adapter"], contract.get("adapter_config"))
    base_cmds = adapter.baseline(workspace) + [dict(ch, criterion=x["id"]) for x in contract["criteria"] for ch in x["deterministic_checks"]]
    pre_ids = [f"E{uuid.uuid4().hex[:12]}" for _ in base_cmds]
    frozen = C.freeze(contract, current_rev, pre_ids, now_iso())
    chash = C.contract_hash(frozen)
    atomic_write_json(store.contract_path(), frozen)
    atomic_write_json(store.contract_path(frozen["contract_version"]), frozen)
    if st.get("isolation") == "none" and st.get("start_revision"):
        dirty_snapshot = store.dir / "baseline-dirty"
        manifest = G.snapshot_dirty_baseline(workspace, st["start_revision"], dirty_snapshot)
        store.append_event("baseline.dirty_snapshot", {"paths": len(manifest), "dir": str(dirty_snapshot)})
    with store.transaction() as s:
        s["workspace"] = str(workspace); s["contract_hash"] = chash
        s["baseline"] = {"revision": current_rev, "frozen_at": frozen["baseline"]["frozen_at"], "evidence": pre_ids}
        s["status"] = "FROZEN"
        s["deadline_epoch"] = time.time() + int(s["budgets"]["wall_time_seconds"])
    # 4. baseline evidence at the frozen revision (before any editable execution)
    results = []
    for cmd, eid in zip(base_cmds, pre_ids):
        r = _run_check(cmd, workspace, contract["budgets"]["child_timeout_seconds"])
        results.append(r)
        try:
            record_evidence(store, kind=cmd.get("kind", "check"), criterion_ids=[cmd.get("criterion")] if cmd.get("criterion") else [contract["criteria"][0]["id"]],
                            summary=f"BASELINE {cmd['cmd'][:120]} -> exit {r['exit_code']}", revision=current_rev,
                            submitted_by="controller:baseline", command=cmd["cmd"], exit_code=r["exit_code"],
                            stdout=r["stdout"], stderr=r["stderr"], data={"baseline": True, "passed": r["passed"]}, preassigned_id=eid)
        except EvidenceError as e:
            log(store, f"baseline evidence rejected: {e}")
    # Keep which checks were already green before any edit. Both halves of this comparison were being computed
    # and one thrown away: the evaluator's manifest is filtered to the current revision, so it never saw the
    # baseline result and was structurally unable to notice that a check it was about to PASS on had been
    # passing all along. Measured: 61% of criterion checks were green at freeze.
    with store.transaction() as s:
        s["baseline_check_results"] = {r["cmd"]: bool(r["passed"]) for r in results}
    store.append_event("run.frozen", {"contract_hash": chash, "revision": current_rev, "workspace": str(workspace),
                                      "baseline_checks": [{"cmd": r["cmd"], "exit": r["exit_code"]} for r in results]})
    log(store, f"FROZEN contract {chash[:12]} at {current_rev[:12]}; workspace {workspace}")
    return frozen


def _run_check(cmd: dict, cwd: Path, max_timeout: int) -> dict:
    t = min(int(cmd.get("timeout_seconds", 600)), int(max_timeout))
    t0 = time.monotonic()
    from .process import _kill_group
    with tempfile.TemporaryDirectory(prefix="longrun-check-") as td:
        out_path = Path(td) / "stdout.txt"
        err_path = Path(td) / "stderr.txt"
        with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
            p = subprocess.Popen(cmd["cmd"], shell=True, cwd=str(cmd.get("cwd") or cwd),
                                 stdout=out_f, stderr=err_f, start_new_session=True)
            try:
                rc = p.wait(timeout=t)
                to = False
            except subprocess.TimeoutExpired:
                _kill_group(p)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_group(p, grace=0)
                rc, to = None, True
        out = _tail_text(out_path, 20_000)
        err = _tail_text(err_path, 20_000)
        regex_matched = (_file_matches_regex(out_path, cmd.get("expect_stdout_regex"))
                         if cmd.get("expect_stdout_regex") else None)
    if to:
        err = (err + "\nTIMEOUT").strip()
    expect_exit = int(cmd.get("expect_exit", 0))
    passed = (rc == expect_exit)
    rx = cmd.get("expect_stdout_regex")
    if passed and rx:
        passed = regex_matched
    return {"id": cmd.get("id"), "criterion": cmd.get("criterion"), "cmd": cmd["cmd"], "exit_code": rc, "passed": passed,
            "expected_exit": expect_exit, "expect_stdout_regex": rx, "regex_matched": regex_matched,
            "timed_out": to, "stdout": out[-20000:], "stderr": err[-20000:], "duration_s": round(time.monotonic() - t0, 2)}


def _prepare_host_services(store: RunStore) -> None:
    """Start adapter-owned local services before a controller invokes their checks.

    Unity Editor relies on the Unity Hub's licensing client.  Launching the
    editor directly after a stale client dies can leave the named-pipe mutex in
    a state where every batch editor waits out its licensing timeout.  Hub owns
    the healthy client, so start it unobtrusively before the VR baseline and
    every VR round gate.  The subsequent CompileOnly check remains the actual
    proof that the host is usable.
    """
    if store.read().get("adapter") != "vr_visual":
        return
    p = subprocess.run(["open", "-gja", "Unity Hub"], capture_output=True, text=True)
    if p.returncode != 0:
        raise ControllerError(f"could not start Unity Hub for VR checks: {(p.stderr or p.stdout).strip()}")
    store.append_event("host_service.prepared", {"service": "Unity Hub", "reason": "Unity licensing IPC"})
    time.sleep(2)


def run_deterministic_checks(store: RunStore, submitted_by: str, max_total_seconds: int | None = None) -> list[dict]:
    st = store.read()
    contract = json.loads(store.contract_path().read_text())
    ws = Path(st["workspace"] or st["project_root"])
    results = []
    t0 = time.monotonic()
    for x in contract["criteria"]:
        for ch in x["deterministic_checks"]:
            if max_total_seconds and time.monotonic() - t0 > max_total_seconds:
                break
            before_rev = G.content_revision(ws)
            r = _run_check(dict(ch, criterion=x["id"]), ws, contract["budgets"]["child_timeout_seconds"])
            after_rev = G.content_revision(ws)
            if after_rev != before_rev:
                # Contract checks are observations, not builders.  If a check
                # rewrites tracked output, the content revision moves after the
                # builder submitted evidence and the whole ledger appears stale.
                # Fail that check explicitly so the repair session fixes the
                # mutating verifier instead of resubmitting identical evidence
                # forever.  Keep the changed tree intact; it belongs to the run.
                r["passed"] = False
                r["stderr"] = ((r.get("stderr") or "") +
                               "\nLONGRUN: deterministic check modified the workspace "
                               f"({before_rev[:12]} -> {after_rev[:12]}). "
                               "Checks must be read-only; write reports to a temporary path or stdout.\n")
                store.append_event("check.workspace_mutated", {
                    "criterion": x["id"], "cmd": ch["cmd"],
                    "before_revision": before_rev, "after_revision": after_rev,
                })
            results.append(r)
            try:
                record_evidence(store, kind="check", criterion_ids=[x["id"]],
                                summary=f"CHECK {ch['cmd'][:120]} -> exit {r['exit_code']} ({'pass' if r['passed'] else 'FAIL'})",
                                revision=after_rev, submitted_by=submitted_by, command=ch["cmd"], exit_code=r["exit_code"],
                                stdout=r["stdout"], stderr=r["stderr"], data={"passed": r["passed"], "timed_out": r["timed_out"]})
            except EvidenceError as e:
                log(store, f"check evidence rejected: {e}")
    return results


# ---------------------------------------------------------------------------- session launching
def _child_env(store: RunStore, session_id: str, role: str, ttl: int,
               driver_name: str | None = None) -> dict:
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SIMPLE"):
        env.pop(k, None)
    st = store.read()
    env[ENV_TOKEN] = mint(store.secret() or "", run_id=store.run_id, session_id=session_id, role=role,
                          controller_pid=os.getpid(), ttl_seconds=ttl, contract_hash=st.get("contract_hash"))
    env["LONGRUN_RUN_ID"] = store.run_id
    env["LONGRUN_SESSION_MARKER"] = f"{store.run_id}:{session_id}"
    env["LONGRUN_ROLE"] = role
    env["LONGRUN_HOME"] = str(data_root())
    # A Codex child needs the account credential, but must not inherit the
    # user's interactive config (remote MCPs, desktop hooks, experimental
    # features).  Such inherited startup work has blocked planners before
    # their first action.  Give each run a credential-only CODEX_HOME.
    if driver_name == "codex":
        st_home = store.tmp_dir / "codex-home"
        st_home.mkdir(parents=True, exist_ok=True)
        auth_src = Path.home() / ".codex" / "auth.json"
        auth_dst = st_home / "auth.json"
        if auth_src.is_file() and not auth_dst.exists():
            shutil.copy2(auth_src, auth_dst)
            auth_dst.chmod(0o600)
        env["CODEX_HOME"] = str(st_home)
    if driver_name == "opencode":
        # Harness-scoped only: ordinary interactive OpenCode sessions keep their
        # own settings.  Auto approval cannot override these explicit denies.
        builder_bash = {
            "*": "allow",
            "git push*": "deny", "git reset --hard*": "deny",
            "git checkout *": "deny", "git rebase*": "deny",
            "git worktree*": "deny", "rm *": "deny",
            # Paid-resource contracts may need one repository-owned,
            # ownership-scoped deadline/cost watchdog to survive the builder
            # process group.  The builder prompt limits this exception; keep
            # launchctl denied and keep nested Longrun controllers denied.
            "launchctl *": "deny",
            # A paid-resource watchdog must be able to drop only the session
            # cleanup marker before setsid; otherwise the finally block below
            # kills the watchdog even though it escaped the process group.
            # The builder prompt scopes this to one receipt-bound watchdog.
            "*LONGRUN_SESSION_MARKER*": "deny",
            "env -u LONGRUN_SESSION_MARKER nohup setsid *": "allow",
            "longrun run*": "deny", "longrun go*": "deny",
            "longrun evaluate*": "deny", "longrun checkpoint*": "deny",
            "longrun stop*": "deny", "longrun reset*": "deny",
            "longrun contract rebase*": "deny", "longrun uninstall*": "deny",
        }
        permission = ({"*": "allow", "question": "deny", "doom_loop": "deny",
                       "external_directory": "deny", "bash": builder_bash}
                      if role == "builder" else
                      {"*": "allow", "edit": "deny", "bash": "deny",
                       "external_directory": "deny", "question": "deny", "task": "deny"})
        config_content = {"permission": permission}
        contract_doc = {}
        if store.contract_path().is_file():
            try:
                contract_doc = json.loads(store.contract_path().read_text())
            except (OSError, json.JSONDecodeError):
                contract_doc = {}
        swarm_cfg = swarm_config_from_contract(contract_doc) if role == "builder" else {}
        if swarm_cfg:
            # Background agents outlive individual manager turns only when the
            # run owns a persistent OpenCode server. launch_session provides it.
            env["OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS"] = "true"
            config_content["agent"] = {
                "swarm-researcher": {
                    "description": "Read-only independent investigator for one manager-assigned Skyline shard.",
                    "mode": "subagent", "steps": 30,
                    "permission": {"*": "deny", "read": "allow", "grep": "allow",
                                   "glob": "allow", "lsp": "allow", "webfetch": "allow",
                                   "websearch": "allow"},
                },
                "swarm-worker": {
                    "description": "Implementation worker restricted to the exclusive shard owned in its prompt.",
                    "mode": "subagent", "steps": 60,
                    "permission": {"*": "allow", "question": "deny", "task": "deny",
                                   "external_directory": "deny", "bash": builder_bash},
                },
            }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config_content)
        env["OPENCODE_AUTO_SHARE"] = "false"
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
        env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] = "true"
    return env


def _codex_builder_writable_dirs(store: RunStore, workspace: Path) -> list[Path]:
    """Return only the extra paths a Codex builder needs beyond its workspace.

    Git worktrees keep their index and common object store outside the checked-out
    directory, while receipt submission writes only under the run evidence dir.
    Without these narrow grants a builder can edit code but cannot commit or
    submit the evidence that lets the controller evaluate that code.
    """
    # `longrun observe` takes a short-lived lock and appends an authenticated
    # receipt under the run directory.  The previous evidence-only grant made
    # a builder's own observation fail before it could report Unity failures.
    dirs = [store.dir]
    if not G.is_git_repo(workspace):
        return dirs
    for arg in ("--git-dir", "--git-common-dir"):
        proc = subprocess.run(["git", "rev-parse", "--path-format=absolute", arg], cwd=workspace,
                              capture_output=True, text=True)
        candidate = Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
        if candidate and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _register_child(store: RunStore, session_id: str, role: str) -> None:
    with store.transaction() as s:
        s["children"].append({"session_id": session_id, "role": role, "pid": None, "pgid": None,
                              "started_at": now_iso(), "ended_at": None, "exit": None})
        s["counters"]["child_sessions"] = s["counters"].get("child_sessions", 0) + 1


def _child_started(store: RunStore, session_id: str, pid: int, pgid: int | None) -> None:
    with store.transaction() as s:
        for c in s["children"]:
            if c["session_id"] == session_id and c["ended_at"] is None:
                c["pid"] = pid; c["pgid"] = pgid


def _child_ended(store: RunStore, session_id: str, rc, extra: dict | None = None) -> None:
    with store.transaction() as s:
        for c in s["children"]:
            if c["session_id"] == session_id and c["ended_at"] is None:
                c["ended_at"] = now_iso(); c["exit"] = rc
                if extra:
                    c.update(extra)



# Per-model limits are phrased differently from account limits and were missed entirely: the message that ends a
# Fable session is "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
# — no "usage limit", no reset time, so the strategic fallback never fired for the one case it was built for.
_LIMIT_RE = __import__("re").compile(r"(usage|session|rate)\s*limit|reached your\b[^.\n]{0,40}\blimit|/usage-credits|"
                                    r"resets?\s+(at\s+)?\d{1,2}(:\d{2})?\s*(am|pm)|overloaded|529|too many requests|"
                                    r"ECONNRESET|ENOTFOUND|getaddrinfo|network error|fetch failed", __import__("re").I)
_RESET_RE = __import__("re").compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", __import__("re").I)
_TERMINAL_QUOTA_RE = __import__("re").compile(
    r"you(?:'ve| have)\s+hit\s+your\s+weekly\s+limit", __import__("re").I)
_NON_RETRYABLE_PROVIDER_RE = __import__("re").compile(
    r"\b(?:invalid_json_schema|invalid_request_error|unsupported_parameter|model_not_found|"
    r"authentication_error|permission_error)\b", __import__("re").I)
_RETRYABLE_PROVIDER_TRANSPORT_RE = __import__("re").compile(
    r"ProviderResponseStreamError|finish_reason:\s*network_error|\bnetwork[_ ]error\b|"
    r"ECONNRESET|ENOTFOUND|getaddrinfo|fetch failed", __import__("re").I)


def _non_retryable_provider_error(text: str) -> bool:
    """Classify deterministic request/config failures, not transient provider failures."""
    return bool(_NON_RETRYABLE_PROVIDER_RE.search(text or ""))


def _retryable_provider_transport_error(text: str) -> bool:
    """Recognize explicit transient transport failures emitted by provider CLIs."""
    return bool(_RETRYABLE_PROVIDER_TRANSPORT_RE.search(text or ""))


def _infra_failure(res, n_actions: int, text: str, *, produced_evidence: bool = True) -> tuple[bool, int]:
    """A child that did no work and died quickly (or reports a provider/usage limit) is an infrastructure failure,
    not a build round. Returns (is_infra, seconds_to_wait).

    A usage limit can also land in the middle of a session that already did work: the child then dies with dozens
    of actions behind it and nothing submitted. Charging that as a round is wrong twice over — it burns a round and
    it tells the builder to change hypothesis because of someone else's rate limit."""
    if res.interrupted:
        return False, 0
    # A full child timeout with no actions and no submitted evidence is a silent
    # provider/model failure, not a meaningful planner/evaluator/build round.
    # Treat it as infrastructure so strategic roles can use their configured
    # fallback instead of spending the next outer attempt on the same silent
    # model.  A timeout after real work remains a real round.
    if res.timed_out:
        return (True, 0) if n_actions == 0 and not produced_evidence else (False, 0)
    if res.exit_code == 0:
        return False, 0
    text = text or ""
    if n_actions > 0:
        if produced_evidence or not _LIMIT_RE.search(text):
            return False, 0                     # real work happened: this is a round, however it ended
    elif res.duration_s > 90 and not _LIMIT_RE.search(text):
        return False, 0
    m = _RESET_RE.search(text)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        h = h % 12 + (12 if ap == "pm" else 0)
        now = time.localtime(); target = time.mktime(now[:3] + (h, mi, 0) + now[6:])
        if target < time.time() - 60:
            target += 86400
        return True, int(max(60, target - time.time() + 90))
    return True, 120


def _opencode_recovery_reason(res, summary: dict, *, produced_evidence: bool) -> str | None:
    """Classify an interrupted OpenCode builder transport without judging its work.

    The workspace is durable and the Codex evaluator is still the only component
    that can accept it.  Until candidate evidence exists, an abnormal CLI end is
    therefore safe to resume inside the same Longrun round instead of charging a
    repair round for a provider/transport failure.
    """
    if produced_evidence or res.interrupted:
        return None
    if getattr(res, "idle_timed_out", False):
        return "stream_idle"
    if res.timed_out:
        return "child_timeout"
    if res.exit_code not in (0, None):
        return f"process_exit_{res.exit_code}"
    if summary.get("is_error"):
        return "provider_error"
    finish_reason = summary.get("finish_reason")
    if finish_reason in ("length", "max_tokens"):
        return f"truncated_{finish_reason}"
    if summary.get("num_turns") and not summary.get("terminal"):
        return "premature_eof"
    if not summary.get("num_turns") and not str(summary.get("text") or "").strip():
        return "empty_exit"
    return None


def _codex_sol_stall_action(model: str | None, idle_timed_out: bool, prior_stalls: int) -> str | None:
    if model != "gpt-5.6-sol" or not idle_timed_out:
        return None
    return "retry_same_sol" if prior_stalls == 0 else "stop"


def _strategic_initial_progress_timeout_seconds(role: str) -> int:
    """Allow contract repair enough time to reason before its first stream event.

    Contract repair receives the original goal, rejected spec, reviewer findings,
    and strict output schema.  Sol can legitimately spend longer than the normal
    strategic startup window reasoning over that payload before emitting an item.
    """
    return 300 if role == "contract_repair" else 90


def _session_timeout_seconds(deadline_epoch: float, budgets: dict, role: str,
                             strategic_codex: bool, *, now: float | None = None) -> int:
    """Cap every child attempt, including transport retries, by the run's remaining wall budget."""
    remaining = deadline_epoch - (time.time() if now is None else now)
    if remaining <= 30:
        return 0
    configured = budgets["child_timeout_seconds"] if role == "builder" else budgets.get("evaluator_timeout_seconds", 900)
    return min(int(configured), int(remaining))


def _progress_deadline_reason(*, elapsed_s: float, turns_without_progress: int,
                              deadline_s: int, turn_limit: int) -> str | None:
    if turn_limit > 0 and turns_without_progress >= turn_limit:
        return f"no reviewable artifact or test evidence for {turns_without_progress} turns"
    if deadline_s > 0 and elapsed_s >= deadline_s:
        return f"no reviewable artifact or test evidence for {int(elapsed_s)} seconds"
    return None


def _evidence_progress_fingerprint(evidence: dict) -> str | None:
    """Identify a real, successful result while ignoring ledger-only churn.

    IDs, timestamps, summaries, criterion links, copied paths, and artifact
    filenames are deliberately excluded: changing those does not create a new
    artifact or reality test.  This keeps duplicate evidence submissions from
    resetting the live progress deadline.
    """
    kind = evidence.get("kind")
    artifact_hashes = sorted({
        str(a.get("sha256")) for a in (evidence.get("artifacts") or [])
        if isinstance(a, dict) and a.get("sha256")
    })
    stdout_hash = evidence.get("stdout_sha256")
    data = evidence.get("data") if evidence.get("data") else None
    if kind in {"artifact", "screenshot", "video", "capture_manifest", "metric"}:
        if not artifact_hashes:
            return None
    else:
        return None

    payload = {
        "kind": kind,
        "artifact_hashes": artifact_hashes,
        "stdout_sha256": stdout_hash if artifact_hashes else None,
        "data": data if artifact_hashes else None,
        "command": None,
        "exit_code": None,
        "revision": None,
    }
    return sha256_bytes(canonical_json(payload).encode())


def _has_cumulative_run_delta(current_revision: str, baseline_revision: str | None) -> bool:
    """True only after workspace bytes diverge from the exact frozen baseline."""
    return bool(baseline_revision) and current_revision != baseline_revision


def launch_session(store: RunStore, runner: ChildRunner, *, role: str, prompt: str, json_schema: dict | None = None,
                   max_turns: int | None = None, model: str | None = None, on_actions=None) -> dict:
    st = store.read()
    contract = json.loads(store.contract_path().read_text()) if store.contract_path().is_file() else {"allowed_commands": [], "budgets": st["budgets"]}
    choice = resolve_model(role, st["driver"], cli_model=model)
    driver = get_driver(choice["driver"])
    if not driver.available():
        raise ControllerError(f"resolved driver {choice['driver']!r} for {role} but its executable is unavailable")
    model = choice["model"]
    ws = Path(st["workspace"] or st["project_root"])
    sid = driver.new_session_id()
    b = st["budgets"]
    deadline_epoch = st["deadline_epoch"] or (time.time() + b["wall_time_seconds"])
    strategic_codex = choice["driver"] == "codex" and choice["tier"] == "strategic"
    timeout = _session_timeout_seconds(deadline_epoch, b, role, strategic_codex)
    if timeout <= 0:
        raise ControllerError("wall-time budget exhausted before launching session")
    opencode_builder = choice["driver"] == "opencode" and role == "builder"
    swarm_cfg = swarm_config_from_contract(contract) if opencode_builder else {}
    deny = [str(keys_root()) + "/**", str(store.dir) + "/state.json", str(store.dir) + "/events.jsonl", str(store.dir) + "/contract*.json"]
    extra = {"effort": choice["effort"]}
    if choice["driver"] == "codex":
        extra.update({"schema_path": store.tmp_dir / f"schema-{sid}.json", "last_message_path": store.sessions_dir / f"{sid}.last.txt",
                      "writable_dirs": _codex_builder_writable_dirs(store, ws) if role == "builder" else [],
                      # A builder is an owner-authorized longrun worker.  It must
                      # be able to operate local tools and daemons (Unity needs a
                      # /tmp UPM socket and per-user licensing state).  The prior
                      # workspace sandbox caused false build failures.  Planning
                      # and evaluation remain read-only via the driver.
                      "sandbox_mode": "danger-full-access" if role == "builder" else "workspace-write"})
    turn_limit = int(max_turns or b.get("max_turns_per_session", 120))
    try:
        cmd = driver.build_command(role=role, prompt=prompt, session_id=sid, cwd=ws,
                                   max_turns=turn_limit,
                                   allowed_commands=contract.get("allowed_commands", []), deny_paths=deny, model=model,
                                   json_schema=json_schema, max_budget_usd=b.get("max_cost_usd"),
                                   permission_mode=st.get("permission_mode") or _cfg().get("default_permission_mode", "acceptEdits"), **extra)
    except ValueError as e:
        raise NonRetryableProviderRequestError(str(e)) from e
    _register_child(store, sid, role)
    env = _child_env(store, sid, role, ttl=timeout + 600, driver_name=choice["driver"])
    opencode_server = None
    if swarm_cfg:
        opencode_server = _start_opencode_server(store, sid=sid, cwd=ws, env=env)
        extra["attach_url"] = opencode_server["url"]
        cmd = driver.build_command(
            role=role, prompt=prompt, session_id=sid, cwd=ws, max_turns=turn_limit,
            allowed_commands=contract.get("allowed_commands", []), deny_paths=deny, model=model,
            json_schema=json_schema, max_budget_usd=b.get("max_cost_usd"),
            permission_mode=st.get("permission_mode") or _cfg().get("default_permission_mode", "acceptEdits"),
            **extra,
        )
        store.append_event("session.opencode_server", {
            "session_id": sid, "role": role, "url": opencode_server["url"],
        })
    out_p = store.sessions_dir / f"{sid}.{role}.stream.jsonl"
    err_p = store.sessions_dir / f"{sid}.{role}.stderr.txt"
    (store.sessions_dir / f"{sid}.{role}.cmd.txt").write_text(" ".join(shlex.quote(c) for c in cmd))
    store.append_event("session.launch", {"session_id": sid, "role": role, "timeout_s": timeout, "driver": choice["driver"],
                                          "model": model, "effort": choice["effort"], "model_source": choice["source"]})
    # The complete stream is already written to out_p by ChildRunner. Keep only
    # a bounded window in the controller for the live loop guard, then parse the
    # file once when the attempt ends. Re-parsing an ever-growing in-memory list
    # every 25 events made long OpenCode sessions quadratic.
    guard_lines = deque(maxlen=2000)
    stream_progress = {"lines": 0}
    stop_flag = {"fired": False, "reasons": []}
    turn_cap = {"count": 0, "fired": False}
    # Swarm ledger persists across transport attempts (the persistent server
    # owns the background children); `attempt_base_turn` keeps the live
    # watchdog per-attempt so a resumed stream is not judged by the previous
    # attempt's cumulative turn count.
    live_swarm = {"actions": [], "attempt_base_turn": 0, "last_research_turn": 0,
                  "dispatch_stalled": False}
    submission_session_ids = {sid}
    ev_at_start = len(list_evidence(store))
    open_at_start = {cid for cid, state in (st.get("criteria") or {}).items()
                     if state.get("status") != "PASS"}
    prior_progress_fingerprints = {
        fp for e in list_evidence(store)
        if bool(open_at_start.intersection(e.get("criterion_ids") or []))
        for fp in [_evidence_progress_fingerprint(e)] if fp is not None
    }
    progress_guard = {
        "last_at": time.monotonic(), "last_turn": 0, "fingerprints": prior_progress_fingerprints,
        "last_scan_at": 0.0, "event_written": False,
    }
    # OpenCode reports a tool event only after its foreground subprocess exits.
    # Wall-clock no-progress killing here would destroy legitimate Unity/GPU
    # work. Model-controlled churn is bounded by turns; foreground work is
    # bounded by the child timeout.
    progress_deadline_s = 0
    progress_turn_limit = int(b.get("max_turns_without_progress", 24))
    qualifying_progress_kinds = {
        "artifact", "build", "screenshot", "video", "capture_manifest", "metric", "test",
    }

    def refresh_progress_guard(*, force: bool = False) -> None:
        if role != "builder" or stop_flag["fired"]:
            return
        now = time.monotonic()
        if not force and now - progress_guard["last_scan_at"] < 1.0:
            return
        progress_guard["last_scan_at"] = now
        open_criteria = {cid for cid, state in (st.get("criteria") or {}).items()
                         if state.get("status") != "PASS"}
        fingerprints = {
            fp for e in list_evidence(store)
            if e.get("submitted_by") in submission_session_ids and e.get("kind") in qualifying_progress_kinds
            and bool(open_criteria.intersection(e.get("criterion_ids") or []))
            for fp in [_evidence_progress_fingerprint(e)] if fp is not None
        }
        if not fingerprints.issubset(progress_guard["fingerprints"]):
            progress_guard["fingerprints"].update(fingerprints)
            progress_guard["last_at"] = now
            progress_guard["last_turn"] = turn_cap["count"]
            return
        elapsed = now - progress_guard["last_at"]
        turns = turn_cap["count"] - progress_guard["last_turn"]
        reason = _progress_deadline_reason(
            elapsed_s=elapsed, turns_without_progress=turns,
            deadline_s=progress_deadline_s, turn_limit=progress_turn_limit)
        if reason:
            stop_flag["fired"] = True
            stop_flag["reasons"] = [reason]
            if not progress_guard["event_written"]:
                progress_guard["event_written"] = True
                store.append_event("session.progress_deadline", {
                    "session_id": sid, "role": role, "reason": reason,
                    "turns_without_progress": turns,
                    "seconds_without_progress": round(elapsed, 1),
                    "unique_progress_results": len(progress_guard["fingerprints"]),
                })

    def on_line(line: str):
        guard_lines.append(line)
        stream_progress["lines"] += 1
        if choice["driver"] == "opencode" and line.startswith("{"):
            try:
                event = json.loads(line)
                if event.get("type") == "step_start":
                    turn_cap["count"] += 1
                    turn_cap["fired"] = turn_cap["count"] > turn_limit
                elif swarm_cfg and event.get("type") == "tool_use":
                    part = event.get("part") or {}
                    state = part.get("state") or {}
                    inp = state.get("input") or {}
                    if str(part.get("tool") or "").lower() == "task":
                        action = {
                            "tool": "task", "input": inp,
                            "is_error": state.get("status") == "error",
                            "child_session_id": (state.get("metadata") or {}).get("sessionId"),
                        }
                        live_swarm["actions"].append(action)
                        if re.search(r"\bR\d{2}\b", " ".join(
                                str(inp.get(k) or "") for k in ("description", "prompt")), re.I):
                            live_swarm["last_research_turn"] = (turn_cap["count"] -
                                                                live_swarm["attempt_base_turn"])
            except json.JSONDecodeError:
                pass
        if (swarm_cfg and not live_swarm["dispatch_stalled"] and
                swarm_research_dispatch_stalled(
                    live_swarm["actions"], swarm_cfg,
                    manager_turns=turn_cap["count"] - live_swarm["attempt_base_turn"],
                    last_research_turn=live_swarm["last_research_turn"])):
            live_swarm["dispatch_stalled"] = True
            stop_flag["fired"] = True
            stop_flag["reasons"] = ["swarm research wave underfilled for six manager turns"]
            store.append_event("session.swarm_nudge", {
                "session_id": sid, "role": role, "manager_turns": turn_cap["count"],
                "launched": analyze_swarm(live_swarm["actions"], swarm_cfg).get("launched", []),
                "reason": "research_dispatch_stalled",
            })
        refresh_progress_guard()
        if on_actions and stream_progress["lines"] % 25 == 0:
            parsed = driver.parse_stream(guard_lines)
            # Count evidence submitted so far in THIS session, exactly as the end-of-round check at the bottom
            # of run_loop does. Without it `same_file_edits_without_evidence` sees an empty map, so a builder
            # that edited one file 12 times was killed mid-session even when it had been submitting evidence
            # all along — measured over 98 builder sessions, this was 24 of the 30 recorded guard firings,
            # by far the largest single waste in the harness.
            new_ev = max(0, sum(1 for d in store.evidence_dir.iterdir()
                                if d.is_dir() and (d / "record.json").is_file()) - ev_at_start)
            files = {a["file"] for a in parsed["actions"] if a.get("file")}
            rep = analyze_stream(parsed["actions"], {f: new_ev for f in files})
            if rep.fired:
                stop_flag["fired"] = True; stop_flag["reasons"] = rep.reasons

    def should_stop() -> bool:
        refresh_progress_guard()
        if stop_flag["fired"]:
            return True
        if turn_cap["fired"]:
            return True
        return (store.dir / "STOP").exists()

    def on_idle_heartbeat(progress: dict) -> None:
        store.append_event("session.stream_idle_heartbeat", {
            "session_id": sid, "role": role, "driver": choice["driver"], "model": model, **progress})
        log(store, f"{role}: {model} still running; stream idle {progress['idle_s']}s "
                   f"({progress['stream_lines']} lines, {progress['stream_bytes']} bytes observed)")

    def is_initial_strategic_progress(line: str) -> bool:
        """Lifecycle acknowledgements prove transport setup, not model work."""
        try:
            event_type = json.loads(line).get("type")
        except json.JSONDecodeError:
            return bool(line.strip())
        return event_type not in {"thread.started", "turn.started"}

    runner.on_child_start = lambda pid, pgid: _child_started(store, sid, pid, pgid)
    infra_tries = 0
    sol_stalls = 0
    resumed_opencode_sessions: set[str] = set()
    session_wall_started = time.monotonic()
    total_cost_usd = 0.0
    swarm_actions: list[dict] = []
    swarm_report: dict = {}
    terminal_quota = None
    terminal_stall = None
    terminal_request = None
    terminal_swarm_exhaustion = None
    sid_closed = False
    try:
        while True:
            try:
                res = runner.run(cmd, cwd=ws, env=env, timeout_s=timeout, stdout_path=out_p, stderr_path=err_p,
                                 should_stop=should_stop, on_stdout_line=on_line,
                             # OpenCode emits a tool event only after the subprocess exits. A
                             # legitimate Unity build is therefore indistinguishable from a
                             # silent model by stdout alone; observe it, but do not kill it on
                             # an idle-stream timer. Actual EOF/error/exit is recovered below.
                             idle_timeout_s=900 if strategic_codex else None,
                             idle_heartbeat_s=300 if strategic_codex else (180 if opencode_builder else None),
                             on_idle_heartbeat=on_idle_heartbeat,
                             # A Codex thread/turn acknowledgement can be followed by a transport that
                             # never delivers a model event.  Ninety seconds admits normal startup and
                             # repository reads, while avoiding the former 900-second blind wait.
                                 initial_progress_timeout_s=(
                                     _strategic_initial_progress_timeout_seconds(role)
                                     if strategic_codex else None
                                 ),
                                 is_initial_progress_line=is_initial_strategic_progress if strategic_codex else None)
            finally:
                # The persistent server owns background subagents across manager
                # retries. Clean the whole marker only after the logical session.
                escaped = ([] if opencode_server else
                           cleanup_processes_with_env_marker(env["LONGRUN_SESSION_MARKER"]))
                if escaped:
                    store.append_event("session.escaped_processes_cleaned", {
                        "session_id": sid, "role": role, "pids": escaped})
            with out_p.open("r", encoding="utf-8", errors="replace") as stream_fh:
                parsed = driver.parse_stream(stream_fh)
            summ = driver.summarize_result(parsed["result"])
            if swarm_cfg:
                swarm_actions.extend(a for a in parsed["actions"] if str(a.get("tool") or "").lower() == "task")
                swarm_report = analyze_swarm(swarm_actions, swarm_cfg, summ.get("text") or "")
                store.append_event("session.swarm_progress", {
                    "session_id": sid, "role": role, "launched": swarm_report.get("launched", []),
                    "task_ids": swarm_report.get("task_ids", {}),
                    "missing_researchers": swarm_report.get("missing_researchers", []),
                    "missing_workers": swarm_report.get("missing_workers", []),
                    "wrong_mode": swarm_report.get("wrong_mode", []),
                    "wrong_type": swarm_report.get("wrong_type", []),
                    "task_errors": swarm_report.get("task_errors", []),
                    "done_marker": swarm_report.get("done_marker", False),
                    "blocked_marker": swarm_report.get("blocked_marker", False),
                })
            total_cost_usd += float(summ.get("cost_usd") or 0.0)
            err_txt = _tail_text(err_p, 4000)
            submitted = any(e.get("submitted_by") in submission_session_ids for e in list_evidence(store))
            provider_error_text = summ.get("provider_error_text") or ""
            failure_text = (summ.get("text") or "") + "\n" + provider_error_text + "\n" + err_txt
            if _TERMINAL_QUOTA_RE.search(failure_text):
                terminal_quota = (summ.get("text") or err_txt or "weekly quota exhausted").strip()[-500:]
                store.append_event("session.terminal_quota", {
                    "session_id": sid, "role": role, "driver": choice["driver"], "message": terminal_quota})
                log(store, f"{role}: terminal provider quota; stopping without retry")
                break
            if provider_error_text and _non_retryable_provider_error(provider_error_text):
                terminal_request = provider_error_text.strip()[-1000:]
                store.append_event("session.non_retryable_provider_request", {
                    "session_id": sid, "role": role, "driver": choice["driver"],
                    "message": terminal_request})
                log(store, f"{role}: provider rejected a deterministic request/configuration; stopping without retry")
                break
            infra, wait_s = _infra_failure(res, len(parsed["actions"]), failure_text,
                                           produced_evidence=submitted)
            opencode_recovery = (_opencode_recovery_reason(res, summ, produced_evidence=submitted)
                                 if opencode_builder else None)
            if swarm_cfg:
                opencode_recovery = opencode_recovery or swarm_recovery_reason(swarm_report, summ)
                if live_swarm["dispatch_stalled"]:
                    opencode_recovery = "swarm_research_dispatch_stalled"
            if opencode_recovery:
                infra = True
                wait_s = (2 if opencode_recovery.startswith("swarm_") or opencode_recovery.startswith("clean_stop_")
                          else (15, 60, 180)[min(infra_tries, 2)])
            strategic_stall = bool(getattr(res, "idle_timed_out", False) or
                                   getattr(res, "initial_progress_timed_out", False))
            sol_stall_action = _codex_sol_stall_action(model, strategic_stall, sol_stalls)
            if sol_stall_action == "stop":
                terminal_stall = f"{role} gpt-5.6-sol produced no substantive stream progress in two attempts"
                store.append_event("session.sol_stall_exhausted", {"session_id": sid, "role": role,
                                                                     "model": model, "attempts": 2})
                log(store, terminal_stall + "; stopping without Terra fallback")
                break
            if opencode_builder and _retryable_provider_transport_error(failure_text):
                max_infra_tries = 6
            else:
                max_infra_tries = ((int(swarm_cfg.get("manager_retries", 3)) if swarm_cfg else 2)
                                   if opencode_builder else 6)
            if not infra or infra_tries >= max_infra_tries:
                # Exhausting the manager recovery budget is its own outcome, not an
                # ordinary failed builder round: the journal must say so and the
                # outcome must end honestly instead of buying gate/evaluator/repair
                # rounds on a manager that never dispatched the swarm.
                if swarm_cfg and opencode_recovery and infra_tries >= max_infra_tries:
                    store.append_event("session.swarm_recovery_exhausted", {
                        "session_id": sid, "role": role, "reason": opencode_recovery,
                        "tries": infra_tries, "budget": max_infra_tries,
                    })
                    log(store, f"swarm manager recovery budget exhausted ({opencode_recovery} "
                               f"after {infra_tries}/{max_infra_tries} attempts); surfacing as explicit failure")
                    terminal_swarm_exhaustion = (f"swarm manager recovery budget exhausted after "
                                                 f"{infra_tries} attempts ({opencode_recovery}); "
                                                 f"workspace preserved")
                break
            if opencode_recovery and live_swarm.get("dispatch_stalled"):
                # The live guard raised this stop itself to swap the manager
                # transport; a still-set stop flag would veto its own recovery
                # branch. Owner STOP and the turn cap are re-checked below and win.
                stop_flag["fired"] = False
                stop_flag["reasons"] = []
            if should_stop():
                break
            infra_tries += 1
            if sol_stall_action == "retry_same_sol":
                sol_stalls += 1
                wait_s = 2
                store.append_event("session.sol_stall_retry", {"session_id": sid, "role": role,
                                                                 "model": model, "attempt": 1})
                initial_cutoff = _strategic_initial_progress_timeout_seconds(role)
                stall_window = (f"before the {initial_cutoff}s initial-progress cutoff"
                                if getattr(res, "initial_progress_timed_out", False) else "for 900s")
                log(store, f"{role}: gpt-5.6-sol stream stalled {stall_window}; retrying the same model once")
            wait_s = min(wait_s, 4 * 3600)
            fb = choice.get("fallback")
            silent_timeout = res.timed_out and len(parsed["actions"]) == 0 and not submitted
            if sol_stall_action != "retry_same_sol" and fb and model != fb["model"] and (_LIMIT_RE.search((summ.get("text") or "") + err_txt) or silent_timeout):
                # the primary strategic model (e.g. fable) hit its own usage limit -> fail over to the fallback model right away
                store.append_event("session.model_failover", {"role": role, "from": model, "to": fb["model"], "effort": fb.get("effort")})
                log(store, f"{role}: {model} unavailable ({(summ.get('text') or err_txt or '').strip()[-100:]!r}); failing over to {fb['model']}/{fb.get('effort')}")
                model, extra["effort"] = fb["model"], fb.get("effort")
                wait_s = 5
            # Provider backoff is part of elapsed wall time. Never let a reset wait
            # itself carry the run beyond the absolute contract deadline.
            wait_s = min(wait_s, max(0, int(deadline_epoch - time.time() - 30)))
            store.append_event("session.infra_wait", {"session_id": sid, "role": role, "try": infra_tries, "wait_s": wait_s,
                                                      "exit": res.exit_code, "hint": (summ.get("text") or err_txt or "")[-300:]})
            if opencode_recovery:
                log(store, f"OpenCode builder transport ended ({opencode_recovery}); preserving the workspace and "
                           f"recovering inside the same round (try {infra_tries}/{max_infra_tries})")
            else:
                log(store, f"{role} session died before doing any work (exit {res.exit_code}: {(summ.get('text') or err_txt or '').strip()[-120:]!r}); "
                           f"treating as infrastructure failure, waiting {wait_s}s then retrying (try {infra_tries}/6)")
            end = time.time() + wait_s
            while time.time() < end:
                if should_stop():
                    break
                time.sleep(min(15, end - time.time()))
            guard_lines.clear(); stream_progress["lines"] = 0; stop_flag["fired"] = False
            # Recovery continues the same logical manager session: keep the
            # accumulated task ledger so already-dispatched shards are never
            # re-counted as missing, and restart the live watchdog window from the
            # current cumulative turn count.
            live_swarm["attempt_base_turn"] = turn_cap["count"]
            live_swarm["last_research_turn"] = 0
            live_swarm["dispatch_stalled"] = False
            # Recovery is part of the same logical builder session: do not grant a
            # fresh turn budget just because the provider transport restarted.
            turn_cap["fired"] = False
            external_session_id = parsed.get("session_id") if opencode_builder else None
            resume_session_id = None
            if external_session_id and external_session_id not in resumed_opencode_sessions:
                resume_session_id = external_session_id
                resumed_opencode_sessions.add(external_session_id)
            recovery_mode = "resume" if resume_session_id else "fresh"
            if opencode_recovery:
                store.append_event("session.opencode_recovery", {
                    "session_id": sid, "external_session_id": external_session_id,
                    "reason": opencode_recovery, "mode": recovery_mode, "try": infra_tries,
                })
            _child_ended(store, sid, res.exit_code, {
                "timed_out": res.timed_out, "idle_timed_out": getattr(res, "idle_timed_out", False),
                "duration_s": round(res.duration_s, 1), "cost_usd": summ.get("cost_usd"),
                "num_turns": summ.get("num_turns"), "external_session_id": external_session_id,
                "recovery_reason": opencode_recovery,
            })
            sid_closed = True
            timeout = _session_timeout_seconds(deadline_epoch, b, role, strategic_codex)
            if timeout <= 0:
                store.append_event("session.recovery_budget_exhausted", {
                    "session_id": sid, "role": role, "driver": choice["driver"], "try": infra_tries,
                })
                log(store, f"{role}: wall-time budget exhausted before the next infrastructure recovery attempt")
                break
            sid = driver.new_session_id()
            submission_session_ids.add(sid)
            if choice["driver"] == "codex":
                # Retry artifacts are session-scoped. Reusing the first attempt's
                # paths makes the final summary read stale/empty output and can turn
                # a successful fallback into another apparent invalid response.
                extra["schema_path"] = store.tmp_dir / f"schema-{sid}.json"
                extra["last_message_path"] = store.sessions_dir / f"{sid}.last.txt"
            retry_prompt = prompt
            retry_extra = dict(extra)
            if opencode_builder:
                recovery_note = (swarm_corrective_note(swarm_report) if swarm_cfg and
                                 (opencode_recovery.startswith("swarm_") or opencode_recovery.startswith("clean_stop_")) else
                                 "The previous OpenCode transport ended before the Longrun builder task was complete. "
                                 "Continue from the current workspace exactly as it stands. Inspect the current diff and "
                                 "existing evidence, do not restart or revert completed work, finish the remaining contract "
                                 "criteria, run the required checks, and submit candidate evidence before stopping.")
                retry_prompt = recovery_note if resume_session_id else f"{prompt}\n\nRECOVERY AFTER TRANSPORT FAILURE:\n{recovery_note}"
                retry_extra["resume_session_id"] = resume_session_id
            try:
                cmd = driver.build_command(role=role, prompt=retry_prompt, session_id=sid, cwd=ws,
                                           max_turns=int(max_turns or b.get("max_turns_per_session", 120)),
                                           allowed_commands=contract.get("allowed_commands", []), deny_paths=deny, model=model,
                                           json_schema=json_schema, max_budget_usd=b.get("max_cost_usd"),
                                           permission_mode=st.get("permission_mode") or _cfg().get("default_permission_mode", "acceptEdits"), **retry_extra)
            except ValueError as e:
                raise NonRetryableProviderRequestError(str(e)) from e
            _register_child(store, sid, role)
            sid_closed = False
            if not opencode_server:
                env = _child_env(store, sid, role, ttl=timeout + 600, driver_name=choice["driver"])
            out_p = store.sessions_dir / f"{sid}.{role}.stream.jsonl"; err_p = store.sessions_dir / f"{sid}.{role}.stderr.txt"
            (store.sessions_dir / f"{sid}.{role}.cmd.txt").write_text(" ".join(shlex.quote(c) for c in cmd))
            # Log the effort actually on the command line, not the one first resolved: a failover changes both the
            # model and the effort, and recording the stale effort makes the journal misdescribe what ran. Reading
            # a journal that misdescribed which model served a night is how the failover gap stayed hidden.
            store.append_event("session.launch", {"session_id": sid, "role": role, "timeout_s": timeout, "driver": choice["driver"],
                                                  "model": model, "effort": extra.get("effort", choice["effort"]),
                                                  "model_source": choice["source"], "retry_of_infra": True})
            runner.on_child_start = lambda pid, pgid: _child_started(store, sid, pid, pgid)
    finally:
        # Guaranteed for PASS, FAIL, interrupt, and exception: the persistent
        # server owns the background swarm children and must never outlive the
        # logical session.
        if opencode_server:
            _stop_opencode_server(opencode_server)
            escaped = cleanup_processes_with_env_marker(env["LONGRUN_SESSION_MARKER"])
            store.append_event("session.opencode_server_stopped", {
                "session_id": sid, "role": role, "escaped_pids": escaped,
            })
    if choice["driver"] == "codex" and extra.get("last_message_path") and Path(extra["last_message_path"]).is_file():
        summ["text"] = Path(extra["last_message_path"]).read_text()
        try:
            summ["structured_output"] = json.loads(summ["text"]) if summ["text"].strip().startswith("{") else None
        except json.JSONDecodeError:
            summ["structured_output"] = None
    if not sid_closed:
        _child_ended(store, sid, res.exit_code, {"timed_out": res.timed_out, "duration_s": round(res.duration_s, 1),
                                                 "cost_usd": summ.get("cost_usd"), "num_turns": summ.get("num_turns"),
                                                 "loop_kill": stop_flag["fired"], "external_session_id": parsed.get("session_id")})
    with store.transaction() as s:
        s["counters"]["cost_usd"] = round(float(s["counters"].get("cost_usd", 0.0)) + total_cost_usd, 4)
        s["counters"]["wall_seconds"] = round(
            float(s["counters"].get("wall_seconds", 0.0)) + (time.monotonic() - session_wall_started), 1)
    store.append_event("session.end", {"session_id": sid, "role": role, "exit": res.exit_code, "timed_out": res.timed_out,
                                       "interrupted": res.interrupted, "duration_s": round(res.duration_s, 1),
                                       "cost_usd": summ.get("cost_usd"), "num_turns": summ.get("num_turns"),
                                       "actions": len(parsed["actions"]), "loop_kill": stop_flag["fired"], "loop_reasons": stop_flag["reasons"]})
    if res.interrupted:
        raise Interrupted()
    if terminal_quota:
        raise TerminalQuotaError(terminal_quota)
    if terminal_request:
        raise NonRetryableProviderRequestError(terminal_request)
    if terminal_stall:
        raise StrategicModelStalledError(terminal_stall)
    if terminal_swarm_exhaustion:
        # Reaches _run_loop as a ControllerError, which finishes the outcome
        # FAILED with this exact reason instead of continuing to gate,
        # evaluate, or repair an unstarted swarm.
        raise ControllerError(terminal_swarm_exhaustion)
    return {"session_id": sid, "result": res, "summary": summ, "actions": parsed["actions"], "loop_kill": stop_flag}


# ---------------------------------------------------------------------------- evaluation
def evaluate(store: RunStore, runner: ChildRunner, *, model: str | None = None, force: bool = False) -> dict:
    """Run deterministic checks, then a fresh evaluator; validate; apply transitions. Returns
    {verdict|None, delta|None, skipped:bool, error:str|None}."""
    st = store.read()
    if st["status"] not in ("FROZEN", "RUNNING", "EVALUATING", "REPAIRING", "RESTARTING"):
        raise ControllerError(f"cannot evaluate in state {st['status']}")
    contract = json.loads(store.contract_path().read_text())
    ws = Path(st["workspace"] or st["project_root"])
    adapter = load_adapter(st["adapter"], contract.get("adapter_config"))
    det = run_deterministic_checks(store, submitted_by="controller:evaluate")
    base_green = {c for c, ok in (st.get("baseline_check_results") or {}).items() if ok}
    for r in det:
        r["at_baseline"] = "passed" if r["cmd"] in base_green else ("failed" if r["cmd"] in (st.get("baseline_check_results") or {}) else "not run")
    rev = G.content_revision(ws)
    manifest = evidence_manifest(store, revision=rev)
    dirty_snapshot = store.dir / "baseline-dirty"
    if st["start_revision"] and dirty_snapshot.is_dir():
        diff = G.diff_text_from_dirty_baseline(
            ws, st["start_revision"], dirty_snapshot, exclude_globs=adapter.diff_exclude_globs)
    else:
        diff = G.diff_text(ws, st["start_revision"], exclude_globs=adapter.diff_exclude_globs) if st["start_revision"] else ""
    deterministic_identity = [
        {k: r.get(k) for k in ("criterion", "cmd", "exit_code", "expected_exit", "passed",
                               "expect_stdout_regex", "regex_matched", "timed_out")}
        for r in det
    ]
    mh = manifest_hash(manifest, diff, st["contract_hash"], rev,
                       deterministic_results=deterministic_identity)
    if not force and st["loop"].get("last_eval_input_hash") == mh:
        store.append_event("evaluation.skipped_unchanged", {"input_hash": mh, "revision": rev})
        log(store, "evaluation skipped: inputs unchanged since last evaluation")
        return {"verdict": None, "delta": None, "skipped": True, "error": None, "revision": rev}
    with store.transaction() as s:
        s["status"] = "EVALUATING"
    det_view = []
    for r in det:
        view = {k: r.get(k) for k in ("criterion", "cmd", "exit_code", "expected_exit", "passed", "timed_out",
                                      "expect_stdout_regex", "regex_matched", "at_baseline")}
        # The repair builder must see why a controller-owned check failed.  A
        # truncated tail is enough to diagnose a bad regex/exit expectation and
        # avoids forwarding whole build logs into the evaluator prompt.
        if not r["passed"]:
            view["stdout_tail"] = (r.get("stdout") or "")[-1200:]
            view["stderr_tail"] = (r.get("stderr") or "")[-1200:]
        det_view.append(view)
    standing = st.get("standing_results") or None
    from .referent import referent_report
    referents = referent_report(ws, contract) or None
    if referents:
        unresolved = [(r["criterion"], l["literal"]) for r in referents for l in r["literals"] if not l["resolves"]]
        if unresolved:
            store.append_event("referent.unresolved", {"literals": [{"criterion": c, "literal": t} for c, t in unresolved]})

    def _prompt(retry_error: str | None = None) -> str:
        return evaluator_prompt(contract=contract, contract_hash=st["contract_hash"], run_id=store.run_id, revision=rev,
                                baseline=st["baseline"], evidence_manifest=manifest, diff=diff,
                                adapter_fragment=adapter.evaluator_prompt_fragment(), workspace=ws,
                                deterministic_results=det_view, standing_results=standing, referents=referents,
                                retry_error=retry_error)

    eval_id = f"EV{uuid.uuid4().hex[:8]}"
    store.append_event("evaluation.start", {"id": eval_id, "revision": rev, "input_hash": mh, "evidence_count": len(manifest)})
    err = None
    verdict = None
    raw = None
    first_verdicts: dict[str, str] = {}
    # One retry, and only on a citation-shaped rejection. A rejected verdict is discarded whole — two of fifteen
    # went that way one night, $10.35 of judgement plus the repair round each one bought — and the model has
    # already done the reading, so a corrected citation costs one warm turn. The retry may not improve any
    # criterion's verdict: it exists to fix a citation, not to argue a FAIL into a PASS.
    for attempt in (0, 1):
        sess = launch_session(store, runner, role="evaluator", prompt=_prompt(err if attempt else None),
                              json_schema=EVALUATOR_JSON_SCHEMA, max_turns=40 if attempt == 0 else 12, model=model)
        raw = sess["summary"].get("structured_output")
        err = None
        try:
            obj = raw if isinstance(raw, dict) else extract_json_object(sess["summary"].get("text") or "")
            if attempt == 0 and isinstance(obj, dict) and isinstance(obj.get("criteria"), list):
                first_verdicts = {c.get("id"): c.get("verdict") for c in obj["criteria"] if isinstance(c, dict)}
            verdict = validate_verdict(obj, run_id=store.run_id, contract_hash=st["contract_hash"], evaluated_revision=rev,
                                       contract=contract, evidence_manifest=manifest, baseline_green_commands=base_green)
        except EvaluatorError as e:
            err = str(e)
        if verdict and attempt == 1:
            upgraded = [c["id"] for c in verdict["criteria"]
                        if c["verdict"] == "PASS" and first_verdicts.get(c["id"]) not in (None, "PASS")]
            if upgraded:
                verdict = None
                err = f"retry upgraded {upgraded} to PASS after the first pass did not; discarded"
                store.append_event("evaluation.retry_rejected", {"id": eval_id, "criteria": upgraded})
        if verdict or not err:
            break
        if attempt == 0:
            store.append_event("evaluation.retry", {"id": eval_id, "error": err[:300]})
            log(store, f"evaluator output rejected on citations; retrying once: {err[:160]}")
    # Criteria the contract marked deterministic_only are settled by their checks, not by prose. The policy was
    # validated and offered to the planner but never consumed by anything, so those criteria were judged at full
    # price anyway. Freshness is deliberately NOT required here: deterministic_only is the contract stating that
    # the command is the whole proof, and the honest form of such a criterion is often preservation ("the shift
    # still plays"), where a check that was green before and is green now is exactly the evidence. The stale-check
    # rule belongs to the LLM path, where the evaluator is asserting a judgement rather than reading an exit code.
    # A criterion that passes without anything having moved is still worth seeing, so it is logged.
    if verdict:
        for spec in contract["criteria"]:
            if spec["evaluator_policy"] != "deterministic_only":
                continue
            mine = [r for r in det if r.get("criterion") == spec["id"]]
            fresh = [r for r in mine if r["cmd"] not in base_green]
            resolved = "PASS" if (mine and all(r["passed"] for r in mine)) else "FAIL"
            if resolved == "PASS" and not fresh:
                store.append_event("criterion.passed_without_change",
                                   {"id": eval_id, "criterion": spec["id"],
                                    "note": "every check for this criterion was already green at the frozen baseline"})
            for c in verdict["criteria"]:
                if c["id"] != spec["id"] or c["verdict"] == resolved:
                    continue
                was = c["verdict"]
                store.append_event("evaluation.deterministic_override",
                                   {"id": eval_id, "criterion": spec["id"], "was": was, "now": resolved})
                c["verdict"] = resolved
                failed_details = []
                for r in mine:
                    if r["passed"]:
                        continue
                    detail = (f"cmd={r['cmd']!r}; exit={r.get('exit_code')} expected={r.get('expected_exit')}; "
                              f"regex={r.get('expect_stdout_regex')!r} matched={r.get('regex_matched')}; "
                              f"stdout_tail={(r.get('stdout') or '')[-500:]!r}; stderr_tail={(r.get('stderr') or '')[-300:]!r}")
                    failed_details.append(detail)
                c["reason"] = (f"resolved by the controller from this criterion's deterministic checks "
                               f"({len(fresh)} of {len(mine)} not already green at baseline); "
                               f"evaluator had said {was}. " +
                               (("CHECK FAILURE: " + " | ".join(failed_details) + ". ") if failed_details else "") +
                               c["reason"])[:2000]
        if verdict["overall"] == "PASS" and any(c["verdict"] != "PASS" for c in verdict["criteria"]):
            verdict["overall"] = "NEEDS_REWORK"
        elif all(c["verdict"] == "PASS" for c in verdict["criteria"]):
            verdict["overall"] = "PASS"
    (store.dir / "evaluations").mkdir(exist_ok=True)
    atomic_write_json(store.dir / "evaluations" / f"{eval_id}.json", {"id": eval_id, "revision": rev, "raw": raw,
                      "text": (sess["summary"].get("text") or "")[:20000], "verdict": verdict, "error": err,
                      "session_id": sess["session_id"], "manifest_ids": [m["id"] for m in manifest]})
    if err:
        store.append_event("evaluation.rejected", {"id": eval_id, "error": err[:500]})
        with store.transaction() as s:
            s["counters"]["evaluations"] = s["counters"].get("evaluations", 0) + 1
            s["status"] = "RUNNING"
            s["loop"]["last_eval_input_hash"] = mh
        log(store, f"evaluator output REJECTED: {err}")
        return {"verdict": None, "delta": None, "skipped": False, "error": err, "revision": rev}
    with store.transaction() as s:
        delta = apply_transitions(s, verdict, evaluation_id=eval_id)
        s["counters"]["evaluations"] = s["counters"].get("evaluations", 0) + 1
        s["loop"]["last_eval_input_hash"] = mh
        s["last_verdict"] = verdict
        s["status"] = "RUNNING"
    store.append_event("evaluation.applied", {"id": eval_id, "overall": verdict["overall"], "delta": delta,
                                              "failure_signature": verdict["failure_signature"]})
    log(store, f"evaluation {eval_id}: {verdict['overall']} delta={delta}")
    return {"verdict": verdict, "delta": delta, "skipped": False, "error": None, "revision": rev, "eval_id": eval_id}


# ---------------------------------------------------------------------------- terminal
def finish(store: RunStore, status: str, reason: str, capsule: dict | None = None) -> None:
    with store.transaction() as s:
        s["status"] = status; s["terminal_reason"] = reason; s["ended_at"] = now_iso()
        if capsule:
            s["failure_capsule"] = capsule
    store.append_event("run.finished", {"status": status, "reason": reason})
    log(store, f"FINISHED {status}: {reason}")
    _append_history(store, status, reason, capsule)
    # Codex needs a short-lived credential-only home while a child is running,
    # not a permanent auth.json copy in every finished run directory.
    shutil.rmtree(store.tmp_dir / "codex-home", ignore_errors=True)


def _append_history(store: RunStore, status: str, reason: str, capsule: dict | None) -> None:
    """One line per finished run in the *project's* own `.longrun/history.jsonl`.

    Every run starts cold: state, loop counters and failure signatures all live inside one run's store and
    die with it, so nothing carries a lesson forward. The measured cost of that is not subtle — of ten
    outcomes that landed on one autonomous night, seven were repairs of damage earlier autonomous batches
    had left, and no mechanism anywhere noticed the pattern. This is deliberately data, not a judgement:
    no model is called, nothing is scored, and the planner (already a strategic model) is handed the last
    several entries so it can see the shape of recent work before choosing the next outcome."""
    try:
        st = store.read(verify=False)
        root = Path(st["project_root"])
        d = root / ".longrun"
        if not d.is_dir():
            return
        crit = st.get("criteria", {})
        row = {
            "run_id": store.run_id, "ended_at": st.get("ended_at"), "status": status,
            "outcome": (st.get("outcome_title") or (capsule or {}).get("outcome") or "")[:300],
            "reason": reason[:300],
            "criteria": {k: v.get("status") for k, v in crit.items()},
            "cost_usd": round(float(st.get("counters", {}).get("cost_usd", 0.0)), 2),
            "rounds": st.get("counters", {}).get("rounds"),
            "repairs": st.get("counters", {}).get("repairs"),
            "wall_minutes": round(float(st.get("counters", {}).get("wall_seconds", 0.0)) / 60.0),
            "failure_signatures": (st.get("loop", {}) or {}).get("failure_signatures", [])[-3:],
            "surviving_facts": [f[:200] for f in (capsule or {}).get("surviving_facts", [])[-4:]],
        }
        with (d / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:                  # history is a convenience, never a reason to fail a run
        log(store, f"history not written: {e}")



def _partial(store: RunStore, contract: dict) -> tuple[str, list[str], list[str]]:
    """A run that ran out of rounds is not automatically worthless. One night's run ended with four of five
    criteria PASS and a working traffic system, and the whole branch was discarded because the fifth was a
    composition judgement. Report what was verified so the next planner can finish it instead of redoing it."""
    crit = store.read(verify=False).get("criteria", {})
    passed = [k for k, v in crit.items() if v.get("status") == "PASS"]
    open_ = [x["id"] for x in contract["criteria"] if x["id"] not in passed]
    return ("PARTIAL_PASS" if passed and open_ else "RESET_RECOMMENDED"), passed, open_


def _finish_bounded(store: RunStore, contract: dict, reason: str, capsule: dict) -> str:
    """Terminal exit for a run that hit a bound (rounds, spend-without-delta, restart budget)."""
    status, passed, open_ = _partial(store, contract)
    if status == "PARTIAL_PASS":
        reason = (f"{reason}; {len(passed)} of {len(contract['criteria'])} criteria verified "
                  f"({', '.join(passed)}) — work kept on the run branch, still open: {', '.join(open_)}")
    finish(store, status, reason, capsule)
    return status


def _capsule(store: RunStore, contract: dict, attempts: list[dict], observations: list[str]) -> dict:
    st = store.read()
    ws = Path(st["workspace"] or st["project_root"])
    diff_stat = ""
    if st["start_revision"]:
        diff_stat = G.diff_text(ws, st["start_revision"], max_bytes=0)
    findings = []
    for cid, rec in st.get("criteria", {}).items():
        findings.append({"id": cid, "verdict": rec.get("status"), "reason": rec.get("last_reason", "")})
    hyps = [e["data"].get("note", "") for e in store.events() if e["kind"] == "observation.recorded" and str(e["data"].get("note", "")).startswith("HYPOTHESIS")]
    facts = [e["data"].get("blocker") for e in store.events() if e["kind"] == "observation.recorded" and e["data"].get("blocker")]
    return build_failure_capsule(st, contract, attempts=attempts, evaluator_findings=findings, diff_summary=diff_stat,
                                 surviving_facts=facts, rejected_hypotheses=hyps, observations=observations)


# ---------------------------------------------------------------------------- main loop
def run_loop(run_id: str, *, model: str | None = None, eval_model: str | None = None) -> str:
    store = RunStore(run_id)
    if not store.exists():
        raise ControllerError(f"no such run {run_id}")
    lease = _acquire_run_controller(store)
    runner = None
    try:
        st = store.read()
        if st["status"] in TERMINAL_STATES:
            raise ControllerError(f"run is terminal ({st['status']}); use `longrun reset` for a fresh child run")
        # reconcile children left by a crashed controller only after acquiring
        # the lease; a second resume must never kill the first controller's child.
        from .process import reconcile_children
        with store.transaction() as s:
            s["children"] = reconcile_children(s["children"])
            s["controller_pid"] = os.getpid()
        if (store.dir / "STOP").exists():
            (store.dir / "STOP").unlink()
        runner = ChildRunner(on_child_start=lambda pid, pgid: None, on_child_end=lambda pid, rc: None)
        runner.install_signal_handlers()
        try:
            return _run_loop(store, runner, model, eval_model)
        except Interrupted:
            finish(store, "INTERRUPTED", "controller received SIGINT/SIGTERM; child process group terminated")
            return "INTERRUPTED"
        except TerminalQuotaError as e:
            finish(store, "FAILED", f"terminal provider quota: {e}")
            return "FAILED"
        except NonRetryableProviderRequestError as e:
            finish(store, "FAILED", f"non-retryable provider request: {e}")
            return "FAILED"
        except StrategicModelStalledError as e:
            finish(store, "FAILED", str(e))
            return "FAILED"
        except TamperDetected as e:
            runner.kill_current()
            store.append_event("state.tamper_detected", {"error": str(e)[:300]})
            st = store.read(verify=False)
            st["status"] = "FAILED"; st["terminal_reason"] = "authoritative state was modified outside the controller"; st["ended_at"] = now_iso()
            store._write_state(st)
            log(store, "FAILED: state tamper detected")
            return "FAILED"
    finally:
        if runner is not None:
            runner.restore_signal_handlers()
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        lease.close()


def _run_loop(store: RunStore, runner: ChildRunner, model: str | None, eval_model: str | None) -> str:
    st = store.read()
    if st["status"] == "PLANNED":
        _prepare_host_services(store)
        freeze_run(store, runner)
        st = store.read()
    if st["status"] == "FROZEN":
        with store.transaction() as s:
            s["status"] = "RUNNING"
    contract = json.loads(store.contract_path().read_text())
    adapter = load_adapter(st["adapter"], contract.get("adapter_config"))
    ws = Path(st["workspace"] or st["project_root"])
    b = contract["budgets"]
    attempts: list[dict] = []
    observations: list[str] = []
    findings: list[dict] = []
    next_strategy: str | None = None
    capsule_for_builder = None
    changed_strategy_required = False
    is_repair = False
    while True:
        st = store.read()
        if (store.dir / "STOP").exists():
            finish(store, "STOPPED", "stop requested by owner"); return "STOPPED"
        remaining = (st["deadline_epoch"] or 0) - time.time()
        if remaining < 60:
            finish(store, "BUDGET_EXHAUSTED", "wall-time budget exhausted", _capsule(store, contract, attempts, observations)); return "BUDGET_EXHAUSTED"
        if b.get("max_cost_usd") and st["counters"].get("cost_usd", 0) >= float(b["max_cost_usd"]):
            finish(store, "BUDGET_EXHAUSTED", "cost budget exhausted", _capsule(store, contract, attempts, observations)); return "BUDGET_EXHAUSTED"
        if st["counters"]["rounds"] >= int(b["max_rounds"]):
            return _finish_bounded(store, contract, f"max_rounds ({b['max_rounds']}) reached without PASS", _capsule(store, contract, attempts, observations))
        # Rounds are a poor currency: one night's rounds cost $2.22 to $11.80. What actually distinguishes a run
        # worth continuing from one worth stopping is whether the money is still moving criteria, so bound the
        # spend since the last criterion delta rather than the spend overall.
        spent_since_delta = float(st["counters"].get("cost_usd", 0.0)) - float(st.get("cost_at_last_delta") or 0.0)
        if spent_since_delta > float(b.get("max_cost_without_delta_usd") or 15.0) and st.get("loop", {}).get("no_delta_checkpoints", 0) >= 1:
            return _finish_bounded(store, contract,
                   f"${spent_since_delta:.2f} spent since the last criterion moved; stopping instead of buying more rounds",
                   _capsule(store, contract, attempts, observations))
        round_no = st["counters"]["rounds"] + 1
        with store.transaction() as s:
            s["counters"]["rounds"] = round_no
            s["status"] = "REPAIRING" if is_repair else "RUNNING"
        store.append_event("round.start", {"round": round_no, "repair": is_repair, "changed_strategy_required": changed_strategy_required})
        log(store, f"round {round_no} start (repair={is_repair}, remaining {int(remaining)}s)")
        ev_before = len(list_evidence(store))
        round_started = time.time()
        prompt = builder_prompt(contract=contract, state=st, round_no=round_no, is_repair=is_repair, findings=findings,
                                capsule=capsule_for_builder, adapter_fragment=adapter.builder_prompt_fragment(),
                                workspace=ws, run_id=store.run_id, changed_strategy_required=changed_strategy_required,
                                next_strategy=next_strategy)
        capsule_for_builder = None
        try:
            sess = launch_session(store, runner, role="builder", prompt=prompt, model=model, on_actions=True)
        except (TerminalQuotaError, NonRetryableProviderRequestError, StrategicModelStalledError):
            raise
        except ControllerError as e:
            # An unrecoverable builder session (for example an exhausted swarm
            # manager budget) ends the outcome with the honest status here,
            # instead of falling through to gate/evaluator/repair as if the
            # round had produced reviewable work.
            finish(store, "FAILED", f"builder session failed terminally: {e}")
            return "FAILED"
        ev_after = list_evidence(store)
        new_ev = len(ev_after) - ev_before
        changed = G.changed_files(ws, st["start_revision"])
        current_content_revision = G.content_revision(ws)
        has_run_delta = _has_cumulative_run_delta(
            current_content_revision, (st.get("baseline") or {}).get("revision"))
        doc_only = bool(changed) and all(f.lower().endswith((".md", ".rst", ".txt")) for f in changed)
        # The "same file edited over and over with nothing to show for it" rule is gated on evidence, but the
        # argument was never passed from either call site, so it was always {} and the rule silently degenerated
        # to "12 edits to one file ⇒ stagnation" — aimed squarely at the one generator script most batches edit.
        op = analyze_stream(sess["actions"], {f: new_ev for f in changed})
        attempts.append({"round": round_no, "kind": "repair" if is_repair else "build",
                         "summary": (sess["summary"].get("text") or "")[:400],
                         "result": f"exit={sess['result'].exit_code} timed_out={sess['result'].timed_out} loop_kill={sess['loop_kill']['fired']} new_evidence={new_ev}"})
        if sess["loop_kill"]["fired"] or op.fired:
            store.append_event("loop.operational", {"round": round_no, "reasons": sess["loop_kill"]["reasons"] or op.reasons})
            log(store, f"operational loop detected: {sess['loop_kill']['reasons'] or op.reasons}")
        # ---- evaluate (skip the LLM evaluator when the builder submitted nothing: no criterion can PASS without evidence)
        # ---- cheap round gate: never pay an LLM evaluator for a workspace that cannot build
        _prepare_host_services(store)
        gate_failures = []
        for cmd in (contract.get("round_gate_commands") or adapter.round_gate_commands or []):
            r = _run_check(cmd, ws, int(b.get("child_timeout_seconds", 2700)))
            if not r["passed"]:
                gate_failures.append(f"`{cmd['cmd']}` -> exit {r.get('exit_code')}: {(r.get('stdout') or r.get('stderr') or '')[-600:]}")
        # The standing regression suite: blocking like the gate, but never a criterion. It answers "is what
        # already worked still working", which nine consecutive contracts each paid a criterion and an
        # evaluator judgement to re-ask, in nine slightly different wordings, one of them wrong.
        standing = []
        for cmd in adapter.standing_checks:
            r = _run_check(cmd, ws, int(b.get("child_timeout_seconds", 2700)))
            standing.append({"cmd": cmd["cmd"], "exit_code": r.get("exit_code"), "passed": r["passed"]})
            if not r["passed"]:
                gate_failures.append(f"REGRESSION `{cmd['cmd']}` -> exit {r.get('exit_code')}: "
                                     f"{(r.get('stdout') or r.get('stderr') or '')[-600:]}")
        if standing:
            store.append_event("standing_checks.run", {"round": round_no, "results": standing})
            with store.transaction() as s2:
                s2["standing_results"] = standing
        if gate_failures:
            store.append_event("round.gate_failed", {"round": round_no, "failures": [g[:300] for g in gate_failures]})
            log(store, f"round {round_no} gate failed; skipping evaluation: {gate_failures[0][:160]}")
            findings = [{"id": x["id"], "verdict": "INSUFFICIENT_EVIDENCE",
                         "reason": "the workspace does not pass the round gate; fix this first:\n" + "\n".join(gate_failures)}
                        for x in contract["criteria"]]
            next_strategy = None   # no evaluation happened this round; an older round's advice is not about this failure
            attempts.append({"round": round_no, "kind": "repair" if is_repair else "build",
                             "summary": "round gate failed", "result": gate_failures[0][:200]})
            with store.transaction() as s2:
                s2["counters"]["repairs"] = s2["counters"].get("repairs", 0) + 1
            if st["counters"]["repairs"] + 1 > int(b["max_repairs"]):
                return _finish_bounded(store, contract, "round gate kept failing (workspace does not build)",
                                       _capsule(store, contract, attempts, observations))
            is_repair = True
            changed_strategy_required = False
            continue

        # Harvest what the builder produced but never filed. Four sessions one night were killed — by the
        # timeout or the loop guard — after they had already run the full verification: the captures sat on
        # disk, the ledger was empty, and the round was scored worthless and bought a repair. The controller
        # can hash these artifacts itself, so their survival should not depend on the session's.
        known_hashes = {a["sha256"] for e in ev_after for a in (e.get("artifacts") or [])}
        for kw in adapter.post_round(ws, round_started, contract, known_hashes):
            try:
                rec = record_evidence(store, revision=G.content_revision(ws), submitted_by="controller:harvest", **kw)
                store.append_event("evidence.harvested", {"round": round_no, "id": rec["id"], "kind": rec["kind"]})
                log(store, f"harvested unclaimed {rec['kind']} evidence {rec['id']} left by round {round_no}")
            except EvidenceError as e:
                log(store, f"harvest rejected: {e}")
        ev_after = list_evidence(store)

        # A useful code/config round may leave a real workspace delta yet omit
        # manual ledger ceremony. In that case run controller-owned checks and
        # let the evaluator inspect the diff instead of buying a blind repair.
        # Evidence copied from an unchanged frozen workspace may be an old
        # baseline artifact resubmitted under a new id/criterion. Require a
        # cumulative run delta, while still allowing a later repair round to
        # submit evidence for bytes legitimately created by an earlier round.
        if not has_run_delta:
            store.append_event("evaluation.skipped_no_evidence", {"round": round_no, "session_id": sess["session_id"]})
            log(store, "evaluation skipped: run has no content delta from the frozen baseline")
            ev = {"verdict": {"overall": "NEEDS_REWORK", "criteria": [{"id": x["id"], "verdict": "INSUFFICIENT_EVIDENCE", "evidence_ids": [],
                                                                        "reason": "the run has no content delta from its frozen baseline"} for x in contract["criteria"]],
                              "failure_signature": "no workspace delta", "recommended_next_strategy": "produce the contracted artifact in the workspace, then submit its evidence",
                              "evaluated_revision": G.content_revision(ws), "run_id": store.run_id, "contract_hash": st["contract_hash"]},
                  "delta": {"passed": [], "failed": [], "insufficient": [x["id"] for x in contract["criteria"]], "owner": [], "regressed": []},
                  "skipped": True, "error": None, "synthetic": True}
            with store.transaction() as s:
                s["loop"]["failure_signatures"] = (s["loop"].get("failure_signatures") or [])[-5:]
        else:
            ev = evaluate(store, runner, model=eval_model)
        st = store.read()
        verdict, delta = ev.get("verdict"), ev.get("delta")
        if delta and (delta.get("passed") or delta.get("failed") or delta.get("regressed")):
            with store.transaction() as s2:
                s2["cost_at_last_delta"] = float(s2["counters"].get("cost_usd", 0.0))
        if verdict:
            findings = [c for c in verdict["criteria"] if c["verdict"] != "PASS"]
            # The evaluator is asked for one paragraph on what to do differently, the schema requires it and the
            # validator checks it — and until now it was stored and dropped. The next builder round was handed the
            # per-criterion reasons and had to re-derive the strategy the harness had already paid a strategic-tier
            # model to write. This is the one part of a failed round that the measured literature says carries the
            # gain (diagnosis on top of a hard external signal), so it is forwarded verbatim rather than re-invented.
            next_strategy = str(verdict.get("recommended_next_strategy") or "").strip() or None
            if verdict["overall"] == "PASS":
                finish(store, "PASSED", "all criteria PASS by independent evaluation"); return "PASSED"
            if verdict["overall"] == "OWNER_JUDGMENT_REQUIRED" or any(c["verdict"] == "OWNER_JUDGMENT_REQUIRED" for c in verdict["criteria"]):
                finish(store, "OWNER_JUDGMENT_REQUIRED", "evaluator: irreducible owner judgment", _capsule(store, contract, attempts, observations)); return "OWNER_JUDGMENT_REQUIRED"
        blockers = [e for e in store.events() if e["kind"] == "observation.recorded" and e["data"].get("blocker") and e["data"].get("round") == round_no]
        rs = {"new_evidence": new_ev, "edited_files": changed, "doc_only": doc_only, "plan_rewrites": 0,
              "spend_usd": sess["summary"].get("cost_usd") or 0.0, "actions": len(sess["actions"]),
              "blocker_demonstrated": bool(blockers), "is_repair": is_repair,
              "changed_hypothesis": changed_strategy_required and any(str(e["data"].get("note", "")).startswith("HYPOTHESIS") for e in store.events()
                                                                       if e["kind"] == "observation.recorded" and e["data"].get("round") == round_no)}
        with store.transaction() as s:
            strat = strategic_check(s, verdict=verdict, delta=delta, round_summary=rs)
        stagnant = strat.fired or sess["loop_kill"]["fired"] or op.fired or (ev.get("error") is not None and new_ev == 0)
        if strat.fired:
            store.append_event("loop.strategic", {"round": round_no, "reasons": strat.reasons})
            log(store, f"strategic stagnation: {strat.reasons}")
        st = store.read()
        needs_rework = verdict is not None and verdict["overall"] in ("NEEDS_REWORK", "RESET_RECOMMENDED")
        # ---- policy
        if verdict and verdict["overall"] == "RESET_RECOMMENDED" and st["counters"]["fresh_restarts"] >= int(b["max_fresh_restarts"]):
            return _finish_bounded(store, contract, "evaluator recommends reset and restart budget is spent", _capsule(store, contract, attempts, observations))
        if stagnant or needs_rework or verdict is None:
            if st["counters"]["repairs"] < int(b["max_repairs"]):
                with store.transaction() as s:
                    s["counters"]["repairs"] += 1
                is_repair = True
                changed_strategy_required = bool(stagnant)
                store.append_event("repair.scheduled", {"n": st["counters"]["repairs"] + 1, "changed_strategy": changed_strategy_required})
                continue
            if st["counters"]["fresh_restarts"] < int(b["max_fresh_restarts"]):
                cap = _capsule(store, contract, attempts, observations)
                # Strategic role: the evaluator's model, or the strategic tier's own default — never the
                # builder's. `or model` fell through to the builder model whenever --eval-model was absent,
                # so `longrun go --model sonnet` (a deliberate cheap-builder experiment) silently demoted the
                # restart manager to sonnet as well, and the one judgement that decides whether to keep a
                # failing run's work was made by the tier the experiment was testing.
                decision = fresh_restart(store, runner, contract, cap, model=eval_model)
                capsule_for_builder = cap
                findings = []
                is_repair = False
                changed_strategy_required = True
                with store.transaction() as s:
                    s["counters"]["repairs"] = 0
                    s["loop"]["failure_signatures"] = []
                    s["loop"]["no_delta_checkpoints"] = 0
                    s["loop"]["repairs_without_move"] = 0
                observations.append(f"fresh restart decision: {decision}")
                continue
            reason = "stagnation persists after repairs and one fresh restart" if stagnant else "criteria still failing after repair/restart budget"
            if any(e["kind"] == "observation.recorded" and e["data"].get("blocker") for e in store.events()):
                finish(store, "BLOCKED", "demonstrated blocker on ledger; " + reason, _capsule(store, contract, attempts, observations)); return "BLOCKED"
            return _finish_bounded(store, contract, reason, _capsule(store, contract, attempts, observations))
        # progress without full pass: continue normal rounds
        is_repair = False
        changed_strategy_required = False


def fresh_restart(store: RunStore, runner: ChildRunner, contract: dict, capsule: dict, model: str | None) -> str:
    st = store.read()
    ws = Path(st["workspace"] or st["project_root"])
    with store.transaction() as s:
        s["counters"]["fresh_restarts"] += 1
        s["status"] = "RESTARTING"
    restart_id = f"R{uuid.uuid4().hex[:8]}"
    # An in-place run may have started from owner-authored dirty work. Resetting to HEAD would erase that
    # baseline, and even capturing it as a binary patch can be multi-gigabyte. Keep the current work and let
    # the next builder session change strategy without a destructive restart.
    dirty_in_place_baseline = (st.get("isolation") == "none"
                               and not str(st.get("start_content_revision") or "").endswith("+clean"))
    if dirty_in_place_baseline:
        store.append_event("restart.decision", {"id": restart_id, "decision": "APPLY",
                                                "rationale": "dirty in-place baseline is owner work; restart kept it",
                                                "skipped_session": True})
        with store.transaction() as s:
            s["status"] = "RUNNING"
        log(store, "restart: preserving dirty in-place baseline; continuing with a new builder session")
        return "APPLY"
    patch = store.unique_artifact_path(f"interrupted-diff-{restart_id}", ".patch")
    patch_saved = G.save_patch(ws, st["start_revision"], patch) if st["start_revision"] else False
    stat = G.diff_text(ws, st["start_revision"], max_bytes=0) if st["start_revision"] else ""
    rc = 0 if st["start_revision"] else 1
    store.append_event("restart.start", {"id": restart_id, "patch": str(patch), "capsule": capsule})
    prompt = restart_manager_prompt(contract=contract, capsule=capsule, diff_stat=stat if rc == 0 else "", workspace=ws)
    decision = "APPLY"
    if st["start_revision"] and not patch_saved:
        store.append_event("restart.decision", {"id": restart_id, "decision": decision,
                                                "rationale": "recovery patch could not be saved; destructive restart disabled",
                                                "skipped_session": True})
        with store.transaction() as s:
            s["status"] = "RUNNING"
        log(store, "restart: recovery patch unavailable; preserving work and continuing")
        return decision
    # Do not buy a judgement when there is nothing to judge. This role chooses between keeping the
    # interrupted work, keeping part of it and throwing it away — and with an empty diff the only possible
    # answer is APPLY. Measured across every run on this machine it has decided APPLY 5 times out of 5,
    # so the cheap half of that record is at least removed here; when there IS a diff the question is real
    # and still gets asked.
    if rc != 0 or not stat.strip():
        store.append_event("restart.decision", {"id": restart_id, "decision": decision,
                                                "rationale": "no interrupted diff to keep or discard; not judged",
                                                "skipped_session": True})
        with store.transaction() as s:
            s["status"] = "RUNNING"
        log(store, "restart: nothing to decide (empty diff); continuing without a restart-manager session")
        return decision
    try:
        sess = launch_session(store, runner, role="restart_manager", prompt=prompt, json_schema=RESTART_DECISION_SCHEMA, max_turns=25, model=model)
        so = sess["summary"].get("structured_output")
        if not isinstance(so, dict):
            so = extract_json_object(sess["summary"].get("text") or "")
        if so.get("decision") in ("APPLY", "PARTIALLY_APPLY", "DISCARD"):
            decision = so["decision"]
        rationale = str(so.get("rationale", ""))[:1000]
        if decision == "DISCARD" and st["start_revision"]:
            G.hard_reset(ws, st["start_revision"])
        elif decision == "PARTIALLY_APPLY" and st["start_revision"]:
            for p in so.get("discard_paths") or []:
                G._git(ws, "checkout", st["start_revision"], "--", p)
        store.append_event("restart.decision", {"id": restart_id, "decision": decision, "rationale": rationale,
                                                "new_hypothesis": str(so.get("new_hypothesis", ""))[:1000]})
    except (TerminalQuotaError, StrategicModelStalledError, NonRetryableProviderRequestError):
        raise
    except Exception as e:
        store.append_event("restart.decision", {"id": restart_id, "decision": decision, "error": str(e)[:300]})
    with store.transaction() as s:
        s["status"] = "RUNNING"
    log(store, f"fresh restart {restart_id}: {decision}")
    return decision


# ---------------------------------------------------------------------------- auto-plan (goal -> contract)
def review_manual_contract(store: RunStore, goal: str, spec: dict, *, model: str | None = None) -> dict:
    """Apply the same independent intent boundary to a hand-authored contract."""
    from .planner import (INTENT_REVIEW_SCHEMA, OwnerConfirmationRequired, intent_review_prompt,
                          validate_intent_review, validate_planner_spec)
    if not isinstance(goal, str) or len(goal.strip()) < 10:
        raise ControllerError("manual contract requires a non-empty `goal` copied from the owner request")
    validate_planner_spec(spec, goal)
    st = store.read()
    if st["status"] != "CREATED":
        raise ControllerError(f"manual intent review requires a CREATED run, got {st['status']}")
    root = Path(st["project_root"])
    with store.transaction() as s:
        s["deadline_epoch"] = time.time() + 1800
        s["goal"] = goal
    runner = ChildRunner(); runner.install_signal_handlers()
    try:
        session = launch_session(
            store, runner, role="intent_reviewer",
            prompt=intent_review_prompt(goal=goal, spec=spec, project_root=root,
                                        chain_context=st.get("chain_context") or {}),
            json_schema=INTENT_REVIEW_SCHEMA, max_turns=18, model=model)
        review = session["summary"].get("structured_output")
        if not isinstance(review, dict):
            review = extract_json_object(session["summary"].get("text") or "")
        try:
            validate_intent_review(review, goal)
        except OwnerConfirmationRequired as e:
            raise ControllerError(str(e)) from e
        store.append_event("plan.intent_review.accepted", {
            "attempt": 1, "manual": True, "summary": str(review.get("summary") or "")[:500]})
        return set_contract(store, spec)
    finally:
        runner.restore_signal_handlers()


def auto_plan(store: RunStore, goal: str, *, model: str | None = None) -> dict:
    """Fresh strategic-tier planner turns a one-sentence goal into a validated contract. One retry with the validator error."""
    from .planner import (CONTRACT_SPEC_SCHEMA, INTENT_REVIEW_SCHEMA, OwnerConfirmationRequired,
                          planner_prompt, contract_repair_prompt, intent_review_prompt, default_hints, parse_spec,
                          validate_planner_spec, validate_intent_review)
    from .contract import ContractError
    st = store.read()
    if st["status"] not in ("CREATED",):
        raise ControllerError(f"auto_plan requires a CREATED run, got {st['status']}")
    root = Path(st["project_root"])
    adapter = load_adapter(st["adapter"], None)
    with store.transaction() as s:
        s["deadline_epoch"] = time.time() + 1800   # planning budget
        s["goal"] = goal
    runner = ChildRunner(); runner.install_signal_handlers()
    err = None
    candidate_spec = None
    repair_review = None
    try:
        # The independent review may expose a different material omission after the planner fixes the first
        # one. Three bounded drafts allow that correction while still preventing an open-ended planning loop.
        for attempt in (1, 2, 3):
            chain_context = st.get("chain_context") or {}
            repairing = candidate_spec is not None and repair_review is not None
            prompt = (contract_repair_prompt(goal=goal, spec=candidate_spec, review=repair_review)
                      if repairing else
                      planner_prompt(goal=goal, project_root=root, adapter_name=st["adapter"],
                                     adapter_fragment=adapter.builder_prompt_fragment(), prior_error=err,
                                     project_hints=default_hints(root), run_id=store.run_id))
            store.append_event("plan.auto.start", {"attempt": attempt, "goal": goal[:500]})
            sess = launch_session(store, runner, role="contract_repair" if repairing else "planner", prompt=prompt,
                                  json_schema=CONTRACT_SPEC_SCHEMA, max_turns=12 if repairing else 40, model=model)
            try:
                spec = parse_spec(sess["summary"])
                validate_planner_spec(spec, goal)
                candidate_spec = spec
                # A fresh strategic session checks the planner's actual pass conditions against the verbatim
                # owner request. This catches material dilution before freeze without adding a review loop.
                review_sess = launch_session(
                    store, runner, role="intent_reviewer",
                    prompt=intent_review_prompt(goal=goal, spec=spec, project_root=root,
                                                chain_context=chain_context),
                    json_schema=INTENT_REVIEW_SCHEMA, max_turns=18, model=model)
                review = review_sess["summary"].get("structured_output")
                if not isinstance(review, dict):
                    review = extract_json_object(review_sess["summary"].get("text") or "")
                repair_review = review if review.get("verdict") == "REJECT" else None
                review_result = validate_intent_review(review, goal)
                if review_result == "OWNER_OVERRIDE_APPLIED":
                    store.append_event("plan.intent_review.owner_override_applied", {
                        "attempt": attempt,
                        "objection_key": str((review.get("owner_objection") or {}).get("objection_key") or "")[:64],
                    })
                store.append_event("plan.intent_review.accepted", {
                    "attempt": attempt, "summary": str(review.get("summary") or "")[:500]})
                rationale = spec.get("rationale", "")
                spec = {k: v for k, v in spec.items() if k != "rationale"}
                # The project's own adapter settings (capture dir, views, the real build command for the round
                # gate) belong to the repo, not to whatever the planner happened to write this time.
                proj_cfg = root / ".longrun" / "config.json"
                if proj_cfg.is_file():
                    try:
                        ac = (json.loads(proj_cfg.read_text()) or {}).get("adapter_config")
                        if isinstance(ac, dict):
                            spec["adapter_config"] = {**ac, **(spec.get("adapter_config") or {})}
                    except (OSError, json.JSONDecodeError):
                        pass
                c = set_contract(store, spec)
                store.append_event("plan.auto.accepted", {"attempt": attempt, "rationale": str(rationale)[:600], "criteria": [x["id"] for x in c["criteria"]]})
                (store.dir / "contract.spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False))
                return c
            except (TerminalQuotaError, StrategicModelStalledError, NonRetryableProviderRequestError):
                raise
            except OwnerConfirmationRequired as e:
                objection = e.objection
                atomic_write_json(store.dir / "owner-confirmation-required.json", objection)
                source_lines = [f"{x.get('title')}: {x.get('locator')}" for x in (objection.get("sources") or [])[:5]]
                reason = (f"{objection.get('question')} Objection key: {objection.get('objection_key')}. "
                          + " Sources: " + "; ".join(source_lines))[:2000]
                store.append_event("plan.intent_review.owner_confirmation_required", {
                    "attempt": attempt, "objection": objection})
                finish(store, "OWNER_JUDGMENT_REQUIRED", reason)
                raise ControllerError(reason)
            except (ContractError, ControllerError, ValueError, KeyError) as e:
                err = str(e)[:400]
                if err.startswith("independent owner-intent review rejected"):
                    store.append_event("plan.intent_review.rejected", {"attempt": attempt, "error": err})
                else:
                    # Local/schema failures need a fresh planner. Only a concrete independent
                    # REJECT is safe to repair narrowly from the previous complete candidate.
                    candidate_spec = None
                    repair_review = None
                store.append_event("plan.auto.rejected", {"attempt": attempt, "error": err})
                log(store, f"auto-plan attempt {attempt} rejected: {err}")
    finally:
        runner.restore_signal_handlers()
    finish(store, "FAILED", f"auto-plan failed: {err}")
    raise AutoPlanFailedError(f"auto-plan failed after three bounded attempts: {err}")
