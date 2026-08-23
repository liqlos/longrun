"""What a run actually cost, by role.

The harness already writes every child session's stream to `sessions/<id>.<role>.stream.jsonl`, and the
driver's final `result` record carries `total_cost_usd` and the token counts. Nothing here estimates or
prices anything — it reads the figure the CLI itself reported and adds it up. That matters: the first
time this was worked out by hand, the answer contradicted the obvious guess (the bill was dominated by
cache *creation* on short strategic sessions, not by thinking), and a guess would have optimised the
wrong thing.

Reporting only. It never gates, never warns, and never changes a run.
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import runs_root

ROLE_ORDER = ["builder", "evaluator", "planner", "restart_manager"]


def _blank() -> dict:
    return {"calls": 0, "priced": 0, "cost": 0.0, "out": 0, "cache_read": 0, "cache_write": 0, "seconds": 0.0}


def session_costs(run_dir: Path) -> list[dict]:
    """One record per child session of this run, in file order."""
    out = []
    sess = run_dir / "sessions"
    if not sess.is_dir():
        return out
    for f in sorted(sess.glob("*.stream.jsonl")):
        parts = f.name.split(".")
        role = parts[-3] if len(parts) >= 3 else "unknown"
        last = None
        for line in f.read_text(errors="replace").splitlines():
            if '"total_cost_usd"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "result":
                last = e
        u = (last or {}).get("usage") or {}
        out.append({
            "session_id": parts[0], "role": role,
            # A session that died before its first result line still cost wall-clock and still shows up
            # here at $0 — hiding it would understate how many calls a night actually made. `priced` says
            # whether a cost figure existed at all: the codex driver reports none, and printing its runs as
            # "$0.00" would present an unavailable number as a cheap one, in the one module whose whole
            # claim is that it never invents a figure.
            "priced": last is not None and last.get("total_cost_usd") is not None,
            "cost": float((last or {}).get("total_cost_usd") or 0.0),
            "out": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0),
            "seconds": float((last or {}).get("duration_ms") or 0) / 1000.0,
        })
    return out


def by_role(run_dirs: list[Path]) -> tuple[dict, list[tuple[Path, float]]]:
    roles: dict[str, dict] = {}
    per_run: list[tuple[Path, float]] = []
    for d in run_dirs:
        total = 0.0
        for s in session_costs(d):
            r = roles.setdefault(s["role"], _blank())
            r["calls"] += 1
            r["priced"] += 1 if s["priced"] else 0
            for k in ("cost", "out", "cache_read", "cache_write", "seconds"):
                r[k] += s[k]
            total += s["cost"]
        per_run.append((d, total))
    return roles, per_run


def find_runs(project: Path | None = None, run_prefix: str | None = None) -> list[Path]:
    root = runs_root()
    found = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        sp = d / "state.json"
        if not sp.is_file():
            continue
        if run_prefix and not d.name.startswith(run_prefix):
            continue
        if project is not None:
            try:
                st = json.loads(sp.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            # An empty project_root would resolve to the *current* directory and silently match whatever
            # project the report was invoked from, so a run that does not name its project is skipped.
            root = str(st.get("project_root") or "")
            if not root or Path(root).resolve() != project:
                continue
        found.append(d)
    return sorted(found, key=lambda p: (p / "state.json").stat().st_mtime)


def _status(d: Path) -> str:
    try:
        return str(json.loads((d / "state.json").read_text()).get("status", "?"))
    except (json.JSONDecodeError, OSError):
        return "?"


def report(run_dirs: list[Path], *, per_run: bool = True) -> str:
    roles, runs = by_role(run_dirs)
    total = sum(v["cost"] for v in roles.values())
    L = [f"{len(run_dirs)} run(s), ${total:,.2f}"]
    if not roles:
        return L[0] + " — no child sessions recorded"
    L += ["", f"{'role':<17}{'calls':>6}{'$':>10}{'%':>5}{'out tok':>12}{'cache rd':>14}{'cache wr':>13}{'hours':>7}"]
    order = [r for r in ROLE_ORDER if r in roles] + sorted(set(roles) - set(ROLE_ORDER))
    unpriced = False
    for role in order:
        v = roles[role]
        if v["priced"]:
            money, pct = f"{v['cost']:>10.2f}", f"{100 * v['cost'] / max(total, 1e-9):>5.0f}"
        else:
            money, pct, unpriced = f"{'n/a':>10}", f"{'-':>5}", True
        L.append(f"{role:<17}{v['calls']:>6}{money}{pct}"
                 f"{v['out']:>12,}{v['cache_read']:>14,}{v['cache_write']:>13,}{v['seconds'] / 3600:>7.1f}")
    if unpriced:
        L.append("  n/a = the driver reported no cost for these sessions (codex does not); tokens are still real")
    if per_run and len(runs) > 1:
        L += ["", "per run:"]
        for d, c in runs:
            L.append(f"  {d.name[:8]}  {_status(d):<19} ${c:>8.2f}")
    return "\n".join(L)
