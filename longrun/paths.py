"""Filesystem layout. Everything lives under one user-level data root; runs are isolated by UUID."""
from __future__ import annotations
import os
import hashlib
from pathlib import Path


def data_root() -> Path:
    env = os.environ.get("LONGRUN_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "longrun"


def runs_root() -> Path:
    return data_root() / "runs"


def keys_root() -> Path:
    # run secrets live outside the run dir so a builder deny-rule can cover them precisely
    return data_root() / "keys"


def backups_root() -> Path:
    return data_root() / "backups"


def archive_root() -> Path:
    return data_root() / "archive"


def run_dir(run_id: str) -> Path:
    return runs_root() / run_id


def project_marker(project_root: Path) -> Path:
    """Per-project pointer file. Presence never activates anything; it only records the last run id."""
    return project_root / ".longrun" / "project.json"


def chain_stop_marker(project_root: Path) -> Path:
    """Private, per-project stop latch for a whole ``longrun go --chain`` invocation.

    It deliberately lives in the harness data directory: requesting a stop must not
    dirty a project's checkout, and the digest keeps unrelated projects separate.
    A new ``go`` invocation clears its own stale latch before it creates a run.
    """
    root_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()
    return data_root() / "chain-stops" / root_key


def chain_lock_path(project_root: Path) -> Path:
    """Kernel-backed singleton lock for one ``go`` controller per project."""
    root_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()
    return data_root() / "chain-locks" / root_key


def run_admission_lock_path(project_root: Path) -> Path:
    """Short-lived atomic admission lock for *every* way of creating a run.

    ``chain_lock_path`` protects the high-level ``go`` command.  ``plan`` and
    programmatic callers also create runs, so they need a separate lock around
    the state check and initial state write.
    """
    root_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()
    return data_root() / "run-admission-locks" / root_key


def ensure_dirs() -> None:
    for p in (runs_root(), keys_root(), backups_root(), archive_root()):
        p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(keys_root(), 0o700)
    except OSError:
        pass
