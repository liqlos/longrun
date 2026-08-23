"""Controller-owned run store.

Guarantees: file lock around every read-modify-write, atomic replace, monotonic event sequence
numbers, schema versioning, and stale-write rejection (optimistic version check).
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import SCHEMA_VERSION
from .paths import run_dir, keys_root, ensure_dirs

TERMINAL_STATES = {"PASSED", "PARTIAL_PASS", "RESET_RECOMMENDED", "OWNER_JUDGMENT_REQUIRED", "BLOCKED", "STOPPED",
                   "INTERRUPTED", "BUDGET_EXHAUSTED", "FAILED"}
ACTIVE_STATES = {"CREATED", "PLANNED", "FROZEN", "RUNNING", "EVALUATING", "REPAIRING", "RESTARTING"}


class StaleWrite(Exception):
    pass


class TamperDetected(Exception):
    pass


class StoreError(Exception):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any, mode: int | None = None) -> None:
    atomic_write(path, (json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(), mode)


class RunStore:
    """One store per run id. All authoritative mutation goes through `transaction()`."""

    def __init__(self, run_id: str):
        if not run_id or "/" in run_id or ".." in run_id:
            raise StoreError("bad run id")
        self.run_id = run_id
        self.dir = run_dir(run_id)
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock_path = self.dir / ".lock"
        self.evidence_dir = self.dir / "evidence"
        self.artifacts_dir = self.dir / "artifacts"
        self.tmp_dir = self.dir / "tmp"
        self.sessions_dir = self.dir / "sessions"

    # ------------------------------------------------------------------ creation
    @classmethod
    def create(cls, project_root: Path, adapter: str, start_revision: str | None, budgets: dict,
               parent_run_id: str | None = None, driver: str = "claude") -> "RunStore":
        ensure_dirs()
        rid = str(uuid.uuid4())
        st = cls(rid)
        st.dir.mkdir(parents=True, exist_ok=False)
        for d in (st.evidence_dir, st.artifacts_dir, st.tmp_dir, st.sessions_dir):
            d.mkdir()
        secret = os.urandom(32).hex()
        atomic_write(keys_root() / f"{rid}.key", secret.encode(), mode=0o600)
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": rid,
            "parent_run_id": parent_run_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "version": 0,
            "status": "CREATED",
            "project_root": str(project_root),
            "adapter": adapter,
            "driver": driver,
            "start_revision": start_revision,
            "workspace": None,
            "contract_hash": None,
            "contract_version": 0,
            "baseline": None,
            "budgets": budgets,
            "counters": {"rounds": 0, "repairs": 0, "fresh_restarts": 0, "evaluations": 0,
                         "child_sessions": 0, "cost_usd": 0.0, "wall_seconds": 0.0, "stop_blocks": {}},
            "criteria": {},          # id -> {status: FAIL|PASS|..., evidence_ids: [], last_verdict: {...}}
            "loop": {"failure_signatures": [], "no_delta_checkpoints": 0, "last_criteria_fingerprint": None,
                     "last_eval_input_hash": None},
            "children": [],          # {pid, pgid, role, session_id, started_at, ended_at, exit}
            "deadline_epoch": None,
            "terminal_reason": None,
            "failure_capsule": None,
        }
        st._write_state(state)
        st.events_path.touch()
        st.append_event("run.created", {"adapter": adapter, "project_root": str(project_root)}, locked=False)
        return st

    def exists(self) -> bool:
        return self.state_path.is_file()

    def secret(self) -> str | None:
        p = keys_root() / f"{self.run_id}.key"
        try:
            return p.read_text().strip()
        except OSError:
            return None

    # ------------------------------------------------------------------ locking / state
    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _sig(self, st: dict) -> str | None:
        sec = self.secret()
        if not sec:
            return None
        import hmac as _hmac
        return _hmac.new(sec.encode(), canonical_json(st).encode(), hashlib.sha256).hexdigest()

    def read(self, verify: bool = True) -> dict:
        with open(self.state_path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if st.get("schema_version") != SCHEMA_VERSION:
            st = migrate_state(st)
        if verify:
            sp = self.dir / "state.sig"
            if sp.is_file():
                want = sp.read_text().strip()
                got = self._sig(st)
                if got is not None and want != got:
                    raise TamperDetected(f"state.json signature mismatch for run {self.run_id}")
        return st

    def _write_state(self, st: dict) -> None:
        atomic_write_json(self.state_path, st)
        sig = self._sig(st)
        if sig:
            atomic_write(self.dir / "state.sig", sig.encode(), mode=0o600)

    @contextmanager
    def transaction(self, expected_version: int | None = None) -> Iterator[dict]:
        """Locked read-modify-write. The yielded dict is written back atomically with version+1.
        If `expected_version` is given and does not match, StaleWrite is raised before mutation."""
        with self._lock():
            st = self.read()
            if expected_version is not None and st["version"] != expected_version:
                raise StaleWrite(f"state version is {st['version']}, expected {expected_version}")
            before = canonical_json(st)
            yield st
            if canonical_json(st) != before:
                st["version"] += 1
                st["updated_at"] = now_iso()
                self._write_state(st)

    # ------------------------------------------------------------------ events
    def append_event(self, kind: str, data: dict | None = None, locked: bool = True) -> dict:
        def _do() -> dict:
            seq = self._last_seq() + 1
            ev = {"seq": seq, "ts": now_iso(), "run_id": self.run_id, "kind": kind, "data": data or {}}
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return ev
        if locked:
            with self._lock():
                return _do()
        return _do()

    def _last_seq(self) -> int:
        if not self.events_path.is_file():
            return 0
        last = 0
        with open(self.events_path, "rb") as fh:
            try:
                fh.seek(-4096, os.SEEK_END)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", "replace").strip().splitlines()
        for line in reversed(tail):
            try:
                last = int(json.loads(line)["seq"])
                break
            except Exception:
                continue
        return last

    def events(self) -> list[dict]:
        out = []
        if not self.events_path.is_file():
            return out
        with open(self.events_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    # ------------------------------------------------------------------ helpers
    def contract_path(self, version: int | None = None) -> Path:
        if version is None:
            return self.dir / "contract.json"
        return self.dir / f"contract.v{version}.json"

    def unique_artifact_path(self, stem: str, suffix: str) -> Path:
        self.artifacts_dir.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)[:60]
        return self.artifacts_dir / f"{safe}.{now_iso().replace(':', '').replace('.', '')}.{uuid.uuid4().hex[:8]}{suffix}"


def migrate_state(st: dict) -> dict:
    """Schema migrations. v0 (unversioned) -> v1 adds fields with safe defaults."""
    v = st.get("schema_version", 0)
    if v < 1:
        st.setdefault("counters", {})
        st.setdefault("loop", {"failure_signatures": [], "no_delta_checkpoints": 0,
                               "last_criteria_fingerprint": None, "last_eval_input_hash": None})
        st.setdefault("children", [])
        st["schema_version"] = 1
    return st


def find_active_runs(project_root: Path | None = None) -> list[dict]:
    from .paths import runs_root
    out = []
    root = runs_root()
    if not root.is_dir():
        return out
    for d in root.iterdir():
        sp = d / "state.json"
        if not sp.is_file():
            continue
        try:
            st = json.loads(sp.read_text())
        except Exception:
            continue
        # A controller PID can be reused by the OS long after a crashed run.  Do not let an
        # expired CREATED/active record become a permanent project lock merely because an
        # unrelated process later received the same PID.  The run's own wall budget is the
        # authoritative upper bound on how long it can be active.
        created = st.get("created_at")
        wall = (st.get("budgets") or {}).get("wall_time_seconds")
        expired = False
        if isinstance(created, str) and isinstance(wall, (int, float)):
            try:
                started = datetime.fromisoformat(created.replace("Z", "+00:00"))
                expired = datetime.now(timezone.utc).timestamp() >= started.timestamp() + float(wall)
            except (ValueError, TypeError, OverflowError):
                expired = False
        if st.get("status") in ACTIVE_STATES and not expired:
            if project_root is None or Path(st.get("project_root", "")).resolve() == project_root.resolve():
                out.append(st)
    return out
