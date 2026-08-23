"""Session-scoped hook handlers: `longrun hook <event>`.

These are only ever registered through `--settings` on controller-launched sessions. They are written to be
safe even if someone wires them globally: with no valid controller-issued token bound to this session id,
every handler exits 0 with no output (fail open) after a constant-time check.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

from .token import ENV_TOKEN, parse_unverified, verify, controller_alive
from .store import RunStore, ACTIVE_STATES

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MUTATING_BASH = ("git add", "git commit", "rm ", "mv ", "> ", ">>", "tee ", "longrun evidence submit", "longrun observe",
                 "chmod", "sed -i", "python -c", "python3 -c")


def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _auth(payload: dict) -> tuple[RunStore, dict, dict] | None:
    """Return (store, state, token_payload) if and only if this session is a controller-launched run session."""
    tok = os.environ.get(ENV_TOKEN)
    if not tok:
        return None
    p = parse_unverified(tok)
    if not p:
        return None
    store = RunStore(p["run_id"])
    if not store.exists():
        return None
    secret = store.secret()
    if not secret:
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    v = verify(tok, secret, expect_session_id=sid)
    if not v:
        return None
    if not controller_alive(v.get("cpid", -1)):
        return None
    try:
        st = store.read()
    except Exception:
        return None
    if st.get("status") not in ACTIVE_STATES:
        return None
    # session must be registered by the controller as a live child of this run
    if not any(c.get("session_id") == sid and c.get("ended_at") is None for c in st.get("children", [])):
        return None
    return store, st, v


def handle(event: str) -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
    except Exception:
        return 0
    auth = _auth(payload)
    if auth is None:
        return 0                     # ordinary session, spoofed/stale/malformed token, dead controller: no-op
    store, st, tok = auth
    role = tok.get("role")
    try:
        if event == "stop":
            return _stop(store, st, tok, payload)
        if event == "session-start":
            return _session_start(store, st, tok, payload)
        if event == "pre-tool-use":
            return _pre_tool_use(store, st, tok, payload)
        if event == "task-completed":
            return _task_completed(store, st, tok, payload)
    except Exception as e:  # never break a session because of the harness
        try:
            store.append_event("hook.error", {"event": event, "error": str(e)[:300], "role": role})
        except Exception:
            pass
        return 0
    return 0


def _stop(store: RunStore, st: dict, tok: dict, payload: dict) -> int:
    """Block only an authenticated builder session, only for a bounded, actionable continuation:
    it ends without having submitted any evidence or observation this session."""
    if tok.get("role") != "builder":
        return 0
    if payload.get("stop_hook_active"):
        pass  # still bounded by our own counter below
    sid = payload["session_id"]
    from .evidence import list_evidence
    submitted = [e for e in list_evidence(store) if e.get("submitted_by") == sid]
    observed = any(ev["kind"] == "observation.recorded" and ev["data"].get("session_id") == sid for ev in store.events())
    if submitted or observed:
        return 0
    max_blocks = int((st.get("budgets") or {}).get("max_stop_blocks_per_session", 2))
    with store.transaction() as s:
        blocks = s["counters"].setdefault("stop_blocks", {})
        n = int(blocks.get(sid, 0))
        if n >= max_blocks:
            store.append_event("hook.stop.gave_up", {"session_id": sid, "blocks": n}, locked=False)
            return 0
        blocks[sid] = n + 1
    store.append_event("hook.stop.blocked", {"session_id": sid, "block": n + 1, "max": max_blocks})
    _out({"decision": "block", "reason": (
        f"longrun ({n + 1}/{max_blocks}): this builder session ends without any evidence or observation on the ledger. "
        f"Before ending: run the relevant checks and submit what you have with "
        f"`longrun evidence submit --criterion <id> --kind <check|test|screenshot|...> --summary '...' [--artifact <path>] [--cmd '<command you ran>' --exit <code>]`, "
        f"or, if you are blocked, `longrun observe --blocker '<what blocks and the proof>'`. Do not claim completion; the evaluator decides.")})
    return 0


def _session_start(store: RunStore, st: dict, tok: dict, payload: dict) -> int:
    from .contract import contract_summary
    cp = store.contract_path()
    if not cp.is_file():
        return 0
    c = json.loads(cp.read_text())
    role = tok.get("role")
    lines = [f"longrun run {store.run_id[:8]} — role: {role}. Contract hash {st.get('contract_hash', '')[:12]}, "
             f"revision {st.get('start_revision') or 'n/a'}.", contract_summary(c)]
    if role == "builder":
        lines.append("Rules: work only on the listed criteria or a demonstrated blocker. You cannot mark anything done; "
                     "submit candidate evidence with `longrun evidence submit` and let the controller-launched evaluator decide. "
                     "Never edit files under ~/.local/share/longrun. Run `longrun status` for current criterion status.")
    _out({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)[:6000]}})
    return 0


def _pre_tool_use(store: RunStore, st: dict, tok: dict, payload: dict) -> int:
    role = tok.get("role")
    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") or {}
    if role in ("evaluator", "restart_manager", "planner", "contract_repair"):
        cmd = str(inp.get("command", "")) if isinstance(inp, dict) else ""
        mutating = tool in WRITE_TOOLS or (tool == "Bash" and any(m in cmd for m in MUTATING_BASH))
        if mutating:
            store.append_event("evaluator.mutation_attempt", {"session_id": payload.get("session_id"), "tool": tool,
                                                              "input": json.dumps(inp)[:500], "role": role})
            _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": f"longrun: {role} is read-only; mutation attempt recorded."}})
            return 0
        return 0
    # builder: protect harness state and secrets regardless of allowlists
    if role == "builder":
        s = json.dumps(inp)
        from .paths import keys_root
        protected = [str(keys_root()), str(store.dir / "state.json"), str(store.dir / "state.sig"), str(store.dir / "events.jsonl"),
                     str(store.dir / "contract"), str(store.dir / "evidence"), str(store.dir / "evaluations"), str(store.dir / ".lock")]
        # the run workspace (a git worktree under the run dir) is the builder's legitimate editing area
        touches_protected = any(p in s for p in protected)
        if touches_protected and tool in WRITE_TOOLS | {"Bash"} and "longrun " not in str(inp.get("command", "")):
            store.append_event("builder.state_mutation_attempt", {"session_id": payload.get("session_id"), "tool": tool, "input": s[:500]})
            _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": "longrun: harness state is controller-owned; use `longrun evidence submit`."}})
    return 0


def _task_completed(store: RunStore, st: dict, tok: dict, payload: dict) -> int:
    """Deterministic criterion checks inside an authenticated builder session: run, record as evidence, and
    give feedback. Bounded by the hook timeout the controller set."""
    if tok.get("role") != "builder":
        return 0
    from .controller import run_deterministic_checks
    try:
        results = run_deterministic_checks(store, submitted_by=f"hook:{payload.get('session_id')}", max_total_seconds=240)
    except Exception as e:
        return 0
    failed = [r for r in results if not r["passed"]]
    if failed:
        summary = "; ".join(f"{r['criterion']}: `{r['cmd'][:60]}` exit {r['exit_code']}" for r in failed[:6])
        _out({"hookSpecificOutput": {"hookEventName": "TaskCompleted", "additionalContext":
              f"longrun deterministic checks failing: {summary}. Evidence recorded; the criterion stays FAIL until fixed."}})
    return 0
