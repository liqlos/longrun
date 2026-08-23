"""Controller-owned evidence ledger.

Builder may *submit candidate evidence*. The controller binds it to run id, contract hash, revision,
timestamp, command, exit code and artifact hashes, and rejects stale / wrong-run / wrong-revision /
malformed / unsupported evidence. Evidence never changes criterion status by itself.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .contract import EVIDENCE_TYPES
from .store import RunStore, now_iso, sha256_file, sha256_bytes, canonical_json


class EvidenceError(ValueError):
    pass


def _rel_ok(p: Path, roots: list[Path]) -> bool:
    rp = p.resolve()
    for r in roots:
        try:
            rp.relative_to(r.resolve())
            return True
        except ValueError:
            continue
    return False


def record_evidence(store: RunStore, *, kind: str, criterion_ids: list[str], summary: str,
                    revision: str | None, submitted_by: str, command: str | None = None,
                    exit_code: int | None = None, stdout: str | None = None, stderr: str | None = None,
                    artifacts: list[str] | None = None, artifact_roots: list[Path] | None = None,
                    data: dict | None = None, expected_run_id: str | None = None,
                    expected_contract_hash: str | None = None, contract_hash: str | None = None,
                    current_revision: str | None = None, preassigned_id: str | None = None) -> dict:
    """Validate and append one evidence record. Returns the record. Raises EvidenceError on rejection."""
    if kind not in EVIDENCE_TYPES:
        raise EvidenceError(f"unsupported evidence type {kind!r}; allowed: {sorted(EVIDENCE_TYPES)}")
    if expected_run_id is not None and expected_run_id != store.run_id:
        raise EvidenceError(f"evidence run_id {expected_run_id} does not match run {store.run_id}")
    st = store.read()
    if st.get("status") in ("PASSED", "STOPPED", "INTERRUPTED", "RESET_RECOMMENDED", "FAILED"):
        raise EvidenceError(f"run is {st['status']}; evidence no longer accepted")
    chash = st.get("contract_hash")
    if chash is None:
        raise EvidenceError("contract not frozen; evidence not accepted before FROZEN")
    if expected_contract_hash is not None and expected_contract_hash != chash:
        raise EvidenceError("evidence bound to a different contract hash (stale contract)")
    if contract_hash is not None and contract_hash != chash:
        raise EvidenceError("evidence bound to a different contract hash (stale contract)")
    known = {c["id"] for c in _contract_criteria(store)}
    bad = [c for c in criterion_ids if c not in known]
    if bad:
        raise EvidenceError(f"unknown criterion ids {bad}")
    if not criterion_ids:
        raise EvidenceError("evidence must link to at least one criterion (or use kind=observation with criterion 'run')")
    if current_revision is not None and revision is not None and revision != current_revision:
        raise EvidenceError(f"stale revision {revision[:12]} != current {current_revision[:12]}")
    if not summary or len(summary.strip()) < 8:
        raise EvidenceError("summary required")
    eid = preassigned_id or f"E{uuid.uuid4().hex[:12]}"
    if not re.match(r"^E[0-9a-f]{12}$", eid):
        raise EvidenceError("bad evidence id")
    edir = store.evidence_dir / eid
    edir.mkdir(parents=True, exist_ok=False)
    arts = []
    roots = artifact_roots or [Path(st["project_root"])]
    if st.get("workspace"):
        roots.append(Path(st["workspace"]))
    for a in artifacts or []:
        p = Path(a)
        if not p.is_absolute():
            p = (Path(st.get("workspace") or st["project_root"]) / a)
        if not p.is_file():
            shutil.rmtree(edir, ignore_errors=True)
            raise EvidenceError(f"artifact not found: {a}")
        if not _rel_ok(p, roots + [store.dir]):
            shutil.rmtree(edir, ignore_errors=True)
            raise EvidenceError(f"artifact outside allowed roots: {a}")
        h = sha256_file(p)
        dest = edir / f"{h[:16]}_{p.name}"[:120]
        shutil.copy2(p, dest)
        arts.append({"path": str(p), "sha256": h, "size": p.stat().st_size, "copy": str(dest)})
    if stdout:
        (edir / "stdout.txt").write_text(stdout[-200_000:], encoding="utf-8")
    if stderr:
        (edir / "stderr.txt").write_text(stderr[-200_000:], encoding="utf-8")
    rec = {
        "id": eid, "run_id": store.run_id, "contract_hash": chash, "kind": kind,
        "criterion_ids": list(criterion_ids), "summary": summary.strip()[:2000], "revision": revision,
        "submitted_by": submitted_by, "recorded_at": now_iso(), "command": command, "exit_code": exit_code,
        "stdout_sha256": sha256_bytes(stdout.encode()) if stdout else None,
        "stdout_tail": (stdout or "")[-4000:] or None,
        "artifacts": arts, "data": data or {}, "verified": False, "verified_by": None,
    }
    (edir / "record.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    store.append_event("evidence.recorded", {"id": eid, "kind": kind, "criteria": criterion_ids,
                                             "revision": revision, "by": submitted_by})
    return rec


def _contract_criteria(store: RunStore) -> list[dict]:
    p = store.contract_path()
    if not p.is_file():
        return []
    return json.loads(p.read_text())["criteria"]


def list_evidence(store: RunStore) -> list[dict]:
    out = []
    if not store.evidence_dir.is_dir():
        return out
    for d in sorted(store.evidence_dir.iterdir()):
        r = d / "record.json"
        if r.is_file():
            try:
                out.append(json.loads(r.read_text()))
            except Exception:
                continue
    return sorted(out, key=lambda e: e["recorded_at"])


def evidence_manifest(store: RunStore, revision: str | None = None) -> list[dict]:
    """Compact manifest for the evaluator: only current-revision evidence bound to the current contract."""
    st = store.read()
    m = []
    for e in list_evidence(store):
        if e["contract_hash"] != st.get("contract_hash"):
            continue
        if revision is not None and e.get("revision") not in (None, revision):
            continue
        m.append({"id": e["id"], "kind": e["kind"], "criterion_ids": e["criterion_ids"], "summary": e["summary"],
                  "revision": e["revision"], "command": e["command"], "exit_code": e["exit_code"],
                  "submitted_by": e["submitted_by"], "artifacts": [{"path": a["path"], "sha256": a["sha256"]} for a in e["artifacts"]],
                  "stdout_tail": e.get("stdout_tail"), "record_dir": str(store.evidence_dir / e["id"])})
    return m


def manifest_hash(manifest: list[dict], diff_text: str, contract_hash: str, revision: str | None,
                  deterministic_results: list[dict] | None = None) -> str:
    # content-based: identical re-runs of the same checks / resubmission of identical evidence do not change the hash
    # Narrative and submitter metadata do not change the observed result and
    # must not buy another evaluator call. Criterion links remain because they
    # change which pass condition may legally cite the result.
    def result_identity(e: dict) -> dict | None:
        artifact_hashes = sorted({str(a.get("sha256")) for a in (e.get("artifacts") or []) if a.get("sha256")})
        base = {"kind": e.get("kind"), "criterion_ids": sorted(e.get("criterion_ids") or []),
                "revision": e.get("revision")}
        if artifact_hashes:
            # Once bytes are controller-hashed, narrative stdin/stdout, command
            # spelling, submitter and renamed paths cannot create a new result.
            return {**base, "artifact_hashes": artifact_hashes}
        # Unhashed prose/check narration is not a result and cannot buy an
        # evaluator call. Controller-owned deterministic checks are run and
        # compared separately; genuine external results must be artifact-hashed.
        return None

    identities = {canonical_json(identity) for e in manifest
                  if (identity := result_identity(e)) is not None}
    checks = [{k: row.get(k) for k in ("criterion", "cmd", "exit_code", "expected_exit", "passed",
                                               "expect_stdout_regex", "regex_matched", "timed_out")}
              for row in (deterministic_results or [])]
    return sha256_bytes(canonical_json({"m": sorted(identities), "checks": checks,
                                        "d": sha256_bytes(diff_text.encode()), "c": contract_hash, "r": revision}).encode())
