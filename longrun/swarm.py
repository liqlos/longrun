"""Prompt-driven OpenCode background-swarm guardrails.

The builder remains the orchestrator.  This module never assigns work or starts
subagents; it only describes the configured contract and audits the task events
that OpenCode emitted before the parent session stopped.
"""
from __future__ import annotations

import re


DONE_MARKER = "LONGRUN_SWARM_DONE"
BLOCKED_MARKER = "LONGRUN_SWARM_BLOCKED"


def config_from_contract(contract: dict) -> dict:
    raw = ((contract.get("adapter_config") or {}).get("builder_swarm") or {})
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return {}
    return {
        "enabled": True,
        # Hard cost ceiling: a bad contract must not be able to demand an
        # unbounded fanout. The audit target always matches the prompt target.
        "researchers": min(32, max(1, int(raw.get("researchers", 12)))),
        "workers": min(16, max(1, int(raw.get("workers", 5)))),
        "task_retries": max(0, int(raw.get("task_retries", 3))),
        "manager_retries": max(0, int(raw.get("manager_retries", 3))),
    }


def prompt_fragment(cfg: dict) -> str:
    if not cfg:
        return ""
    researchers = cfg["researchers"]
    workers = cfg["workers"]
    retries = cfg["task_retries"]
    return f"""[longrun-swarm target={researchers} writers={workers} background=true]
You are the swarm manager and integrator; Longrun does not decompose or assign the work for you.
- First choose {researchers} genuinely independent research/review questions that reduce risk for this exact contract. Emit R01..R{researchers:02d} as separate `swarm-researcher` task calls with `background=true`, dispatching every missing call immediately across consecutive manager turns instead of waiting for early results. Every task call must include `description`, `prompt`, and `subagent_type`; put its Rxx id in both description and prompt. Decide the roles and questions yourself.
- Wait for all background research completion notifications and synthesize them yourself. Repetition is not consensus: discard duplicate or irrelevant findings. A launch receipt is not a completion.
- Then choose {workers} non-overlapping implementation shards and emit W01..W{workers:02d} as separate `swarm-worker` task calls with `background=true`, again filling every slot before waiting. Put the Wxx id and an explicit exclusive file/output ownership set in each prompt. If five safe edit shards do not exist, use the remaining worker slots for read-only patch proposals; never let two agents edit the same shared file.
- The parent alone edits shared Unity scenes, shared configs, maintained docs, and Longrun evidence; it integrates and verifies the whole result. Subagents may not spawn another swarm.
- A failed child may be retried at most {retries} times, preferably with its existing `task_id`. Before retrying, inspect the workspace and completed reports so the shard is idempotent and already-landed work is not duplicated.
- Longrun owns a persistent OpenCode server, so background tasks survive manager-turn EOF and same-session transport recovery. After recovery, inspect the recorded task ids and durable workspace state; resume or collect an existing task before respawning its shard. Respawn only when that task is failed or unreachable, and never duplicate already-landed work.
- End only after integration, required checks, and evidence submission. Make the final line exactly `{DONE_MARKER}`. For a proven irreducible blocker, record it through `longrun observe --blocker`, ensure no child remains active, and make the final line exactly `{BLOCKED_MARKER}`. A normal stop without one of these handoff markers is treated as an Ox interruption and automatically continued."""


def _shard_id(action: dict) -> str | None:
    inp = action.get("input") or {}
    text = " ".join(str(inp.get(k) or "") for k in ("description", "prompt", "command"))
    match = re.search(r"\b([RW]\d{2})\b", text, re.I)
    return match.group(1).upper() if match else None


def analyze(actions: list[dict], cfg: dict, final_text: str = "") -> dict:
    launched: dict[str, dict] = {}
    malformed: list[str] = []
    task_errors: list[str] = []
    for action in actions:
        if str(action.get("tool") or "").lower() != "task":
            continue
        shard = _shard_id(action)
        inp = action.get("input") or {}
        if not shard:
            malformed.append(str(inp.get("description") or inp.get("command") or "unnamed task")[:120])
            continue
        if action.get("is_error"):
            task_errors.append(shard)
            continue
        # A successful background task tool event proves dispatch, not child
        # completion.  Completion is acknowledged only by the manager marker.
        launched[shard] = {
            "background": bool(inp.get("background")),
            "subagent_type": inp.get("subagent_type"),
            "child_session_id": action.get("child_session_id"),
        }
    expected_r = {f"R{i:02d}" for i in range(1, int(cfg.get("researchers", 0)) + 1)}
    expected_w = {f"W{i:02d}" for i in range(1, int(cfg.get("workers", 0)) + 1)}
    launched_ids = set(launched)
    wrong_mode = sorted(k for k, v in launched.items() if not v["background"])
    wrong_type = sorted(
        k for k, v in launched.items()
        if v["subagent_type"] != ("swarm-researcher" if k.startswith("R") else "swarm-worker")
    )
    return {
        "launched": sorted(launched_ids),
        "task_ids": {k: v["child_session_id"] for k, v in sorted(launched.items())
                     if v.get("child_session_id")},
        "missing_researchers": sorted(expected_r - launched_ids),
        "missing_workers": sorted(expected_w - launched_ids),
        "wrong_mode": wrong_mode,
        "wrong_type": wrong_type,
        "malformed": malformed,
        "task_errors": sorted(set(task_errors) - launched_ids),
        "done_marker": DONE_MARKER in (final_text or ""),
        "blocked_marker": BLOCKED_MARKER in (final_text or ""),
    }


def recovery_reason(report: dict, summary: dict) -> str | None:
    if report.get("blocked_marker"):
        return None
    defects = (
        report.get("missing_researchers") or report.get("missing_workers") or
        report.get("wrong_mode") or report.get("wrong_type") or
        report.get("malformed") or report.get("task_errors")
    )
    if report.get("done_marker") and not defects:
        return None
    if summary.get("terminal"):
        return "swarm_underfilled" if defects else "clean_stop_without_swarm_handoff"
    return None


def research_dispatch_stalled(actions: list[dict], cfg: dict, *, manager_turns: int,
                              last_research_turn: int, grace_turns: int = 6) -> bool:
    """Return true when the manager is working past an underfilled first wave.

    This is deliberately not a scheduler: it does not choose work or require the
    writer wave while researchers may still be returning.  It only enforces the
    prompt's explicit "launch the research wave first" boundary.
    """
    if not cfg or manager_turns < grace_turns:
        return False
    report = analyze(actions, cfg)
    if not report["missing_researchers"]:
        return False
    return manager_turns - last_research_turn >= grace_turns


def corrective_note(report: dict) -> str:
    fields = []
    for key in ("missing_researchers", "missing_workers", "wrong_mode", "wrong_type", "task_errors"):
        if report.get(key):
            fields.append(f"{key}={','.join(report[key])}")
    if report.get("malformed"):
        fields.append(f"malformed={len(report['malformed'])}")
    if report.get("task_ids"):
        fields.append("task_ids=" + ",".join(f"{k}:{v}" for k, v in report["task_ids"].items()))
    detail = "; ".join(fields) if fields else "the final handoff marker was absent"
    return (
        "The parent OpenCode session stopped before the prompt-driven swarm contract was safely handed off: "
        f"{detail}. Continue as the same manager. Do not restart completed work. Inspect the current diff, child "
        "reports, and evidence. The run-scoped OpenCode server preserves background jobs across manager transport "
        "EOF: collect or resume the listed task ids first. Respawn only a failed or unreachable incomplete shard; "
        "integrate, verify, submit evidence, and end with the required exact marker."
    )
