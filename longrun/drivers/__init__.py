"""Backend drivers. Each driver builds a controller-launched, token-carrying, bounded child session command
and normalizes its event stream into {tool, input, result, is_error, file} actions plus a final summary."""
from __future__ import annotations
from .claude import ClaudeDriver
from .codex import CodexDriver
from .opencode import OpenCodeDriver

DRIVERS = {"claude": ClaudeDriver, "codex": CodexDriver, "opencode": OpenCodeDriver}


def get_driver(name: str):
    if name not in DRIVERS:
        raise ValueError(f"unknown driver {name!r}; choose from {sorted(DRIVERS)}")
    return DRIVERS[name]()
