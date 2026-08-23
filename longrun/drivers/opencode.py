"""OpenCode driver: controller-launched ``opencode run --format json`` sessions.

OpenCode is used for the high-volume builder role.  Strategic roles are routed
to Codex by the model resolver so planning and independent evaluation keep the
existing trust boundary and structured-output support.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path


class OpenCodeDriver:
    name = "opencode"

    def available(self) -> bool:
        return shutil.which("opencode") is not None

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def build_command(self, *, role: str, prompt: str, session_id: str, cwd: Path, max_turns: int,
                      allowed_commands: list[str], deny_paths: list[str], model: str | None,
                      json_schema: dict | None, max_budget_usd: float | None, permission_mode: str,
                      system_append: str | None = None, effort: str | None = None,
                      resume_session_id: str | None = None,
                      attach_url: str | None = None) -> list[str]:
        # Permissions are injected per child by controller._child_env.  --auto
        # prevents a non-interactive run from waiting for approval; explicit deny
        # rules remain enforced by OpenCode.
        agent = "build" if role == "builder" else "plan"
        cmd = ["opencode", "run", "--pure", "--format", "json"]
        if attach_url:
            cmd += ["--attach", attach_url]
        cmd += ["--dir", str(cwd), "--agent", agent, "--auto"]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        if model:
            cmd += ["--model", model]
        # OpenCode variants are provider-specific.  Do not translate Longrun's
        # generic effort into --variant unless a future model explicitly maps it.
        full = prompt if not system_append else f"{system_append}\n\n{prompt}"
        if json_schema is not None:
            full += ("\n\nYour final response must be exactly one JSON object matching this schema; "
                     "do not wrap it in Markdown:\n" + json.dumps(json_schema, ensure_ascii=False))
        cmd += [full]
        return cmd

    @staticmethod
    def parse_stream(lines: list[str]) -> dict:
        actions: list[dict] = []
        session_id = None
        last_text = ""
        failed = False
        finish_reason = None
        provider_error_text = ""
        cost = 0.0
        turns = 0
        usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        for raw in lines:
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            session_id = ev.get("sessionID") or session_id
            kind = ev.get("type")
            part = ev.get("part") or {}
            if kind == "step_start":
                turns += 1
            elif kind == "text":
                last_text = part.get("text") or last_text
            elif kind == "tool_use":
                state = part.get("state") or {}
                inp = state.get("input") or {}
                meta = state.get("metadata") or {}
                tool = part.get("tool") or "unknown"
                canonical = {"bash": "Bash", "write": "Write", "edit": "Edit"}.get(tool, tool)
                file_path = (inp.get("filePath") or inp.get("file_path") or inp.get("path") or
                             meta.get("filepath"))
                output = state.get("output")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False) if output is not None else ""
                status = state.get("status")
                actions.append({"tool": canonical, "input": inp, "file": file_path,
                                "result": output[:2000],
                                "is_error": status == "error" or meta.get("exit") not in (0, None),
                                "status": status, "metadata": meta,
                                "child_session_id": meta.get("sessionId") or meta.get("jobId")})
            elif kind == "step_finish":
                finish_reason = part.get("reason") or finish_reason
                tokens = part.get("tokens") or {}
                cache = tokens.get("cache") or {}
                usage["input_tokens"] += int(tokens.get("input") or 0)
                usage["output_tokens"] += int(tokens.get("output") or 0)
                usage["reasoning_tokens"] += int(tokens.get("reasoning") or 0)
                usage["cache_read_tokens"] += int(cache.get("read") or 0)
                usage["cache_write_tokens"] += int(cache.get("write") or 0)
                cost += float(part.get("cost") or 0.0)
                failed = failed or part.get("reason") in ("error", "failed")
            elif kind == "error":
                failed = True
                provider_error = ev.get("error")
                if provider_error:
                    provider_error_text = (json.dumps(provider_error, ensure_ascii=False)
                                           if not isinstance(provider_error, str)
                                           else provider_error)
                data = provider_error.get("data") if isinstance(provider_error, dict) else None
                msg = (part.get("message") or ev.get("message") or
                       (data.get("message") if isinstance(data, dict) else None) or
                       provider_error_text)
                if msg:
                    last_text = str(msg)
        result = {"session_id": session_id, "usage": usage, "text": last_text,
                  "is_error": failed, "cost_usd": cost, "num_turns": turns,
                  "finish_reason": finish_reason, "terminal": finish_reason == "stop",
                  "provider_error_text": provider_error_text}
        return {"actions": actions, "result": result, "session_id": session_id}

    @staticmethod
    def summarize_result(result: dict | None) -> dict:
        if not result:
            return {"cost_usd": 0.0, "num_turns": None, "duration_ms": None,
                    "is_error": True, "text": "", "structured_output": None}
        text = result.get("text") or ""
        structured = None
        if text.strip().startswith("{"):
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                pass
        return {"cost_usd": float(result.get("cost_usd") or 0.0),
                "num_turns": result.get("num_turns"), "duration_ms": None,
                "is_error": bool(result.get("is_error")), "text": text,
                "structured_output": structured, "usage": result.get("usage"),
                "thread_id": result.get("session_id"),
                "provider_error_text": result.get("provider_error_text") or "",
                "finish_reason": result.get("finish_reason"),
                "terminal": bool(result.get("terminal"))}
