"""Evaluator output validation and controller-applied criterion transitions.

The evaluator (a fresh, controller-launched, read-only session) returns exactly one JSON object.
There is no separate --verdict flag anywhere. The controller validates the object against the contract
and the evidence ledger, then applies criterion transitions itself.
"""
from __future__ import annotations
import json
import re
from typing import Any

CRIT_VERDICTS = {"PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "OWNER_JUDGMENT_REQUIRED"}
OVERALL = {"PASS", "NEEDS_REWORK", "RESET_RECOMMENDED", "OWNER_JUDGMENT_REQUIRED"}

EVALUATOR_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["run_id", "contract_hash", "evaluated_revision", "criteria", "overall", "failure_signature",
                 "recommended_next_strategy"],
    "properties": {
        "run_id": {"type": "string"},
        "contract_hash": {"type": "string"},
        "evaluated_revision": {"type": "string"},
        "criteria": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "verdict", "evidence_ids", "reason"],
            "properties": {"id": {"type": "string"},
                           "verdict": {"type": "string", "enum": sorted(CRIT_VERDICTS)},
                           "evidence_ids": {"type": "array", "items": {"type": "string"}},
                           "reason": {"type": "string"}}}},
        "overall": {"type": "string", "enum": sorted(OVERALL)},
        "failure_signature": {"type": "string"},
        "recommended_next_strategy": {"type": "string"},
    },
}


class EvaluatorError(ValueError):
    pass


def extract_json_object(text: str) -> dict:
    """Accept only a single top-level JSON object (optionally in a ```json fence). Anything else is rejected."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    if not (t.startswith("{") and t.endswith("}")):
        raise EvaluatorError("evaluator output is not a single JSON object")
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise EvaluatorError(f"evaluator output is not valid JSON: {e}")
    if not isinstance(obj, dict):
        raise EvaluatorError("evaluator output must be a JSON object")
    return obj


def validate_verdict(obj: dict, *, run_id: str, contract_hash: str, evaluated_revision: str | None,
                     contract: dict, evidence_manifest: list[dict],
                     baseline_green_commands: set[str] | None = None) -> dict:
    """Return a normalized verdict or raise EvaluatorError. Strict: no unknown keys, ids, or overrides.

    `baseline_green_commands` are the deterministic checks that already exited 0 at the frozen revision, before
    any edit. Measured over one night's thirteen runs, 39 of 63 criterion checks (61%) were already green at
    baseline, and two runs began with every check passing — so a criterion resting on such a check alone can
    PASS without the run having changed anything."""
    allowed_keys = set(EVALUATOR_JSON_SCHEMA["properties"])
    extra = set(obj) - allowed_keys
    if extra:
        raise EvaluatorError(f"unsupported keys in evaluator output: {sorted(extra)} (free-form overrides are rejected)")
    missing = set(EVALUATOR_JSON_SCHEMA["required"]) - set(obj)
    if missing:
        raise EvaluatorError(f"missing keys: {sorted(missing)}")
    if obj["run_id"] != run_id:
        raise EvaluatorError(f"run_id mismatch: {obj['run_id']} != {run_id}")
    if obj["contract_hash"] != contract_hash:
        raise EvaluatorError("contract_hash mismatch")
    if evaluated_revision is not None and obj["evaluated_revision"] != evaluated_revision:
        raise EvaluatorError(f"evaluated_revision mismatch: {obj['evaluated_revision'][:12]} != {evaluated_revision[:12]}")
    if obj["overall"] not in OVERALL:
        raise EvaluatorError(f"bad overall {obj['overall']!r}")
    known = {c["id"]: c for c in contract["criteria"]}
    seen = set()
    ev_ids = {e["id"] for e in evidence_manifest}
    ev_by_id = {e["id"]: e for e in evidence_manifest}
    out_crit = []
    if not isinstance(obj["criteria"], list):
        raise EvaluatorError("criteria must be a list")
    for c in obj["criteria"]:
        if not isinstance(c, dict) or set(c) - {"id", "verdict", "evidence_ids", "reason"}:
            raise EvaluatorError(f"bad criterion entry: {c}")
        cid = c.get("id")
        if cid not in known:
            raise EvaluatorError(f"unknown criterion id {cid!r}")
        if cid in seen:
            raise EvaluatorError(f"duplicate criterion id {cid!r}")
        seen.add(cid)
        v = c.get("verdict")
        if v not in CRIT_VERDICTS:
            raise EvaluatorError(f"bad verdict {v!r} for {cid}")
        eids = c.get("evidence_ids") or []
        if not isinstance(eids, list) or not all(isinstance(x, str) for x in eids):
            raise EvaluatorError(f"evidence_ids must be a list of strings for {cid}")
        bad = [e for e in eids if e not in ev_ids]
        if bad:
            raise EvaluatorError(f"criterion {cid} cites evidence not in the current manifest: {bad}")
        if v == "PASS":
            if not eids:
                raise EvaluatorError(f"criterion {cid}: PASS without evidence ids")
            spec = known[cid]
            # every cited evidence must actually be linked to this criterion — no aggregate closing
            unrelated = [e for e in eids if cid not in ev_by_id[e]["criterion_ids"]]
            if unrelated:
                raise EvaluatorError(f"criterion {cid}: cited evidence {unrelated} is not bound to this criterion "
                                     f"(one aggregate verdict cannot close unrelated criteria)")
            kinds = {ev_by_id[e]["kind"] for e in eids}
            req = set(spec["evidence_requirements"])
            if not (kinds & req):
                raise EvaluatorError(f"criterion {cid}: PASS requires evidence of kind {sorted(req)}, cited kinds {sorted(kinds)}")
            if spec["evaluator_policy"] == "owner_judgment":
                raise EvaluatorError(f"criterion {cid}: owner_judgment criteria cannot be PASSed by the evaluator")
            stale = {e for e in eids if (ev_by_id[e].get("command") or "") in (baseline_green_commands or set())}
            if stale and stale == set(eids):
                raise EvaluatorError(
                    f"criterion {cid}: every cited record ({sorted(stale)}) is a check that already passed at the "
                    f"frozen baseline, before any edit — that shows the project's prior state, not this run's work. "
                    f"Cite a fresh artifact as well, or the honest verdict is INSUFFICIENT_EVIDENCE.")
        if not isinstance(c.get("reason"), str) or len(c["reason"].strip()) < 5:
            raise EvaluatorError(f"criterion {cid}: reason required")
        out_crit.append({"id": cid, "verdict": v, "evidence_ids": eids, "reason": c["reason"].strip()[:2000]})
    if len(seen) != len(known):
        raise EvaluatorError(f"evaluator must return a verdict for every criterion; missing {sorted(set(known) - seen)}")
    # consistency: overall PASS requires all criteria PASS
    if obj["overall"] == "PASS" and any(c["verdict"] != "PASS" for c in out_crit):
        raise EvaluatorError("overall PASS while some criteria are not PASS")
    if obj["overall"] != "PASS" and all(c["verdict"] == "PASS" for c in out_crit):
        obj["overall"] = "PASS"
    return {"run_id": run_id, "contract_hash": contract_hash, "evaluated_revision": obj["evaluated_revision"],
            "criteria": out_crit, "overall": obj["overall"],
            "failure_signature": str(obj.get("failure_signature") or "")[:300],
            "recommended_next_strategy": str(obj.get("recommended_next_strategy") or "")[:2000]}


def apply_transitions(state: dict, verdict: dict, *, evaluation_id: str) -> dict:
    """Controller applies criterion transitions. Returns a delta summary. Never called by builder code paths."""
    delta = {"passed": [], "failed": [], "insufficient": [], "owner": [], "regressed": []}
    crit = state.setdefault("criteria", {})
    for c in verdict["criteria"]:
        prev = crit.get(c["id"], {}).get("status", "FAIL")
        rec = crit.setdefault(c["id"], {"status": "FAIL", "evidence_ids": [], "history": []})
        rec["history"].append({"evaluation_id": evaluation_id, "verdict": c["verdict"],
                               "revision": verdict["evaluated_revision"], "evidence_ids": c["evidence_ids"]})
        rec["history"] = rec["history"][-20:]
        if c["verdict"] == "PASS":
            rec["status"] = "PASS"; rec["evidence_ids"] = c["evidence_ids"]; rec["passed_at_revision"] = verdict["evaluated_revision"]
            delta["passed"].append(c["id"])
        else:
            rec["status"] = "FAIL" if c["verdict"] == "FAIL" else c["verdict"]
            if prev == "PASS":
                delta["regressed"].append(c["id"])
            {"FAIL": delta["failed"], "INSUFFICIENT_EVIDENCE": delta["insufficient"],
             "OWNER_JUDGMENT_REQUIRED": delta["owner"]}[c["verdict"]].append(c["id"])
        rec["last_reason"] = c["reason"]
    return delta


def criteria_fingerprint(state: dict) -> str:
    return "|".join(f"{k}:{v.get('status')}" for k, v in sorted(state.get("criteria", {}).items()))
