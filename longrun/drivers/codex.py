"""Codex driver: controller-launched `codex exec --json` threads.

Builder: host access, so a longrun task can operate the local development environment it was assigned
(Unity's local UPM socket and licensing database are one example).  Planner/evaluator remain read-only;
approval stays never and JSONL is parsed.
Evaluator: read-only sandbox, --output-schema forcing one JSON object, last message written to a file.
No Codex hooks are installed anywhere; Codex-specific state is never authoritative.
"""
from __future__ import annotations
import json
import shutil
import uuid
from pathlib import Path


def validate_strict_output_schema(schema: dict) -> None:
    """Reject schemas Codex strict structured output will reject."""
    errors: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        is_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
        if is_object:
            properties = node.get("properties")
            if not isinstance(properties, dict):
                errors.append(f"{path}: object schema needs properties")
            else:
                required = node.get("required")
                missing = sorted(set(properties) - set(required or []))
                extra = sorted(set(required or []) - set(properties))
                if not isinstance(required, list) or missing or extra:
                    errors.append(f"{path}: required must equal properties (missing={missing}, extra={extra})")
            if node.get("additionalProperties") is not False:
                errors.append(f"{path}: additionalProperties must be false")
        for key, child in node.items():
            walk(child, f"{path}.{key}")

    walk(schema, "$")
    if errors:
        raise ValueError("invalid strict Codex output schema: " + "; ".join(errors[:8]))


class CodexDriver:
    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def new_session_id(self) -> str:
        return str(uuid.uuid4())   # controller id; the codex thread id is recorded separately from thread.started

    def build_command(self, *, role: str, prompt: str, session_id: str, cwd: Path, max_turns: int,
                      allowed_commands: list[str], deny_paths: list[str], model: str | None,
                      json_schema: dict | None, max_budget_usd: float | None, permission_mode: str,
                      system_append: str | None = None, schema_path: Path | None = None,
                      last_message_path: Path | None = None, effort: str | None = None,
                      writable_dirs: list[Path] | None = None,
                      sandbox_mode: str | None = None) -> list[str]:
        # The controller supplies a credential-only CODEX_HOME.  `--ignore-user-config`
        # makes that boundary explicit and avoids loading desktop MCPs or hooks.  Do not
        # override nested mcp_servers entries here: when no server is configured, Codex
        # treats such an override as an invalid server with no transport.
        cmd = ["codex", "exec", "--json", "-C", str(cwd), "--skip-git-repo-check", "--ignore-user-config",
               "-c", 'approval_policy="never"']
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if role == "builder":
            cmd += ["--sandbox", sandbox_mode or "workspace-write"]
            for directory in writable_dirs or []:
                cmd += ["--add-dir", str(directory)]
        else:
            cmd += ["--sandbox", "read-only"]
        if json_schema is not None and schema_path is not None:
            validate_strict_output_schema(json_schema)
            schema_path.write_text(json.dumps(json_schema))
            cmd += ["--output-schema", str(schema_path)]
        if last_message_path is not None:
            cmd += ["-o", str(last_message_path)]
        full = prompt if not system_append else f"{system_append}\n\n{prompt}"
        cmd += [full]
        return cmd

    @staticmethod
    def parse_stream(lines: list[str]) -> dict:
        actions: list[dict] = []
        thread_id = None
        usage = None
        last_text = ""
        failed = False
        provider_errors: list[str] = []
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                thread_id = ev.get("thread_id")
            elif t == "turn.completed":
                usage = ev.get("usage")
            elif t == "turn.failed" or t == "error":
                failed = True
                payload = ev.get("message") or ev.get("error")
                if isinstance(payload, dict):
                    payload = payload.get("message") or json.dumps(payload, ensure_ascii=False)
                if payload:
                    provider_errors.append(str(payload))
            elif t == "item.completed":
                it = ev.get("item") or {}
                k = it.get("type")
                if k == "command_execution":
                    actions.append({"tool": "Bash", "input": {"command": it.get("command")}, "file": None,
                                    "result": (it.get("aggregated_output") or "")[:2000],
                                    "is_error": (it.get("exit_code") not in (0, None))})
                elif k == "file_change":
                    for ch in it.get("changes") or []:
                        actions.append({"tool": "apply_patch", "input": {"path": ch.get("path"), "kind": ch.get("kind")},
                                        "file": ch.get("path"), "result": "ok", "is_error": False})
                elif k == "agent_message":
                    last_text = it.get("text") or last_text
        return {"actions": actions, "result": {"thread_id": thread_id, "usage": usage, "text": last_text,
                "is_error": failed, "provider_error_text": "\n".join(provider_errors[-4:])},
                "session_id": thread_id}

    @staticmethod
    def summarize_result(result: dict | None) -> dict:
        if not result:
            return {"cost_usd": 0.0, "num_turns": None, "duration_ms": None, "is_error": True,
                    "text": "", "provider_error_text": "", "structured_output": None}
        so = None
        txt = result.get("text") or ""
        if txt.strip().startswith("{"):
            try:
                so = json.loads(txt)
            except json.JSONDecodeError:
                so = None
        return {"cost_usd": 0.0, "num_turns": None, "duration_ms": None, "is_error": bool(result.get("is_error")),
                "text": txt, "provider_error_text": result.get("provider_error_text") or "",
                "structured_output": so, "usage": result.get("usage"), "thread_id": result.get("thread_id")}
