"""MacroOutcomeContract: one immutable macro outcome per run, frozen and hashed before editable execution.

The observable outcome, criteria, constraints and non-goals may only change through a controller-recorded
REBASE event that writes a new contract version. The internal plan is not part of the contract.
"""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from typing import Any

from .store import canonical_json, sha256_bytes

CRITERION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
EVIDENCE_TYPES = {"check", "test", "build", "log", "screenshot", "video", "capture_manifest", "diff", "doc",
                  "http", "metric", "artifact", "owner_note", "observation"}
USER_FACING_KINDS = {"user_facing", "player_facing", "ui", "visual"}
# evidence types that alone can never satisfy a user-facing criterion
NON_UI_EVIDENCE = {"test", "check", "build", "log", "diff", "doc", "observation"}
EVALUATOR_POLICIES = {"llm_required", "deterministic_only", "owner_judgment"}
COMPLETION_STATES = ["PASSED", "NEEDS_REWORK", "RESET_RECOMMENDED", "OWNER_JUDGMENT_REQUIRED", "BLOCKED"]

DEFAULT_BUDGETS = {
    "wall_time_seconds": 2 * 3600,
    "child_timeout_seconds": 30 * 60,
    "evaluator_timeout_seconds": 15 * 60,
    "max_rounds": 4,
    "max_cost_without_delta_usd": 15.0,   # stop when this much is spent without any criterion moving
    "max_repairs": 1,
    "max_fresh_restarts": 1,
    "max_stop_blocks_per_session": 2,
    "max_turns_per_session": 60,
    # Wall-clock progress cannot be observed safely while some drivers hold a
    # foreground tool call. Model-controlled churn is bounded by turns; the
    # foreground operation remains bounded by child_timeout_seconds.
    "first_progress_deadline_seconds": 0,
    "max_turns_without_progress": 24,
    "max_cost_usd": None,
}


class ContractError(ValueError):
    pass


def new_contract(*, run_id: str, project_root: str, adapter: str, observable_end_state: str,
                 criteria: list[dict], constraints: list[str] | None = None, non_goals: list[str] | None = None,
                 allowed_replace_remove: list[str] | None = None, proven_blockers: list[str] | None = None,
                 budgets: dict | None = None, owner_judgment_policy: str = "stop_and_ask",
                 start_revision: str | None = None, outcome_id: str | None = None,
                 allowed_commands: list[str] | None = None, workspace_paths: list[str] | None = None,
                 adapter_config: dict | None = None, batch: dict | None = None) -> dict:
    b = dict(DEFAULT_BUDGETS)
    b.update(budgets or {})
    c = {
        "schema_version": 2,
        "contract_version": 1,
        "run_id": run_id,
        "outcome_id": outcome_id or "O1",
        "project_root": project_root,
        "start_revision": start_revision,
        "adapter": adapter,
        "adapter_config": adapter_config or {},
        "observable_end_state": observable_end_state.strip(),
        "batch": dict(batch or {}),
        "baseline": {"revision": None, "evidence": [], "frozen_at": None},
        "criteria": [normalize_criterion(x) for x in criteria],
        "constraints": list(constraints or []),
        "non_goals": list(non_goals or []),
        "allowed_replace_remove": list(allowed_replace_remove or []),
        "proven_blockers": list(proven_blockers or []),
        "allowed_commands": list(allowed_commands or []),
        "workspace_paths": list(workspace_paths or []),
        "budgets": b,
        "owner_judgment_policy": owner_judgment_policy,
        "completion_states": COMPLETION_STATES,
        "frozen": False,
        "rebase_history": [],
    }
    validate_contract(c)
    return c


def normalize_criterion(x: dict) -> dict:
    return {
        "id": str(x.get("id", "")).strip(),
        "statement": str(x.get("statement", "")).strip(),
        "kind": x.get("kind", "functional"),
        "evidence_requirements": list(x.get("evidence_requirements") or ["check"]),
        "deterministic_checks": [normalize_check(c) for c in (x.get("deterministic_checks") or [])],
        "evaluator_policy": x.get("evaluator_policy", "llm_required"),
        "initial_status": "FAIL",
    }


def normalize_check(c: dict | str) -> dict:
    if isinstance(c, str):
        c = {"cmd": c}
    return {"id": c.get("id") or sha256_bytes(c["cmd"].encode())[:10],
            "cmd": c["cmd"], "cwd": c.get("cwd"), "expect_exit": int(c.get("expect_exit", 0)),
            "expect_stdout_regex": c.get("expect_stdout_regex"),
            "timeout_seconds": int(c.get("timeout_seconds", 600))}


def validate_contract(c: dict) -> None:
    if not c.get("observable_end_state") or len(c["observable_end_state"]) < 20:
        raise ContractError("observable_end_state must be a concrete, externally checkable end state (>=20 chars)")
    batch = c.get("batch") or {}
    if int(c.get("schema_version", 1)) >= 2 and not batch:
        raise ContractError("batch is required for schema_version 2 contracts")
    if batch:
        required = {"boundary", "reality_test", "estimated_seconds", "max_foreground_seconds", "deferred_required_outcomes"}
        missing = sorted(required - set(batch))
        if missing:
            raise ContractError(f"batch is missing required fields: {missing}")
        if len(str(batch.get("boundary") or "").strip()) < 12:
            raise ContractError("batch.boundary must name one concrete production-stage boundary")
        if len(str(batch.get("reality_test") or "").strip()) < 12:
            raise ContractError("batch.reality_test must name one concrete reality test")
        estimate = int(batch.get("estimated_seconds") or 0)
        if estimate < 300 or estimate > 7200:
            raise ContractError("batch.estimated_seconds must be between 300 and 7200")
        foreground = int(batch.get("max_foreground_seconds") or 0)
        if foreground < 30 or foreground > 7200:
            raise ContractError("batch.max_foreground_seconds must be between 30 and 7200")
        if not isinstance(batch.get("deferred_required_outcomes"), list):
            raise ContractError("batch.deferred_required_outcomes must be a list")
    crits = c.get("criteria") or []
    if not crits:
        raise ContractError("at least one criterion is required")
    if int(c.get("schema_version", 1)) >= 2 and len(crits) > 4:
        raise ContractError("schema_version 2 contracts allow at most four criteria for one batch")
    ids = [x["id"] for x in crits]
    if len(set(ids)) != len(ids):
        raise ContractError(f"duplicate criterion ids: {ids}")
    for x in crits:
        if not CRITERION_ID_RE.match(x["id"]):
            raise ContractError(f"bad criterion id {x['id']!r}")
        if len(x["statement"]) < 10:
            raise ContractError(f"criterion {x['id']} statement too short")
        if x["evaluator_policy"] not in EVALUATOR_POLICIES:
            raise ContractError(f"criterion {x['id']}: evaluator_policy must be one of {sorted(EVALUATOR_POLICIES)}")
        bad = set(x["evidence_requirements"]) - EVIDENCE_TYPES
        if bad:
            raise ContractError(f"criterion {x['id']}: unknown evidence types {sorted(bad)}")
        if x["kind"] in USER_FACING_KINDS and not (set(x["evidence_requirements"]) - NON_UI_EVIDENCE):
            raise ContractError(f"criterion {x['id']} is {x['kind']} but requires only non-UI evidence "
                                f"({x['evidence_requirements']}); add screenshot/video/capture_manifest/http/metric/artifact/owner_note")
        if x["evaluator_policy"] == "deterministic_only" and not x["deterministic_checks"]:
            raise ContractError(f"criterion {x['id']}: deterministic_only requires deterministic_checks")
    # Deliberately NOT enforced here: a cap on documentation criteria. Four of nine contracts in one night carried
    # one alongside the outcome, and the diary became a second, softer deliverable — but how many documents a
    # contract may demand is a project's policy, not a structural invariant of contracts, and this validator is
    # shared by every project. The planner is told the rule instead, where breaking it costs guidance rather than
    # a hard rejection and a retry.
    b = c.get("budgets", {})
    for k in ("wall_time_seconds", "child_timeout_seconds", "max_rounds", "max_repairs", "max_fresh_restarts"):
        if b.get(k) is None or int(b[k]) < 0:
            raise ContractError(f"budgets.{k} must be a non-negative integer")
    for k in ("first_progress_deadline_seconds", "max_turns_without_progress"):
        if k in b and int(b[k]) < 0:
            raise ContractError(f"budgets.{k} must be a non-negative integer")
    if int(b["max_repairs"]) > 2:
        raise ContractError("budgets.max_repairs may not exceed 2")
    if int(b["max_fresh_restarts"]) > 1:
        raise ContractError("budgets.max_fresh_restarts may not exceed 1")
    if int(b["child_timeout_seconds"]) > int(b["wall_time_seconds"]):
        raise ContractError("child_timeout_seconds must not exceed wall_time_seconds")
    if batch and int(batch["max_foreground_seconds"]) > int(b["child_timeout_seconds"]):
        raise ContractError("batch.max_foreground_seconds must not exceed child_timeout_seconds")


def contract_hash(c: dict) -> str:
    """Hash of the immutable part (excludes the frozen flag and rebase history metadata)."""
    core = {k: v for k, v in c.items() if k not in ("frozen", "rebase_history")}
    return sha256_bytes(canonical_json(core).encode())


def freeze(c: dict, baseline_revision: str | None, baseline_evidence_ids: list[str], frozen_at: str) -> dict:
    if c.get("frozen"):
        raise ContractError("contract already frozen")
    c = copy.deepcopy(c)
    c["baseline"] = {"revision": baseline_revision, "evidence": list(baseline_evidence_ids), "frozen_at": frozen_at}
    validate_contract(c)
    c["frozen"] = True
    return c


def rebase(c: dict, changes: dict, reason: str, at: str) -> dict:
    """Return a new contract version. Only allowed keys may change; everything is recorded."""
    allowed = {"observable_end_state", "criteria", "constraints", "non_goals", "allowed_replace_remove",
               "proven_blockers"}
    bad = set(changes) - allowed
    if bad:
        raise ContractError(f"REBASE may not change {sorted(bad)}")
    if not reason or len(reason.strip()) < 20:
        raise ContractError("REBASE needs a substantive reason (>=20 chars)")
    n = copy.deepcopy(c)
    old_hash = contract_hash(c)
    for k, v in changes.items():
        n[k] = [normalize_criterion(x) for x in v] if k == "criteria" else v
    n["contract_version"] = int(c.get("contract_version", 1)) + 1
    n["rebase_history"] = list(c.get("rebase_history", [])) + [
        {"from_version": c.get("contract_version", 1), "from_hash": old_hash, "reason": reason.strip(), "at": at,
         "changed_keys": sorted(changes)}]
    validate_contract(n)
    return n


def load_contract(p: Path) -> dict:
    c = json.loads(p.read_text(encoding="utf-8"))
    validate_contract(c)
    return c


def contract_summary(c: dict) -> str:
    L = [f"Outcome {c['outcome_id']} (v{c['contract_version']}, adapter={c['adapter']}):", f"  {c['observable_end_state']}",
         ]
    batch = c.get("batch") or {}
    if batch:
        L.extend([
            f"Batch boundary: {batch['boundary']}",
            f"Reality test: {batch['reality_test']}",
            f"Batch estimate: {batch['estimated_seconds']}s; maximum one foreground operation: {batch['max_foreground_seconds']}s",
            "Deferred required outcomes: " + ("; ".join(batch.get("deferred_required_outcomes") or []) or "none"),
        ])
    L.append("Criteria (all start FAIL):")
    for x in c["criteria"]:
        L.append(f"  - {x['id']} [{x['kind']}/{x['evaluator_policy']}] {x['statement']}  evidence: {','.join(x['evidence_requirements'])}"
                 + (f"  checks: {len(x['deterministic_checks'])}" if x["deterministic_checks"] else ""))
    if c["constraints"]:
        L.append("Constraints: " + "; ".join(c["constraints"]))
    if c["non_goals"]:
        L.append("Non-goals: " + "; ".join(c["non_goals"]))
    b = c["budgets"]
    L.append(f"Budgets: wall {b['wall_time_seconds']}s, child {b['child_timeout_seconds']}s, rounds {b['max_rounds']}, "
             f"repairs {b['max_repairs']}, fresh restarts {b['max_fresh_restarts']}")
    return "\n".join(L)
