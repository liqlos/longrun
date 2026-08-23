"""Claude Code driver: controller-launched `claude -p` sessions.

Builder:  narrow allowlist (no bypassPermissions), acceptEdits, session-scoped hooks passed via --settings,
          stream-json parsed for the loop detector.
Evaluator: read-only tool set, dontAsk permission mode, session-scoped PreToolUse hook that denies and records
          any mutation attempt, --json-schema forcing exactly one JSON object.
"""
from __future__ import annotations
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

WRITE_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
LONGRUN_BIN = shutil.which("longrun") or str(Path.home() / ".local/bin/longrun")


def _hook(cmd_event: str, timeout: int = 20) -> dict:
    return {"type": "command", "command": f"{LONGRUN_BIN} hook {cmd_event}", "timeout": timeout}


class ClaudeDriver:
    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def build_command(self, *, role: str, prompt: str, session_id: str, cwd: Path, max_turns: int,
                      allowed_commands: list[str], deny_paths: list[str], model: str | None,
                      json_schema: dict | None, max_budget_usd: float | None, permission_mode: str,
                      system_append: str | None = None, effort: str | None = None) -> list[str]:
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--session-id", session_id,
               "--max-turns", str(max_turns), "--setting-sources", "user,project"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if max_budget_usd:
            cmd += ["--max-budget-usd", str(max_budget_usd)]
        if system_append:
            cmd += ["--append-system-prompt", system_append]
        settings: dict[str, Any] = {"hooks": {}, "permissions": {"deny": []}}
        # Session-scoped hooks only. Nothing is installed globally.
        settings["hooks"]["Stop"] = [{"hooks": [_hook("stop", 20)]}]
        settings["hooks"]["SessionStart"] = [{"hooks": [_hook("session-start", 10)]}]
        settings["hooks"]["PreToolUse"] = [{"matcher": ".*", "hooks": [_hook("pre-tool-use", 10)]}]
        settings["hooks"]["TaskCompleted"] = [{"hooks": [_hook("task-completed", 300)]}]
        for p in deny_paths:
            settings["permissions"]["deny"] += [f"Read({p})", f"Edit({p})", f"Write({p})", f"Bash(cat {p})"]
        if role == "builder":
            cmd += ["--permission-mode", permission_mode]
            if permission_mode == "bypassPermissions":
                cmd += ["--dangerously-skip-permissions"]   # owner opted in via `longrun run --allow-bypass`
            allowed = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "Bash(longrun evidence *)",
                       "Bash(longrun observe *)", "Bash(longrun contract show*)", "Bash(longrun status*)",
                       "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git add *)", "Bash(git commit *)",
                       "Bash(git show*)", "Bash(ls*)", "Bash(mkdir *)"] + [f"Bash({c})" for c in allowed_commands]
            cmd += ["--allowedTools", *allowed]
            cmd += ["--disallowedTools", "Bash(longrun run*)", "Bash(longrun evaluate*)", "Bash(longrun checkpoint*)",
                    "Bash(longrun freeze*)", "Bash(longrun stop*)", "Bash(longrun reset*)", "Bash(longrun rebase*)",
                    "Bash(longrun uninstall*)", "Bash(longrun migrate*)", "Bash(git push*)", "Bash(git reset --hard*)",
                    "Bash(git checkout *)", "Bash(git rebase*)", "Bash(git worktree*)"]
        elif role in ("evaluator",):
            cmd += ["--permission-mode", "dontAsk", "--tools", "Read,Grep,Glob,Bash"]
            cmd += ["--allowedTools", "Read", "Grep", "Glob", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)",
                    "Bash(git status*)", "Bash(ls*)", "Bash(cat *)", "Bash(longrun status*)", "Bash(longrun contract show*)",
                    "Bash(longrun evidence list*)"] + [f"Bash({c})" for c in allowed_commands]
            cmd += ["--disallowedTools", *WRITE_TOOLS, "Bash(git add*)", "Bash(git commit*)", "Bash(rm *)", "Bash(mv *)",
                    "Bash(longrun evidence submit*)", "Bash(longrun observe*)"]
            settings["permissions"]["deny"] += [f"{t}" for t in WRITE_TOOLS]
        elif role in ("restart_manager", "planner", "contract_repair", "intent_reviewer"):
            cmd += ["--permission-mode", "dontAsk", "--tools", "Read,Grep,Glob,Bash"]
            cmd += ["--allowedTools", "Read", "Grep", "Glob", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git status*)", "Bash(ls*)"]
            cmd += ["--disallowedTools", *WRITE_TOOLS]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema)]
        cmd += ["--settings", json.dumps(settings)]
        return cmd

    # ------------------------------------------------------------------ stream parsing
    @staticmethod
    def parse_stream(lines: list[str]) -> dict:
        """Return {actions: [...], result: {...}|None, session_id, tool_uses}."""
        pending: dict[str, dict] = {}
        actions: list[dict] = []
        result = None
        session_id = None
        for line in lines:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            session_id = ev.get("session_id") or session_id
            if t == "assistant":
                for blk in (ev.get("message") or {}).get("content", []) or []:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        inp = blk.get("input") or {}
                        f = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                        pending[blk.get("id")] = {"tool": blk.get("name"), "input": inp, "file": f, "result": None, "is_error": False}
            elif t == "user":
                for blk in (ev.get("message") or {}).get("content", []) or []:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        a = pending.pop(blk.get("tool_use_id"), None)
                        if a is None:
                            continue
                        c = blk.get("content")
                        if isinstance(c, list):
                            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                        a["result"] = (c or "")[:2000] if isinstance(c, str) else json.dumps(c)[:2000]
                        a["is_error"] = bool(blk.get("is_error"))
                        actions.append(a)
            elif t == "result":
                result = ev
        for a in pending.values():
            actions.append(a)
        return {"actions": actions, "result": result, "session_id": session_id}

    @staticmethod
    def summarize_result(result: dict | None) -> dict:
        if not result:
            return {"cost_usd": 0.0, "num_turns": None, "duration_ms": None, "is_error": True, "text": "", "structured_output": None}
        return {"cost_usd": float(result.get("total_cost_usd") or 0.0), "num_turns": result.get("num_turns"),
                "duration_ms": result.get("duration_ms"), "is_error": bool(result.get("is_error")),
                "text": result.get("result") or "", "structured_output": result.get("structured_output"),
                "subtype": result.get("subtype")}
