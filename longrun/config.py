"""Global owner config: ~/.local/share/longrun/config.json. Owner-set defaults so prompts need no flags."""
from __future__ import annotations
import json
from pathlib import Path
from .paths import data_root

DEFAULTS = {
    "default_permission_mode": "bypassPermissions",   # owner decision for this host (evaluator/restart stay read-only)
    "default_isolation": "auto",                      # auto = worktree when the repo is clean, in-place when it is dirty
    "chain_runs_default": 3,                          # `longrun go --chain`: max consecutive outcomes per invocation
    # Free-form owner authority injected into planner/builder prompts.  This is
    # deliberately owner-set (not inferred by a model) because permissions to
    # recreate paid infrastructure or use external services must survive fresh
    # autonomous sessions without being guessed from a stale worklog.
    "owner_policy": "",
}


def config_path() -> Path:
    return data_root() / "config.json"


def load() -> dict:
    c = dict(DEFAULTS)
    p = config_path()
    if p.is_file():
        try:
            c.update({k: v for k, v in json.loads(p.read_text()).items() if k in DEFAULTS})
        except Exception:
            pass
    return c


def write_defaults() -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file():
        p.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    return p
