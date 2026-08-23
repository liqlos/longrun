"""Two-layer loop detector.

Operational repetition is measured on the child session's tool-call stream (action/observation pairs,
error signatures, edit/revert alternation, repeated edits to one file without evidence delta, repeated
evaluator invocation with unchanged inputs).

Strategic stagnation is measured across checkpoints (criterion fingerprint delta, evaluator failure
signature repeats, repairs that do not move the same criterion, spend without fresh evidence,
plan rewriting without implementation evidence).
"""
from __future__ import annotations
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

ERROR_RE = re.compile(r"(Traceback|Error|error:|FAILED|failed|Exception|not found|No such file|panic:|"
                      r"undefined|cannot|denied)", re.I)
DOC_ONLY_RE = re.compile(r"\.(md|rst|txt|adoc)$", re.I)
MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "edit", "write"}
_MUT_RE = re.compile(r"(^|[;&|]\s*)(git\s+(commit|add|checkout|reset|rm|mv)|rm\s|mv\s|cp\s|sed\s+-i|tee\s|>\s*[^&]|>>|python3?\s+-c|longrun\s+evidence|Unity\b.*-executeMethod|make\b|npm\s+run|pytest|cargo\s+(build|test))")


def _looks_mutating(cmd: str) -> bool:
    return bool(_MUT_RE.search(cmd))


def _h(x: Any) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class OperationalReport:
    total_actions: int = 0
    repeated_action_observation_pairs: int = 0      # identical (tool, input, result-hash) seen >= 3 times
    repeated_error_signatures: int = 0              # same error signature >= 3 times
    alternating_edit_revert: int = 0                # A,B,A,B on the same file content hash
    same_file_edits_without_evidence: int = 0       # >= 4 edits to one file with no evidence submitted after
    fired: bool = False
    reasons: list[str] = field(default_factory=list)


def analyze_stream(actions: Iterable[dict], evidence_submissions_after: dict[str, int] | None = None,
                   thresholds: dict | None = None) -> OperationalReport:
    """`actions` are normalized {tool, input, result, is_error, file} dicts in order (see drivers)."""
    # authoring a long doc = many edits of one file; 12 without any evidence is the loop signal.
    # error_repeat is 8, not 5: the harness *tells* the builder to run a guard, fix, and run it again, and a
    # failing check usually prints the same line until the last fix lands. At 5 that normal cycle was a third
    # of all guard kills. Falsifier: if stagnation (loop.strategic) rises while operational kills fall, the
    # loop is simply arriving later and the threshold should go back down.
    th = {"pair_repeat": 4, "error_repeat": 8, "alt_len": 4, "same_file_edits": 12}
    th.update(thresholds or {})
    rep = OperationalReport()
    pairs = Counter(); errs = Counter(); file_hist: dict[str, list[str]] = {}; file_edit_counts = Counter()
    actions_list = list(actions)
    for a in actions_list:
        rep.total_actions += 1
        sig = _h([a.get("tool"), a.get("input")])
        res_h = _h(a.get("result"))
        pairs[(sig, res_h)] += 1
        if a.get("is_error") or (isinstance(a.get("result"), str) and ERROR_RE.search(a["result"] or "")):
            m = re.search(r"([A-Za-z_]*(Error|Exception|error:|FAILED|panic:)[^\n]{0,80})", str(a.get("result")))
            errs[m.group(1).strip() if m else res_h] += 1
        f = a.get("file")
        if f and a.get("tool") in ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "edit", "write"):
            file_edit_counts[f] += 1
            hist = file_hist.setdefault(f, [])
            hist.append(_h(a.get("input")))
    mutating_or_error = {}
    for a in actions_list:
        sig = _h([a.get("tool"), a.get("input")]); rh = _h(a.get("result"))
        is_mut = a.get("tool") in MUTATING_TOOLS or (a.get("tool") == "Bash" and _looks_mutating(str((a.get("input") or {}).get("command", ""))))
        if a.get("is_error") or is_mut:
            mutating_or_error[(sig, rh)] = True
    for (sig, rh), n in pairs.items():
        if n >= th["pair_repeat"] and mutating_or_error.get((sig, rh)):
            rep.repeated_action_observation_pairs += 1      # identical read-only polling (waiting on a build/log) is not a loop
    for e, n in errs.items():
        if n >= th["error_repeat"]:
            rep.repeated_error_signatures += 1
    for f, hist in file_hist.items():
        L = th["alt_len"]
        for i in range(len(hist) - L + 1):
            w = hist[i:i + L]
            if len(set(w)) == 2 and all(w[j] == w[j + 2] for j in range(L - 2)) and w[0] != w[1]:
                rep.alternating_edit_revert += 1
                break
    for f, n in file_edit_counts.items():
        if n >= th["same_file_edits"] and (evidence_submissions_after or {}).get(f, 0) == 0:
            rep.same_file_edits_without_evidence += 1
    if rep.repeated_action_observation_pairs:
        rep.reasons.append(f"{rep.repeated_action_observation_pairs} identical action/observation pair(s) repeated >= {th['pair_repeat']}x")
    if rep.repeated_error_signatures:
        rep.reasons.append(f"{rep.repeated_error_signatures} error signature(s) repeated >= {th['error_repeat']}x")
    if rep.alternating_edit_revert:
        rep.reasons.append("alternating edit/revert pattern on a file")
    if rep.same_file_edits_without_evidence:
        rep.reasons.append("repeated edits to the same file with no evidence submitted")
    rep.fired = bool(rep.reasons)
    return rep


@dataclass
class StrategicReport:
    fired: bool = False
    reasons: list[str] = field(default_factory=list)
    action: str = "continue"   # continue | changed_strategy_attempt | fresh_restart | stop


def strategic_check(state: dict, *, verdict: dict | None, delta: dict | None, round_summary: dict) -> StrategicReport:
    """Update state['loop'] counters in place and decide. `round_summary` carries
    {new_evidence: int, edited_files: [..], doc_only: bool, plan_rewrites: int, spend_usd: float, blocker_demonstrated: bool}."""
    loop = state.setdefault("loop", {})
    rep = StrategicReport()
    from .evaluator import criteria_fingerprint
    fp = criteria_fingerprint(state)
    moved = bool(delta and (delta.get("passed") or delta.get("regressed")))
    if moved:
        loop["no_delta_checkpoints"] = 0
        loop["failure_signatures"] = []       # meaningful progress resets stagnation counters
        loop["repairs_without_move"] = 0
    else:
        if loop.get("last_criteria_fingerprint") == fp and not round_summary.get("blocker_demonstrated"):
            loop["no_delta_checkpoints"] = loop.get("no_delta_checkpoints", 0) + 1
        else:
            loop["no_delta_checkpoints"] = max(loop.get("no_delta_checkpoints", 0), 1) if loop.get("last_criteria_fingerprint") is not None else 0
    loop["last_criteria_fingerprint"] = fp
    if verdict and verdict.get("failure_signature"):
        sigs = loop.setdefault("failure_signatures", [])
        sigs.append(verdict["failure_signature"].strip().lower()[:200])
        loop["failure_signatures"] = sigs[-6:]
    sigs = loop.get("failure_signatures", [])
    if len(sigs) >= 2 and sigs[-1] and sigs[-1] == sigs[-2] and not round_summary.get("changed_hypothesis"):
        rep.reasons.append("same evaluator failure signature twice without a materially different hypothesis")
    if loop.get("no_delta_checkpoints", 0) >= 2:
        rep.reasons.append("two consecutive checkpoints with no criterion delta and no newly demonstrated blocker")
    if round_summary.get("doc_only") and not moved:
        rep.reasons.append("docs/refactor/tooling-only work without a criterion or blocker link")
    if round_summary.get("new_evidence", 0) == 0 and (round_summary.get("spend_usd", 0) > 0 or round_summary.get("actions", 0) > 40):
        rep.reasons.append("high spend/tool use without fresh evidence")
    if round_summary.get("plan_rewrites", 0) >= 2 and round_summary.get("new_evidence", 0) == 0:
        rep.reasons.append("repeated plan rewriting without implementation evidence")
    if round_summary.get("is_repair") and not moved:
        loop["repairs_without_move"] = loop.get("repairs_without_move", 0) + 1
        if loop["repairs_without_move"] >= 2:
            rep.reasons.append("two repairs that did not move the same criterion")
    rep.fired = bool(rep.reasons)
    return rep


def build_failure_capsule(state: dict, contract: dict, *, attempts: list[dict], evaluator_findings: list[dict],
                          diff_summary: str, surviving_facts: list[str], rejected_hypotheses: list[str],
                          observations: list[str]) -> dict:
    """Compact capsule for a fresh restart. Never the full transcript."""
    def clip(s: str, n: int) -> str:
        return (s or "")[:n]
    return {
        "outcome": clip(contract["observable_end_state"], 800),
        "criteria_status": {k: v.get("status") for k, v in state.get("criteria", {}).items()},
        "attempts": [{"round": a.get("round"), "kind": a.get("kind"), "summary": clip(a.get("summary", ""), 400),
                      "result": clip(a.get("result", ""), 200)} for a in attempts[-6:]],
        "observations": [clip(o, 300) for o in observations[-10:]],
        "rejected_hypotheses": [clip(h, 300) for h in rejected_hypotheses[-8:]],
        "surviving_facts": [clip(f, 300) for f in surviving_facts[-12:]],
        "diff_summary": clip(diff_summary, 4000),
        "evaluator_findings": [{"criterion": f.get("id"), "verdict": f.get("verdict"), "reason": clip(f.get("reason", ""), 300)}
                               for f in evaluator_findings[-12:]],
        "budget_spent": {k: state.get("counters", {}).get(k) for k in ("rounds", "repairs", "fresh_restarts", "cost_usd", "wall_seconds", "evaluations")},
        "failure_signatures": state.get("loop", {}).get("failure_signatures", [])[-4:],
    }
