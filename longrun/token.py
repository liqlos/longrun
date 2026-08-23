"""Controller-issued capability tokens.

A token binds (run_id, session_id, role, controller pid, expiry, nonce) with an HMAC over the run secret.
Only the controller (which reads the run key) can mint one. A session cannot activate harness behaviour by
setting an environment variable itself: without a valid signature the hook and CLI fail open / refuse.
Verification is designed to be cheap and to fail open on any malformation.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

ROLES = {"builder", "evaluator", "planner", "contract_repair", "intent_reviewer", "restart_manager", "controller"}
ENV_TOKEN = "LONGRUN_TOKEN"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(secret: str, *, run_id: str, session_id: str, role: str, controller_pid: int, ttl_seconds: int,
         contract_hash: str | None = None) -> str:
    if role not in ROLES:
        raise ValueError("bad role")
    payload = {"run_id": run_id, "session_id": session_id, "role": role, "cpid": int(controller_pid),
               "exp": int(time.time()) + int(ttl_seconds), "nonce": os.urandom(8).hex(),
               "chash": (contract_hash or "")[:16]}
    body = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"lr1.{body}.{_b64e(sig)}"


def parse_unverified(token: str) -> Optional[dict]:
    """Return the payload without verifying. Used only to find which run key to load."""
    try:
        v, body, _ = token.split(".")
        if v != "lr1":
            return None
        p = json.loads(_b64d(body))
        if not isinstance(p, dict) or not isinstance(p.get("run_id"), str):
            return None
        if "/" in p["run_id"] or ".." in p["run_id"] or len(p["run_id"]) > 64:
            return None
        return p
    except Exception:
        return None


def verify(token: str, secret: str, *, expect_session_id: str | None = None, expect_role: str | None = None,
           now: float | None = None) -> Optional[dict]:
    """Return payload if valid; None otherwise. Constant-time signature comparison."""
    try:
        v, body, sig = token.split(".")
        if v != "lr1":
            return None
        good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(good, _b64d(sig)):
            return None
        p = json.loads(_b64d(body))
        if int(p["exp"]) < (now if now is not None else time.time()):
            return None
        if expect_session_id is not None and p.get("session_id") != expect_session_id:
            return None
        if expect_role is not None and p.get("role") != expect_role:
            return None
        if p.get("role") not in ROLES:
            return None
        return p
    except Exception:
        return None


def controller_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
