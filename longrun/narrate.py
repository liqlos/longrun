"""Human-readable live narration of a run: what each session (builder / evaluator / planner / restart manager)
is thinking, which tools it runs, what came back, plus controller milestones. Reads the recorded stream files;
never touches run state."""
from __future__ import annotations
import json
import time
from pathlib import Path

from .store import RunStore, TERMINAL_STATES

ICON = {"builder": "🔨", "evaluator": "⚖️ ", "planner": "🗺️ ", "restart_manager": "🔁"}


def _one(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_tool(name: str, inp: dict) -> str:
    if name == "Bash":
        return f"$ {_one(inp.get('command', ''), 160)}"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"{name.lower()} {inp.get('file_path') or inp.get('notebook_path') or ''}"
    if name in ("Read", "Glob", "Grep"):
        return f"{name.lower()} {_one(inp.get('file_path') or inp.get('pattern') or inp.get('path') or '', 120)}"
    if name in ("Agent", "Task"):
        return f"subagent[{inp.get('subagent_type') or inp.get('model') or ''}] {_one(inp.get('description') or inp.get('prompt', ''), 120)}"
    return f"{name} {_one(json.dumps(inp, ensure_ascii=False), 120)}"


def render_event(ev: dict, role: str, verbose: bool, quiet: bool = False) -> list[str]:
    """`quiet` keeps only what a human reads for progress: the session's own narration sentences and the
    end-of-session summary. Every tool call and every thinking block is dropped — at 120 turns a round those
    scroll the one sentence that says what is going on off the screen."""
    out = []
    t = ev.get("type")
    tag = ICON.get(role, "•")
    if t == "assistant":
        for blk in (ev.get("message") or {}).get("content", []) or []:
            if not isinstance(blk, dict):
                continue
            k = blk.get("type")
            if k == "text" and blk.get("text", "").strip():
                out.append(f"{tag} {_one(blk['text'], 300) if quiet else blk['text'].strip()}")
            elif quiet:
                continue
            elif k == "thinking" and blk.get("thinking", "").strip():
                out.append(f"{tag} 💭 {_one(blk['thinking'], 400 if not verbose else 2000)}")
            elif k == "tool_use":
                out.append(f"{tag} → {_fmt_tool(blk.get('name', ''), blk.get('input') or {})}")
    elif t == "user" and verbose:
        for blk in (ev.get("message") or {}).get("content", []) or []:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                c = blk.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if c:
                    out.append(f"      ↳ {'ERR ' if blk.get('is_error') else ''}{_one(c, 200)}")
    elif t == "result":
        out.append(f"{tag} ■ session ended: turns={ev.get('num_turns')} cost=${float(ev.get('total_cost_usd') or 0):.2f} "
                   f"{'error: ' + str(ev.get('subtype')) if ev.get('is_error') else 'ok'}")
    elif t == "item.completed":   # codex
        it = ev.get("item") or {}
        k = it.get("type")
        if k == "agent_message" and it.get("text"):
            out.append(f"{tag} {_one(it['text'], 300) if quiet else it['text'].strip()}")
        elif quiet:
            pass
        elif k == "command_execution":
            out.append(f"{tag} → $ {_one(it.get('command', ''), 160)}")
        elif k == "file_change":
            out.append(f"{tag} → edit " + ", ".join(ch.get("path", "") for ch in it.get("changes") or []))
        elif k == "reasoning" and it.get("text"):
            out.append(f"{tag} 💭 {_one(it['text'], 400)}")
    elif t == "text":             # opencode --format json
        value = str((ev.get("part") or {}).get("text") or "").strip()
        if value:
            out.append(f"{tag} {_one(value, 300) if quiet else value}")
    elif t == "reasoning":        # opencode; omit from the concise owner view
        value = str((ev.get("part") or {}).get("text") or "").strip()
        if value and not quiet:
            out.append(f"{tag} 💭 {_one(value, 400 if not verbose else 2000)}")
    elif t == "tool_use":         # opencode includes the completed result in the same event
        if quiet:
            return out
        part = ev.get("part") or {}
        state = part.get("state") or {}
        inp = dict(state.get("input") or {})
        if "filePath" in inp and "file_path" not in inp:
            inp["file_path"] = inp["filePath"]
        name = {"bash": "Bash", "write": "Write", "edit": "Edit"}.get(
            str(part.get("tool") or ""), str(part.get("tool") or "unknown"))
        out.append(f"{tag} → {_fmt_tool(name, inp)}")
        result = state.get("output")
        if verbose and result not in (None, ""):
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
            out.append(f"      ↳ {'ERR ' if state.get('status') == 'error' else ''}{_one(result, 200)}")
    elif t == "error":            # opencode provider/session error
        message = (ev.get("part") or {}).get("message") or ev.get("message")
        if message:
            out.append(f"{tag} ERR {_one(str(message), 300)}")
    return out


# ---------------------------------------------------------------------------- after-the-fact progress
MILESTONES = {
    "run.created":       lambda d: "run created",
    "contract.planned":  lambda d: "contract planned: " + ", ".join(d.get("criteria") or []),
    "run.frozen":        lambda d: f"baseline frozen at {str(d.get('revision'))[:14]}",
    "baseline.rebased":  lambda d: "baseline re-taken (project moved while planning)",
    "round.start":       lambda d: f"── round {d.get('round')} ──",
    "evidence.recorded": lambda d: f"evidence {d.get('id')} {d.get('kind')} → {','.join(d.get('criteria') or [])}",
    "evaluation.start":  lambda d: "evaluator reading the ledger…",
    "evaluation.applied": lambda d: "verdict: " + (", ".join(f"{k} {', '.join(v)}" for k, v in (d.get("delta") or {}).items() if v) or "nothing moved"),
    "evaluation.rejected": lambda d: f"verdict REJECTED: {str(d.get('reason'))[:160]}",
    "evaluation.skipped_no_evidence": lambda d: "evaluation skipped — no new evidence this round",
    "loop.operational":  lambda d: "loop guard killed the session: " + "; ".join(d.get("reasons") or []),
    "loop.strategic":    lambda d: "stagnation: " + "; ".join(d.get("reasons") or []),
    "repair.scheduled":  lambda d: f"repair {d.get('n')} scheduled" + (" (changed strategy)" if d.get("changed_strategy") else ""),
    "restart.decision":  lambda d: f"fresh restart decided: {d.get('decision')}",
    "session.model_failover": lambda d: f"{d.get('role')} failed over to {d.get('to')}",
    "run.merged":        lambda d: f"merged {d.get('branch')} into the project",
    "run.finished":      lambda d: f"■ {d.get('status')} — {str(d.get('reason'))[:200]}",
}


def _session_narration(path: Path) -> list[str]:
    """The sentences a session wrote for the owner, in order. Tool calls and thinking are left out."""
    out: list[str] = []
    role = path.name.split(".")[1] if path.name.count(".") >= 3 else "?"
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.extend(render_event(ev, role, verbose=False, quiet=True))
    return out


def progress(store: RunStore, *, limit: int | None = None) -> int:
    """Replay what a run actually did, readably, from files already on disk — no extra tokens, and it works
    on a run that finished days ago. Milestones come from the event ledger; the prose between them is what
    each session told the owner it was doing."""
    s = store.read(verify=False)
    print(f"▶ run {store.run_id[:8]}  {s.get('status')}  {s.get('adapter')}  {s.get('project_root')}")
    lines: list[str] = []
    streams = {p.name.split(".")[0]: p for p in store.sessions_dir.glob("*.stream.jsonl")}
    for ev in store.events():
        kind, d, ts = ev.get("kind"), ev.get("data") or {}, str(ev.get("ts", ""))[11:19]
        if kind == "session.launch":
            lines.append(f"{ts}  {ICON.get(d.get('role'), '•')} {d.get('role')} started "
                         f"({d.get('model') or 'driver default'}/{d.get('effort') or '-'})")
            for x in _session_narration(streams.get(str(d.get("session_id")), Path("/nonexistent"))):
                lines.append(f"          {x}")
        elif kind == "session.end":
            lines.append(f"{ts}  {ICON.get(d.get('role'), '•')} {d.get('role')} ended: {d.get('num_turns')} turns, "
                         f"${float(d.get('cost_usd') or 0):.2f}{', LOOP-KILLED' if d.get('loop_kill') else ''}")
        elif kind in MILESTONES:
            try:
                lines.append(f"{ts}  {MILESTONES[kind](d)}")
            except Exception:
                lines.append(f"{ts}  {kind}")
    for line in (lines[-limit:] if limit else lines):
        print(line)
    crit = "  ".join(f"{k}={v.get('status')}" for k, v in (s.get("criteria") or {}).items())
    print(f"◆ {crit}")
    return 0


def narrate(store: RunStore, *, interval: float = 2.0, verbose: bool = False, since_start: bool = False, quiet: bool = False) -> int:
    """Follow all session streams of the run (newest first appearing as they are created) plus controller.log."""
    import sys; sys.stdout.reconfigure(line_buffering=True)
    print(f"▶ longrun {store.run_id[:8]}  ({store.dir})  — Ctrl-C to leave; the run keeps going")
    offsets: dict[Path, int] = {}
    log_seen = 0 if since_start else _linecount(store.dir / "controller.log")
    if not since_start:
        for f in store.sessions_dir.glob("*.stream.jsonl"):
            offsets[f] = f.stat().st_size
    last_status = None
    try:
        while True:
            # controller log
            lp = store.dir / "controller.log"
            if lp.exists():
                lines = lp.read_text().splitlines()
                for line in lines[log_seen:]:
                    print(f"⏱ {line.split(' longrun[', 1)[-1] if ' longrun[' in line else line}")
                log_seen = len(lines)
            # session streams (ordered by creation)
            for f in sorted(store.sessions_dir.glob("*.stream.jsonl"), key=lambda p: p.stat().st_mtime):
                role = f.name.split(".")[1] if f.name.count(".") >= 3 else "?"
                off = offsets.get(f, 0)
                with open(f, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read()
                if not chunk:
                    continue
                # keep a partial trailing line for next round
                nl = chunk.rfind(b"\n")
                if nl == -1:
                    continue
                offsets[f] = off + nl + 1
                if off == 0:
                    print(f"── {ICON.get(role, '•')} {role} session {f.name.split('.')[0][:8]} started ──")
                for raw in chunk[: nl + 1].decode("utf-8", "replace").splitlines():
                    raw = raw.strip()
                    if not raw.startswith("{"):
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for line in render_event(ev, role, verbose, quiet):
                        print(line)
            s = store.read(verify=False)
            if s["status"] != last_status:
                crit = " ".join(f"{k}={v.get('status')}" for k, v in s.get("criteria", {}).items())
                print(f"◆ status {s['status']}  {crit}")
                last_status = s["status"]
            if s["status"] in TERMINAL_STATES:
                print(f"■ run finished: {s['status']} — {s.get('terminal_reason')}")
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _linecount(p: Path) -> int:
    return len(p.read_text().splitlines()) if p.exists() else 0
