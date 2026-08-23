"""longrun CLI. No command invocation means no harness behaviour."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import shutil
import sys
import time
import subprocess
from pathlib import Path

from . import __version__
from .paths import (data_root, runs_root, keys_root, ensure_dirs, backups_root, archive_root,
                    chain_stop_marker, chain_lock_path)
from .store import RunStore, find_active_runs, TERMINAL_STATES, now_iso, atomic_write_json
from .token import ENV_TOKEN, parse_unverified, verify
from . import gitutil as G


def _die(msg: str, code: int = 2) -> "None":
    sys.stderr.write(f"longrun: {msg}\n")
    sys.exit(code)


def _resolve_run(arg: str | None, project: Path | None = None) -> RunStore:
    if arg:
        st = RunStore(arg)
        if st.exists():
            return st
        # prefix match
        for d in runs_root().iterdir() if runs_root().is_dir() else []:
            if d.name.startswith(arg) and (d / "state.json").is_file():
                return RunStore(d.name)
        _die(f"no run {arg}")
    rid = os.environ.get("LONGRUN_RUN_ID")
    if rid and RunStore(rid).exists():
        return RunStore(rid)
    proj = (project or Path.cwd()).resolve()
    from .paths import project_marker
    for cand in [proj, *proj.parents]:
        m = project_marker(cand)
        if m.is_file():
            try:
                rid = json.loads(m.read_text()).get("last_run_id")
                if rid and RunStore(rid).exists():
                    return RunStore(rid)
            except Exception:
                pass
    _die("no run id given and none discoverable (pass --run <id>)")


def _session_auth(required_roles: set[str]) -> tuple[RunStore, dict]:
    """For builder-facing commands: require a controller-issued token of an allowed role."""
    tok = os.environ.get(ENV_TOKEN)
    if not tok:
        _die("this command needs a controller-issued session token (LONGRUN_TOKEN); it is only available inside a `longrun run` session", 3)
    p = parse_unverified(tok)
    if not p:
        _die("malformed session token", 3)
    st = RunStore(p["run_id"])
    if not st.exists():
        _die("token references an unknown run", 3)
    v = verify(tok, st.secret() or "")
    if not v:
        _die("invalid or expired session token", 3)
    if v["role"] not in required_roles:
        _die(f"role {v['role']} may not run this command", 3)
    state = st.read()
    if not any(c.get("session_id") == v["session_id"] and c.get("ended_at") is None for c in state.get("children", [])):
        _die("session is not a live child of this run", 3)
    return st, v


# ------------------------------------------------------------------------------------ commands
def cmd_doctor(a) -> int:
    ensure_dirs()
    checks = []
    checks.append(("data root", str(data_root()), True))
    checks.append(("claude", shutil.which("claude") or "MISSING", bool(shutil.which("claude"))))
    checks.append(("codex", shutil.which("codex") or "MISSING", bool(shutil.which("codex"))))
    checks.append(("opencode", shutil.which("opencode") or "MISSING", bool(shutil.which("opencode"))))
    checks.append(("git", shutil.which("git") or "MISSING", bool(shutil.which("git"))))
    # global hook audit: any user-level Claude hook referencing longrun or the old autopilot is a violation of explicit opt-in
    settings = Path.home() / ".claude" / "settings.json"
    bad = []
    if settings.is_file():
        try:
            s = json.loads(settings.read_text())
            for ev, groups in (s.get("hooks") or {}).items():
                for g in groups:
                    for h in g.get("hooks", []):
                        c = h.get("command", "")
                        if "longrun" in c or "autopilot" in c:
                            bad.append(f"{ev}: {c}")
        except Exception as e:
            bad.append(f"unreadable settings.json: {e}")
    checks.append(("no global longrun/autopilot hooks in ~/.claude/settings.json", "; ".join(bad) or "ok", not bad))
    codex_hooks = Path.home() / ".codex" / "hooks.json"
    cbad = []
    if codex_hooks.is_file() and "longrun" in codex_hooks.read_text():
        cbad.append("longrun referenced in ~/.codex/hooks.json")
    checks.append(("no global longrun hooks in ~/.codex/hooks.json", "; ".join(cbad) or "ok", not cbad))
    checks.append(("keys dir mode 0700", oct(keys_root().stat().st_mode & 0o777), (keys_root().stat().st_mode & 0o777) == 0o700))
    active = find_active_runs()
    checks.append(("active runs", ", ".join(x["run_id"][:8] for x in active) or "none", True))
    tok = os.environ.get(ENV_TOKEN)
    checks.append(("this shell is inside a run session", "yes" if tok else "no", True))
    cwd = Path.cwd()
    if G.is_git_repo(cwd):
        stranded = _branches_requiring_landing(cwd)
        checks.append(("no accepted run branch awaits landing",
                       ", ".join(f"{b} (+{n})" for b, n in stranded) or "ok", not stranded))
    ok = True
    for name, val, good in checks:
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {name}: {val}")
    print(f"longrun {__version__} — inactive by default; only `longrun run` launches sessions.")
    return 0 if ok else 1


def cmd_init(a) -> int:
    root = Path(a.project).resolve()
    d = root / ".longrun"
    d.mkdir(exist_ok=True)
    cfg = d / "config.json"
    if not cfg.exists() or a.force:
        atomic_write_json(cfg, {"adapter": a.adapter, "driver": a.driver, "note": "project defaults for longrun; presence activates nothing"})
    tmpl = d / "contract.template.json"
    from .contract import DEFAULT_BUDGETS
    atomic_write_json(tmpl, {
        "goal": "<the owner request this contract must satisfy>",
        "observable_end_state": "<what a stranger can observe when this outcome is done>",
        "batch": {"boundary": "<one production-stage boundary>", "reality_test": "<one check of that boundary>",
                  "estimated_seconds": 1800, "max_foreground_seconds": 900, "deferred_required_outcomes": []},
        "criteria": [{"id": "C1", "statement": "<externally checkable statement>", "kind": "functional",
                      "evidence_requirements": ["check"], "deterministic_checks": [{"cmd": "<command that exits 0 when true>"}],
                      "evaluator_policy": "llm_required"}],
        "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
        "budgets": DEFAULT_BUDGETS, "adapter_config": {}})
    gi = root / ".gitignore"
    print(f"initialized {d} (adapter={a.adapter}, driver={a.driver}); template: {tmpl}")
    print("Nothing is active. Next: write a contract JSON, then `longrun plan --project . --contract <file>`.")
    return 0


def cmd_plan(a) -> int:
    from .controller import create_run, review_manual_contract, ControllerError
    root = Path(a.project).resolve()
    cfg = root / ".longrun" / "config.json"
    defaults = json.loads(cfg.read_text()) if cfg.is_file() else {}
    adapter = a.adapter or defaults.get("adapter", "software")
    driver = a.driver or defaults.get("driver", "claude")
    spec = json.loads(Path(a.contract).read_text())
    goal = spec.pop("goal", None)
    from .contract import ContractError
    try:
        st = create_run(root, adapter, driver, budgets=spec.get("budgets"), isolation=a.isolation, allow_dirty=a.allow_dirty)
    except ControllerError as e:
        _die(str(e))
    try:
        c = review_manual_contract(st, goal, spec)
    except (ControllerError, ContractError) as e:
        with st.transaction() as s:
            s["status"] = "FAILED"; s["terminal_reason"] = f"contract rejected: {e}"[:500]
        st.append_event("run.finished", {"status": "FAILED", "reason": "contract rejected"})
        _die(f"contract rejected: {e}")
    from .contract import contract_summary, contract_hash
    print(f"run {st.run_id} PLANNED (not frozen, nothing executed)")
    print(contract_summary(c))
    print(f"prelim hash {contract_hash(c)[:12]}; state dir {st.dir}")
    print(f"Next: `longrun run --run {st.run_id[:8]}` (freezes baseline first, then executes).")
    return 0


def cmd_run(a) -> int:
    from .controller import run_loop, ControllerError
    st = _resolve_run(a.run, Path(a.project) if a.project else None)
    from .config import load as _cfg
    if not a.permission_mode:
        a.permission_mode = _cfg().get("default_permission_mode", "acceptEdits")
        a.allow_bypass = True   # owner default from ~/.local/share/longrun/config.json
    if a.permission_mode:
        if a.permission_mode == "bypassPermissions" and not a.allow_bypass:
            _die("bypassPermissions is not the default builder mode on the host. For maximum autonomy prefer "
                 "`--permission-mode auto`; if you really want a blanket bypass, add --allow-bypass (owner accepts the risk; recorded in the run log)")
        with st.transaction() as s:
            s["permission_mode"] = a.permission_mode
            s["owner_accepted_bypass"] = bool(a.allow_bypass and a.permission_mode == "bypassPermissions")
        if a.permission_mode == "bypassPermissions":
            st.append_event("run.owner_accepted_bypass", {"permission_mode": "bypassPermissions"})
    try:
        status = run_loop(st.run_id, model=a.model, eval_model=a.eval_model)
    except ControllerError as e:
        _die(str(e))
    print(f"run {st.run_id[:8]} -> {status}")
    return 0 if status == "PASSED" else 1


def cmd_status(a) -> int:
    st = _resolve_run(a.run, Path(a.project) if a.project else None)
    s = st.read()
    if a.json:
        print(json.dumps(s, indent=2)); return 0
    print(f"run {s['run_id']}  status={s['status']}  adapter={s['adapter']} driver={s['driver']}")
    print(f"project {s['project_root']}  workspace {s.get('workspace')}\nstart_rev {s.get('start_revision')}  contract {str(s.get('contract_hash'))[:12]} v{s.get('contract_version')}")
    c = s["counters"]
    print(f"rounds {c['rounds']}  repairs {c['repairs']}  restarts {c['fresh_restarts']}  evaluations {c['evaluations']}  "
          f"cost ${c.get('cost_usd', 0):.2f}  wall {c.get('wall_seconds', 0):.0f}s")
    if s.get("deadline_epoch"):
        print(f"deadline in {int(s['deadline_epoch'] - time.time())}s")
    for cid, rec in s.get("criteria", {}).items():
        print(f"  {cid:12s} {rec.get('status'):24s} {str(rec.get('last_reason', ''))[:100]}")
    if s.get("terminal_reason"):
        print(f"terminal: {s['terminal_reason']}")
    return 0


def cmd_evaluate(a) -> int:
    from .controller import evaluate, ControllerError
    from .process import ChildRunner
    st = _resolve_run(a.run, Path(a.project) if a.project else None)
    tok = os.environ.get(ENV_TOKEN)
    if tok:
        p = parse_unverified(tok) or {}
        if p.get("role") in ("builder", "evaluator"):
            _die("evaluate is a controller command; builder/evaluator sessions cannot invoke it", 3)
    runner = ChildRunner(); runner.install_signal_handlers()
    try:
        r = evaluate(st, runner, model=a.model, force=a.force)
    except ControllerError as e:
        _die(str(e))
    print(json.dumps({k: v for k, v in r.items() if k != "verdict"} | {"verdict": r.get("verdict")}, indent=2))
    return 0


def cmd_checkpoint(a) -> int:
    return cmd_evaluate(a)


def cmd_stop(a) -> int:
    st = _resolve_run(a.run, Path(a.project) if a.project else None)
    (st.dir / "STOP").write_text(now_iso())
    # A chain can finish an outcome between the owner's stop command and this
    # check.  Keep a project-scoped latch as well, so `go --chain 0` cannot
    # quietly create its next run during that race.
    latch = chain_stop_marker(Path(st.read(verify=False)["project_root"]))
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(now_iso())
    s = st.read()
    if s.get("controller_pid") and _pid_alive(s["controller_pid"]):
        print(f"stop requested; controller {s['controller_pid']} will terminate the current child and finish as STOPPED")
        return 0
    if s["status"] not in TERMINAL_STATES:
        with st.transaction() as x:
            x["status"] = "STOPPED"; x["terminal_reason"] = "stopped by owner (no live controller)"; x["ended_at"] = now_iso()
        st.append_event("run.finished", {"status": "STOPPED", "reason": "stopped by owner"})
    print(f"run {st.run_id[:8]} STOPPED")
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0); return True
    except Exception:
        return False



def cmd_prune(a) -> int:
    """Reclaim the workspace of finished runs. A run's worktree carries the project's whole build cache
    (2.3 GB of Unity Library in one measured project); ten outcomes in a night is 23 GB that nothing ever
    reclaimed. Only runs whose branch holds no unmerged work are touched, so nothing unlanded is lost."""
    import shutil
    from . import gitutil as G
    root = Path(a.project).resolve() if a.project else Path.cwd()
    freed = 0
    kept: list[str] = []
    stranded = {b for b, _ in (G.unmerged_run_branches(root) if G.is_git_repo(root) else [])}
    for d in sorted(runs_root().iterdir()):
        if not (d / "state.json").is_file():
            continue
        st = json.loads((d / "state.json").read_text())
        try:
            run_root = Path(st["project_root"]).resolve()
        except (KeyError, OSError):
            continue
        if run_root != root:
            continue
        ws = d / "workspace"
        if st.get("status") not in TERMINAL_STATES or not ws.is_dir():
            continue
        branch = f"longrun/{st['run_id'][:8]}"
        if branch in stranded:
            kept.append(f"{st['run_id'][:8]} ({branch} still holds unmerged work)")
            continue
        size = sum(f.stat().st_size for f in ws.rglob("*") if f.is_file())
        if a.dry_run:
            print(f"would free {size / 1e9:.2f} GB  {st['run_id'][:8]} [{st['status']}]")
        else:
            if G.is_git_repo(root):
                G.remove_worktree(root, ws)
            shutil.rmtree(ws, ignore_errors=True)
            print(f"freed {size / 1e9:.2f} GB  {st['run_id'][:8]} [{st['status']}]")
        freed += size
    if G.is_git_repo(root):
        subprocess.run(["git", "worktree", "prune"], cwd=str(root), capture_output=True)
    for k in kept:
        print(f"kept   {k}")
    print(f"{'would free' if a.dry_run else 'freed'} {freed / 1e9:.2f} GB total"
          + (f"; {len(kept)} run(s) kept because their work is not merged" if kept else ""))
    return 0


def cmd_reset(a) -> int:
    """Create a fresh child run (new UUID, clean counters) from a terminal run, carrying only the contract spec
    and the failure capsule as routed knowledge — never history, baseline, or counters."""
    from .controller import create_run, set_contract, ControllerError
    old = _resolve_run(a.run, Path(a.project) if a.project else None)
    s = old.read()
    if s["status"] not in TERMINAL_STATES:
        _die("reset requires a terminal run; use `longrun stop` first")
    c = json.loads(old.contract_path().read_text())
    spec = {k: c[k] for k in ("observable_end_state", "batch", "criteria", "constraints", "non_goals", "allowed_replace_remove",
                              "proven_blockers", "budgets", "owner_judgment_policy", "outcome_id", "allowed_commands", "adapter_config")}
    spec["criteria"] = [{k: v for k, v in x.items() if k != "initial_status"} for x in spec["criteria"]]
    try:
        new = create_run(Path(s["project_root"]), s["adapter"], s["driver"], budgets=spec["budgets"],
                         isolation=s.get("isolation", "worktree"), allow_dirty=True, parent_run_id=old.run_id)
        set_contract(new, spec)
    except ControllerError as e:
        _die(str(e))
    if s.get("failure_capsule"):
        atomic_write_json(new.dir / "parent_failure_capsule.json", s["failure_capsule"])
    new.append_event("run.reset_from", {"parent_run_id": old.run_id, "parent_status": s["status"]})
    print(f"new run {new.run_id} PLANNED from {old.run_id[:8]} (fresh counters/baseline). `longrun run --run {new.run_id[:8]}`")
    return 0


def cmd_evidence(a) -> int:
    from .evidence import record_evidence, EvidenceError, list_evidence
    from . import gitutil as G
    if a.evidence_cmd == "list":
        st = _resolve_run(a.run, None)
        for e in list_evidence(st):
            print(f"{e['id']} {e['kind']:16s} {','.join(e['criterion_ids']):20s} rev={str(e['revision'])[:14]} by={e['submitted_by'][:24]} {e['summary'][:70]}")
        return 0
    st, tok = _session_auth({"builder", "controller"})
    s = st.read()
    ws = Path(s["workspace"] or s["project_root"])
    rev = G.content_revision(ws)
    try:
        rec = record_evidence(st, kind=a.kind, criterion_ids=a.criterion, summary=a.summary, revision=a.revision or rev,
                              submitted_by=tok["session_id"], command=a.cmd, exit_code=a.exit, artifacts=a.artifact,
                              stdout=(sys.stdin.read() if a.stdin else None), current_revision=rev,
                              expected_run_id=a.run_id, expected_contract_hash=a.contract_hash)
    except EvidenceError as e:
        _die(f"evidence rejected: {e}", 4)
    st.append_event("evidence.submitted", {"id": rec["id"], "session_id": tok["session_id"], "round": s["counters"]["rounds"]}, locked=True)
    print(f"evidence {rec['id']} recorded (candidate; not a verdict) for {a.criterion} at {rev[:14]}")
    return 0


def cmd_observe(a) -> int:
    st, tok = _session_auth({"builder", "controller", "restart_manager"})
    s = st.read()
    data = {"session_id": tok["session_id"], "round": s["counters"]["rounds"]}
    if a.blocker:
        data["blocker"] = a.blocker[:2000]
    if a.note:
        data["note"] = a.note[:2000]
    if not (a.blocker or a.note):
        _die("provide --blocker or --note")
    st.append_event("observation.recorded", data)
    print("recorded")
    return 0


def cmd_contract(a) -> int:
    st = _resolve_run(a.run, None)
    from .contract import contract_summary, contract_hash, rebase, ContractError
    c = json.loads(st.contract_path().read_text())
    if a.contract_cmd == "show":
        print(json.dumps(c, indent=2) if a.json else contract_summary(c) + f"\nhash {contract_hash(c)}"); return 0
    if a.contract_cmd == "rebase":
        tok = os.environ.get(ENV_TOKEN)
        if tok:
            _die("rebase is an owner/controller command, not available inside run sessions", 3)
        changes = json.loads(Path(a.changes).read_text())
        try:
            n = rebase(c, changes, a.reason, now_iso())
        except ContractError as e:
            _die(str(e))
        atomic_write_json(st.contract_path(), n); atomic_write_json(st.contract_path(n["contract_version"]), n)
        with st.transaction() as s:
            s["contract_hash"] = contract_hash(n); s["contract_version"] = n["contract_version"]
            s["criteria"] = {x["id"]: {"status": "FAIL", "evidence_ids": [], "history": []} for x in n["criteria"]}
            s["loop"]["last_eval_input_hash"] = None
        st.append_event("contract.rebase", {"version": n["contract_version"], "hash": contract_hash(n), "reason": a.reason, "changed": sorted(changes)})
        print(f"contract v{n['contract_version']} {contract_hash(n)[:12]}; all criteria reset to FAIL")
        return 0
    return 1


def cmd_hook(a) -> int:
    from .hooks import handle
    return handle(a.event)


def cmd_adapter(a) -> int:
    from .adapters import load_adapter, ADAPTER_NAMES
    if a.adapter_cmd == "list":
        for n in ADAPTER_NAMES:
            print(f"{n:14s} {load_adapter(n).description}")
        return 0
    if a.adapter_cmd == "vr-visual" and a.vr_cmd == "manifest":
        ad = load_adapter("vr_visual", {"views": a.view} if a.view else None)
        known = set()
        m = ad.build_manifest(Path(a.dir), None, known)
        out = Path(a.out or (Path(a.dir) / "capture_manifest.json"))
        atomic_write_json(out, m)
        print(f"{'ok' if m['ok'] else 'PROBLEMS: ' + '; '.join(m['problems'])} -> {out}")
        return 0 if m["ok"] else 1
    return 1


def cmd_migrate(a) -> int:
    from .migrate import migrate_project, migrate_global
    if a.global_bundle:
        print(json.dumps(migrate_global(dry_run=a.dry_run), indent=2)); return 0
    print(json.dumps(migrate_project(Path(a.project).resolve(), dry_run=a.dry_run), indent=2)); return 0


def cmd_uninstall(a) -> int:
    from .migrate import uninstall
    print(json.dumps(uninstall(remove_data=a.purge, dry_run=a.dry_run), indent=2)); return 0


def cmd_models(a) -> int:
    from .models import describe, write_default_table
    if a.init:
        print("wrote", write_default_table())
    print(describe(a.driver))
    return 0


def _live_chain_on(root: Path) -> dict | None:
    """A `go` chain already working this project, or None.

    Two chains on one repository is not a race the harness can win: each freezes its own baseline from the
    same HEAD, each finishes into the same branch, and the first one to land leaves the other's fast-forward
    impossible — the work is stranded and the owner discovers it hours later. It also puts two Unity editors
    and two multi-gigabyte worktrees on one machine. Observed on 2026-08-18: two chains, every landing
    refused, two outcomes carried into main by hand. A live run whose controller process is gone is not a
    chain; it is wreckage, and it is reported separately by `longrun doctor`."""
    from .token import controller_alive
    for st in find_active_runs(root):
        pid = st.get("controller_pid")
        if pid and controller_alive(int(pid)):
            return st
    return None


def _acquire_chain_lock(root: Path):
    """Atomically exclude simultaneous launchd jobs before either can create a run.

    State-based detection alone has a race: two RunAtLoad jobs can both scan before
    either writes controller_pid.  flock closes that reboot/login window.
    """
    path = chain_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _branches_requiring_landing(root: Path) -> list[tuple[str, int]]:
    """Return unmerged branches whose run has independently accepted work.

    A RESET_RECOMMENDED branch is intentionally retained for audit, but its
    changes were not accepted by an evaluator and cannot be treated as the base
    for the next outcome.  Blocking all such branches made an unlimited chain
    permanently stop after one bounded failure even though it can safely plan
    the next outcome from main.  Unknown branches stay conservative and block.
    """
    blocking: list[tuple[str, int]] = []
    for branch, count in G.unmerged_run_branches(root):
        prefix = branch.removeprefix("longrun/")
        matches = [d for d in runs_root().glob(f"{prefix}*") if (d / "state.json").is_file()]
        if len(matches) != 1:
            blocking.append((branch, count))
            continue
        try:
            status = json.loads((matches[0] / "state.json").read_text()).get("status")
        except (OSError, ValueError):
            blocking.append((branch, count))
            continue
        if status in ("PASSED", "PARTIAL_PASS"):
            blocking.append((branch, count))
    return blocking


def _chain_error_was_before_execution(state: dict) -> bool:
    return not (state.get("contract_hash") or state.get("baseline") or
                (state.get("counters") or {}).get("rounds", 0) > 0)


def _chain_outcome_allows_advance(status: str) -> bool:
    return status in ("PASSED", "PARTIAL_PASS")


# Owner intent and external signals are never skipped past; every other
# non-PASS status is a bounded failure governed by the continuation rule below.
_CHAIN_OWNER_STOP_STATUSES = ("STOPPED", "INTERRUPTED", "OWNER_JUDGMENT_REQUIRED")


def _chain_failure_signature(state: dict) -> str:
    return str(((state.get("last_verdict") or {}).get("failure_signature")
                or state.get("terminal_reason") or ""))[:160]


def _chain_failure_continuation(failed_so_far: int, signature: str,
                                previous_signature: str | None) -> str | None:
    """Return a stop reason, or None when exactly one continuation is allowed.

    Policy: sequential outcomes are gated by evaluator-accepted progress. One
    bounded failure buys exactly one strategy change for the NEXT outcome; a
    second consecutive failure — or an immediate repeat of the same failure
    signature — honestly stops the chain instead of replanning the same thing.
    """
    if failed_so_far >= 1:
        return ("a previous failed outcome already used the single allowed strategy "
                "change; stopping instead of replanning")
    if signature and signature == previous_signature:
        return f"same failure signature repeats ({signature}); stopping"
    return None


def cmd_go(a) -> int:
    """One sentence in, bounded autonomous run out: auto-plan (Fable planner) -> freeze -> run. No flags needed."""
    from .controller import (create_run, auto_plan, run_loop, finish, AutoPlanFailedError, ControllerError,
                             TerminalQuotaError, StrategicModelStalledError, NonRetryableProviderRequestError)
    from .config import load as _cfg
    from .contract import contract_summary
    root = Path(a.project).resolve()
    chain_lock = _acquire_chain_lock(root)
    if chain_lock is None:
        print(f"[go] REFUSING TO START. Another longrun go controller already holds the project lock for {root}.",
              flush=True)
        return 1
    # Stop latches apply to the chain that was already running.  An explicit
    # new `go` is the owner's unambiguous request to start again.
    chain_stop_marker(root).unlink(missing_ok=True)
    cfg = root / ".longrun" / "config.json"
    defaults = json.loads(cfg.read_text()) if cfg.is_file() else {}
    adapter = a.adapter or defaults.get("adapter", "software")
    driver = a.driver or defaults.get("driver", "claude")
    chain = a.chain if a.chain is not None else 1
    unlimited = chain == 0            # --chain 0: keep taking the next outcome until stopped
    label = '∞' if unlimited else str(chain)
    last = None
    history = []            # what happened to earlier outcomes in this invocation, fed to the planner
    i = 0
    consecutive_start_failures = 0
    failed_outcomes = 0
    last_fail_sig: str | None = None
    live = _live_chain_on(root)
    if live:
        print(f"\n[go] REFUSING TO START. Another longrun chain is already working {root.name}: run "
              f"{live['run_id'][:8]} ({live.get('status')}), controller pid {live.get('controller_pid')}.\n"
              f"     Two chains on one repository strand each other's work: both freeze from the same HEAD and only\n"
              f"     the first can fast-forward into it. Stop that one first (`longrun stop --run {live['run_id'][:8]}`),\n"
              f"     Reattach with `longrun watch --run {live['run_id'][:8]}`; --force cannot bypass this invariant.", flush=True)
        return 1
    while unlimited or i < chain:
        if chain_stop_marker(root).exists():
            print("[go] stopped by owner; not starting another outcome.", flush=True)
            last = "STOPPED"
            break
        i += 1
        # Between outcomes this chain owns no active run, so anything live here belongs to somebody else.
        live = _live_chain_on(root)
        if live:
            print(f"[go] another chain ({live['run_id'][:8]}) is now working {root.name}; stopping this one "
                  f"instead of stranding its work.", flush=True)
            return 1
        stranded = _branches_requiring_landing(root)
        if stranded:
            names = ", ".join(f"{b} (+{n})" for b, n in stranded)
            print(f"\n[go] REFUSING TO START. Earlier accepted run branches still hold unmerged work: {names}.\n"
                  f"     Starting now would plan against a base that is missing it. Merge those branches or\n"
                  f"     delete them, then run `longrun go` again.", flush=True)
            return 1
        goal = a.goal
        if history:
            goal += "\n\nEarlier outcomes in this invocation (do NOT repeat a failed one; pick the next most valuable visible outcome instead): " + " | ".join(history)
        st = None
        try:
            st = create_run(root, adapter, driver, isolation="auto", allow_dirty=True)
            with st.transaction() as s:
                s["chain_context"] = {
                    "index": i,
                    "limit": None if unlimited else chain,
                    "continues_after_pass": unlimited or i < chain,
                }
            if chain_stop_marker(root).exists():
                (st.dir / "STOP").write_text(now_iso())
                with st.transaction() as s:
                    s["status"] = "STOPPED"; s["terminal_reason"] = "stopped by owner before planning"; s["ended_at"] = now_iso()
                st.append_event("run.finished", {"status": "STOPPED", "reason": "stopped by owner before planning"})
                last = "STOPPED"
                break
            print(f"[go {i}/{label}] run {st.run_id[:8]}: planning from goal…", flush=True)
            c = auto_plan(st, goal, model=a.eval_model)
            print(contract_summary(c), flush=True)
            with st.transaction() as s:
                s["permission_mode"] = _cfg().get("default_permission_mode", "bypassPermissions")
            status = run_loop(st.run_id, model=a.model, eval_model=a.eval_model)
        except TerminalQuotaError as e:
            finish(st, "FAILED", f"terminal provider quota: {e}")
            print(f"[go {i}/{label}] terminal provider quota; stopping chain without retry: {e}", flush=True)
            last = "FAILED"
            break
        except NonRetryableProviderRequestError as e:
            if st is not None:
                finish(st, "FAILED", f"non-retryable provider request: {e}")
            print(f"[go {i}/{label}] provider rejected a deterministic request/configuration; "
                  f"stopping chain without retry: {e}", flush=True)
            last = "FAILED"
            break
        except StrategicModelStalledError as e:
            finish(st, "FAILED", str(e))
            print(f"[go {i}/{label}] {e}; stopping chain", flush=True)
            last = "FAILED"
            break
        except AutoPlanFailedError as e:
            # Contract exhaustion is terminal for this invocation. Treating it like a freeze/start
            # refusal used to make --chain silently create a fresh run and repeat the same planning
            # cycle after the current run had already been marked FAILED.
            print(f"[go {i}/{label}] contract planning exhausted; stopping chain: {e}", flush=True)
            last = "FAILED"
            break
        except ControllerError as e:
            # An owner who typed `longrun stop` is not a failure to skip past. `stop` drops a STOP file and,
            # mid-planning, surfaces here as a ControllerError — so without this check the skip-and-continue
            # below quietly started the *next* outcome instead, and the chain the owner had just stopped went
            # on working (observed 2026-08-20, minutes after the skip path was added).
            if st is not None and (st.dir / "STOP").exists():
                print(f"[go {i}/{label}] stopped by owner during planning.", flush=True)
                last = "STOPPED"
                break
            if st is not None:
                current = st.read(verify=False)
                if current.get("status") == "OWNER_JUDGMENT_REQUIRED":
                    print(f"[go {i}/{label}] owner judgment required; stopping chain: "
                          f"{current.get('terminal_reason') or e}", flush=True)
                    last = "OWNER_JUDGMENT_REQUIRED"
                    break
                # ControllerError is also raised after an outcome has already
                # frozen and executed (for example when its wall budget expires
                # before an infrastructure recovery). That is not a start
                # failure; finish it honestly and apply the same one-shot
                # continuation rule as any other bounded failure.
                if not _chain_error_was_before_execution(current):
                    finish(st, "FAILED", f"outcome failed after execution started: {e}")
                    history.append(f"{c['outcome_id']} -> FAILED ({str(e)[:120]})")
                    last = "FAILED"
                    fail_sig = str(e)[:160]
                    stop_reason = _chain_failure_continuation(failed_outcomes, fail_sig, last_fail_sig)
                    if stop_reason:
                        print(f"[go {i}/{label}] {stop_reason}", flush=True)
                        break
                    failed_outcomes += 1
                    last_fail_sig = fail_sig
                    print(f"[go {i}/{label}] execution failed after start; using the single "
                          f"allowed strategy change for the next outcome", flush=True)
                    continue
            # Otherwise: one outcome failing to start is not a reason to throw away the rest. A `--chain 3`
            # used to die whole on the first ControllerError — on 2026-08-19 a freeze refusal did exactly that
            # to two chains, after their contracts were already paid for. Skip the outcome, tell the next
            # planner it was skipped, and stop only if nothing gets off the ground twice running.
            consecutive_start_failures += 1
            print(f"[go {i}/{label}] outcome could not start: {e}", flush=True)
            history.append(f"outcome {i} could not start ({str(e)[:120]})")
            if consecutive_start_failures >= 2:
                _die(f"two outcomes in a row failed to start; last error: {e}")
            continue
        consecutive_start_failures = 0
        print(f"[go {i}/{label}] run {st.run_id[:8]} -> {status}", flush=True)
        last = status
        s = st.read(verify=False)
        if status in ("PASSED", "PARTIAL_PASS") and s.get("isolation") == "worktree" and s.get("workspace") and Path(s["workspace"]) != root:
            # A finished outcome must land in the project, otherwise the next outcome starts from a stale base.
            # PARTIAL_PASS lands too, and that is deliberate: it used to be the one status that both left a
            # branch behind and let the chain continue, so the next iteration hit the unmerged-branch guard
            # created by its own previous iteration and the whole chain died — a PARTIAL_PASS was structurally
            # unsurvivable (observed 2026-08-19 on m4-llm-optimization-lab, 1143 verified lines stranded and the
            # night over). The work being landed is not unchecked: an independent evaluator confirmed the
            # criteria that passed at this revision, and the ones still open are named in `history` so the next
            # planner finishes them instead of re-planning them.
            branch = f"longrun/{st.run_id[:8]}"
            ok, out = G.fast_forward_into(root, branch)
            st.append_event("run.merged" if ok else "run.merge_skipped", {"branch": branch, "detail": out[:300]})
            print(f"[go {i}/{label}] {'merged' if ok else 'NOT merged'} {branch} into {root.name}: {out[:160]}", flush=True)
            if not ok:
                # Never continue past this: the outcome PASSED but its work is not in the project, so the next
                # planner would re-plan work that is already done and the branch would sit there unnoticed.
                print(f"\n[go] STOPPING THE CHAIN. {branch} finished ({status}) but did not land in {root.name}: {out[:200]}\n"
                      f"     Its commits are safe on that branch. Reconcile before running again — either merge\n"
                      f"     `git merge {branch}` by hand, or drop the branch and let a fresh run redo the outcome\n"
                      f"     from the current base. Do not start a new chain until `longrun doctor` is clean.",
                      flush=True)
                last = "LANDING_BLOCKED"
                break
        sig = ((s.get("last_verdict") or {}).get("failure_signature") or s.get("terminal_reason") or "")[:160]
        entry = f"{c['outcome_id']} -> {status}" + (f" ({sig})" if status != "PASSED" and sig else "")
        if status == "PARTIAL_PASS":
            # Verified work has landed. Name what is verified and what is still open, both on screen and in the
            # history the next planner reads, so the next outcome finishes the remainder rather than redoing it.
            crit = s.get("criteria") or {}
            verified = [k for k, v in crit.items() if v.get("status") == "PASS"]
            still_open = [k for k, v in crit.items() if v.get("status") != "PASS"]
            print(f"[go {i}/{label}] {st.run_id[:8]} verified {len(verified)}/{len(crit)} criteria: "
                  f"{', '.join(verified)} — still open: {', '.join(still_open)}", flush=True)
            entry += f" [verified and landed: {', '.join(verified)}; STILL OPEN: {', '.join(still_open)}]"
        history.append(entry)
        if _chain_outcome_allows_advance(status):
            # Evaluator-accepted progress resets the failure budget: a failure
            # after real progress is a new episode, not a consecutive one.
            failed_outcomes = 0
            last_fail_sig = None
            continue
        # A provider-wide terminal condition (exhausted weekly quota, rejected
        # request configuration) is not repairable by another outcome on the
        # same provider; continuing would only re-buy the identical failure.
        kinds = {e["kind"] for e in st.events()}
        if kinds & {"session.terminal_quota", "session.non_retryable_provider_request"}:
            print(f"[go] STOPPING THE CHAIN: provider-wide terminal condition in outcome {i}.", flush=True)
            break
        if status in _CHAIN_OWNER_STOP_STATUSES:
            break             # owner intent and external signals are never skipped past
        stop_reason = _chain_failure_continuation(failed_outcomes, sig, last_fail_sig)
        if stop_reason:
            print(f"\n[go] STOPPING THE CHAIN after outcome {i} ({status}): {stop_reason}\n"
                  f"     Failure detail: {sig or 'none recorded'}", flush=True)
            break
        failed_outcomes += 1
        last_fail_sig = sig
        print(f"[go {i}/{label}] {status}; using the single allowed strategy change; "
              f"next outcome starts fresh ({sig or 'no failure signature recorded'})", flush=True)
        continue
    return 0 if last in ("PASSED", "PARTIAL_PASS") else 1


def cmd_progress(a) -> int:
    """Read-only replay of a run's progress. `watch` needs you sitting there while it happens; this answers
    "what did it actually do?" afterwards, from the stream files and the event ledger already on disk."""
    from .narrate import progress
    st = _resolve_run(a.run, Path(a.project) if a.project else None)
    return progress(st, limit=a.lines)


def _next_run_of_chain(st: RunStore, poll_s: float = 3.0, grace_s: float = 90.0) -> RunStore | None:
    """The run a still-live `go` chain moved on to after this one finished, or None if the chain is over.

    A `--chain` invocation gives every outcome its own run id, so watching one id ends the moment that outcome
    does — which reads exactly like "the whole task finished" and is not what anyone watching a chain wants.
    While a controller for this project is alive, wait for the project marker to point somewhere new and follow
    it. The grace window covers the gap between one outcome finishing and the next being created (planning
    starts a few seconds later); no live controller means the chain really is over, so stop."""
    from .paths import project_marker
    root = Path(st.read(verify=False)["project_root"])
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if _live_chain_on(root) is None:      # the `go` process lives across outcomes; gone means the chain is
            return None                        # over, whether it finished its count or the owner stopped it
        try:
            rid = json.loads(project_marker(root).read_text()).get("last_run_id")
        except (OSError, ValueError):
            rid = None
        if rid and rid != st.run_id and RunStore(rid).exists():
            return RunStore(rid)
        time.sleep(poll_s)
    return None


def cmd_watch(a) -> int:
    """Live view: status header + tail of controller.log, refreshed until the run is terminal (Ctrl-C to leave)."""
    import subprocess as _sp
    st = _resolve_run(a.run, Path(a.project) if a.project else None)
    logp = st.dir / "controller.log"
    if not a.log and not a.counters:
        from .narrate import narrate
        rc = 0
        since_start = a.from_start
        while True:
            rc = narrate(st, interval=max(1, a.interval), verbose=a.verbose, since_start=since_start,
                         quiet=getattr(a, "quiet", False))
            nxt = _next_run_of_chain(st)
            if nxt is None:
                return rc
            print(f"\n▶ next outcome of this chain: run {nxt.run_id[:8]}\n", flush=True)
            st, since_start = nxt, True     # a new run: show it from its beginning, not from "now"
    print(f"run dir: {st.dir}")
    if a.log:
        print(f"log: {logp}")
        return _sp.call(["tail", "-n", "40", "-f", str(logp)]) if logp.exists() else (print("(no controller.log yet)") or 0)
    seen = 0
    try:
        while True:
            s = st.read(verify=False)
            c = s["counters"]
            crit = " ".join(f"{k}={v.get('status')}" for k, v in s.get("criteria", {}).items())
            print(f"\r[{now_iso()}] {s['run_id'][:8]} {s['status']} rounds={c['rounds']} repairs={c['repairs']} restarts={c['fresh_restarts']} evals={c['evaluations']} ${c.get('cost_usd', 0):.2f} | {crit}", flush=True)
            if logp.exists():
                lines = logp.read_text().splitlines()
                for line in lines[seen:]:
                    print("   " + line)
                seen = len(lines)
            if s["status"] in TERMINAL_STATES:
                print(f"terminal: {s.get('terminal_reason')}"); return 0
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0


def cmd_cost(a) -> int:
    """What was actually spent, by role. Reporting only — reads the cost each child session reported."""
    from . import cost as C
    # Naming a run is already unambiguous; also filtering it by the directory the report happens to be run
    # from turns `longrun cost --run <id>` into "no runs recorded" whenever you are standing somewhere else.
    project = None if (a.all or a.run) else Path(a.project or ".").resolve()
    runs = C.find_runs(project=project, run_prefix=a.run)
    if not runs:
        where = "any project" if a.all else str(project)
        print(f"no runs recorded for {where}" + (f" matching {a.run!r}" if a.run else ""))
        return 0
    print(C.report(runs, per_run=not a.no_per_run))
    return 0


def cmd_runs(a) -> int:
    root = runs_root()
    rows = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        p = d / "state.json"
        if p.is_file():
            try:
                s = json.loads(p.read_text())
                rows.append((s["created_at"], s["run_id"], s["status"], s["adapter"], s["project_root"]))
            except Exception:
                continue
    for r in sorted(rows):
        print("  ".join(str(x) for x in r))
    return 0


# ------------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="longrun", description="Long-Horizon Outcome Harness (inactive unless invoked)")
    ap.add_argument("--version", action="version", version=f"longrun {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor"); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("init"); p.add_argument("--project", default="."); p.add_argument("--adapter", default="software")
    p.add_argument("--driver", default="claude", choices=["claude", "codex", "opencode"]); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("plan"); p.add_argument("--project", default="."); p.add_argument("--contract", required=True)
    p.add_argument("--adapter"); p.add_argument("--driver", choices=["claude", "codex", "opencode"])
    p.add_argument("--isolation", default="auto", choices=["auto", "worktree", "none"]); p.add_argument("--allow-dirty", action="store_true"); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("run"); p.add_argument("--run"); p.add_argument("--project"); p.add_argument("--model", help="builder model"); p.add_argument("--eval-model", help="model for the strategic roles (evaluator, restart manager); default opus, env LONGRUN_STRATEGIC_MODEL")
    p.add_argument("--permission-mode", choices=["acceptEdits", "auto", "dontAsk", "bypassPermissions"], help="builder mode; default acceptEdits+allowlist; auto = Claude auto-mode; bypassPermissions only with --allow-bypass")
    p.add_argument("--allow-bypass", action="store_true", help="owner explicitly accepts a blanket permission bypass for the builder (recorded in the run events)"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("status"); p.add_argument("--run"); p.add_argument("--project"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    for name in ("evaluate", "checkpoint"):
        p = sub.add_parser(name); p.add_argument("--run"); p.add_argument("--project"); p.add_argument("--model"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_evaluate)
    p = sub.add_parser("stop"); p.add_argument("--run"); p.add_argument("--project"); p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("reset"); p.add_argument("--run"); p.add_argument("--project"); p.set_defaults(fn=cmd_reset)
    p = sub.add_parser("prune", help="delete workspaces of finished runs whose work is merged")
    p.add_argument("--project"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_prune)
    p = sub.add_parser("go", help="one sentence in -> auto-planned, bounded autonomous run (no flags needed)")
    p.add_argument("--goal", required=True); p.add_argument("--project", default="."); p.add_argument("--adapter"); p.add_argument("--driver", choices=["claude", "codex", "opencode"])
    p.add_argument("--model"); p.add_argument("--eval-model"); p.add_argument("--force", action="store_true", help="deprecated compatibility flag; one live run per project cannot be bypassed"); p.add_argument("--chain", type=int, help="run up to N consecutive outcomes (default 1); 0 = unlimited, until `longrun stop`/Ctrl-C/owner judgment/failure"); p.set_defaults(fn=cmd_go)
    p = sub.add_parser("cost", help="what runs cost, by role (reporting only)")
    p.add_argument("--project", help="default: the current directory"); p.add_argument("--run", help="one run (id prefix ok)")
    p.add_argument("--all", action="store_true", help="every project, not just this one")
    p.add_argument("--no-per-run", action="store_true", help="totals only"); p.set_defaults(fn=cmd_cost)
    p = sub.add_parser("runs"); p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("watch", help="live status + controller log for a run (id prefix ok)"); p.add_argument("--run"); p.add_argument("--project"); p.add_argument("--interval", type=int, default=2); p.add_argument("--log", action="store_true", help="just tail -f the controller log"); p.add_argument("--counters", action="store_true", help="numeric status line instead of the narrative"); p.add_argument("--verbose", "-v", action="store_true", help="also show tool results and thinking"); p.add_argument("--from-start", action="store_true", help="replay the whole run so far, then follow"); p.add_argument("--quiet", "-q", action="store_true", help="only the sessions' own progress sentences, no tool calls"); p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("progress", help="what a run did, readably — works after it finished (no extra tokens)"); p.add_argument("--run"); p.add_argument("--project"); p.add_argument("-n", "--lines", type=int, help="only the last N lines"); p.set_defaults(fn=cmd_progress)
    p = sub.add_parser("models"); p.add_argument("--driver", default="claude", choices=["claude", "codex", "opencode"]); p.add_argument("--init", action="store_true", help="write ~/.local/share/longrun/models.json with defaults for editing"); p.set_defaults(fn=cmd_models)
    p = sub.add_parser("evidence"); es = p.add_subparsers(dest="evidence_cmd", required=True)
    q = es.add_parser("submit"); q.add_argument("--criterion", action="append", required=True); q.add_argument("--kind", required=True)
    q.add_argument("--summary", required=True); q.add_argument("--cmd"); q.add_argument("--exit", type=int); q.add_argument("--artifact", action="append", default=[])
    q.add_argument("--revision"); q.add_argument("--run-id"); q.add_argument("--contract-hash"); q.add_argument("--stdin", action="store_true")
    q = es.add_parser("list"); q.add_argument("--run")
    p.set_defaults(fn=cmd_evidence)
    p = sub.add_parser("observe"); p.add_argument("--blocker"); p.add_argument("--note"); p.set_defaults(fn=cmd_observe)
    p = sub.add_parser("contract"); cs = p.add_subparsers(dest="contract_cmd", required=True)
    q = cs.add_parser("show"); q.add_argument("--run"); q.add_argument("--json", action="store_true")
    q = cs.add_parser("rebase"); q.add_argument("--run"); q.add_argument("--changes", required=True); q.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_contract)
    p = sub.add_parser("hook"); p.add_argument("event", choices=["stop", "session-start", "pre-tool-use", "task-completed"]); p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("adapter"); asub = p.add_subparsers(dest="adapter_cmd", required=True)
    asub.add_parser("list")
    v = asub.add_parser("vr-visual"); vs = v.add_subparsers(dest="vr_cmd", required=True)
    m = vs.add_parser("manifest"); m.add_argument("--dir", required=True); m.add_argument("--out"); m.add_argument("--view", action="append")
    p.set_defaults(fn=cmd_adapter)
    p = sub.add_parser("migrate"); p.add_argument("--project", default="."); p.add_argument("--global-bundle", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser("uninstall"); p.add_argument("--purge", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_uninstall)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
