from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from longrun.drivers.codex import CodexDriver, validate_strict_output_schema
from longrun.drivers.claude import ClaudeDriver
from longrun.drivers.opencode import OpenCodeDriver
from longrun.narrate import render_event


class TestDrivers(unittest.TestCase):
    def test_codex_commands_and_parse(self):
        d = CodexDriver()
        b = d.build_command(role="builder", prompt="p", session_id="s", cwd=Path("/tmp"), max_turns=5, allowed_commands=[], deny_paths=[], model=None, json_schema=None, max_budget_usd=None, permission_mode="acceptEdits", writable_dirs=[Path("/tmp/evidence"), Path("/tmp/git")])
        self.assertIn("workspace-write", b); self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", b); self.assertIn("--json", b)
        self.assertIn("--ignore-user-config", b)
        self.assertFalse(any("mcp_servers" in item for item in b))
        self.assertEqual(b.count("--add-dir"), 2)
        unity = d.build_command(role="builder", prompt="p", session_id="s", cwd=Path("/tmp"), max_turns=5, allowed_commands=[], deny_paths=[], model=None, json_schema=None, max_budget_usd=None, permission_mode="acceptEdits", sandbox_mode="danger-full-access")
        self.assertIn("danger-full-access", unity)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", unity)
        strict_schema = {"type": "object", "additionalProperties": False, "required": [], "properties": {}}
        e = d.build_command(role="evaluator", prompt="p", session_id="s", cwd=Path("/tmp"), max_turns=5, allowed_commands=[], deny_paths=[], model=None, json_schema=strict_schema, max_budget_usd=None, permission_mode="acceptEdits", schema_path=Path("/tmp/longrun-schema-test.json"), last_message_path=Path("/tmp/longrun-last-test.txt"))
        self.assertIn("read-only", e); self.assertIn("--output-schema", e); self.assertIn("-o", e)
        lines = [json.dumps({"type": "thread.started", "thread_id": "T1"}),
                 json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pytest", "aggregated_output": "1 failed", "exit_code": 1}}),
                 json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}}),
                 json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{\"overall\":\"PASS\"}"}}),
                 json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}})]
        p = d.parse_stream(lines)
        self.assertEqual(p["session_id"], "T1"); self.assertEqual(len(p["actions"]), 2); self.assertTrue(p["actions"][0]["is_error"])
        self.assertEqual(d.summarize_result(p["result"])["structured_output"], {"overall": "PASS"})

    def test_codex_strict_schema_preflight_rejects_nested_optional_properties(self):
        invalid = {"type": "object", "additionalProperties": False,
                   "required": ["budgets"], "properties": {
                       "budgets": {"type": "object", "additionalProperties": False,
                                   "required": ["wall"], "properties": {
                                       "wall": {"type": "integer"},
                                       "turns": {"type": "integer"}}}}}
        with self.assertRaisesRegex(ValueError, r"\$\.properties\.budgets.*missing=\['turns'\]"):
            validate_strict_output_schema(invalid)

    def test_codex_provider_error_is_preserved_separately(self):
        d = CodexDriver()
        message = '{"error":{"code":"invalid_json_schema"},"status":400}'
        parsed = d.parse_stream([
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "error", "message": message}),
            json.dumps({"type": "turn.failed", "error": {"message": message}}),
        ])
        summary = d.summarize_result(parsed["result"])
        self.assertTrue(summary["is_error"])
        self.assertIn("invalid_json_schema", summary["provider_error_text"])

    def test_claude_parse(self):
        d = ClaudeDriver()
        lines = [json.dumps({"type": "assistant", "session_id": "S", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "x.py"}}]}}),
                 json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}),
                 json.dumps({"type": "result", "total_cost_usd": 0.5, "num_turns": 2, "structured_output": {"a": 1}, "is_error": False, "result": "done"})]
        p = d.parse_stream(lines)
        self.assertEqual(p["actions"][0]["file"], "x.py"); self.assertEqual(d.summarize_result(p["result"])["cost_usd"], 0.5)

    def test_opencode_command_and_parse(self):
        d = OpenCodeDriver()
        cmd = d.build_command(role="builder", prompt="p", session_id="s", cwd=Path("/tmp"),
                              max_turns=5, allowed_commands=[], deny_paths=[],
                              model="opencode/x-preview-f-free", json_schema=None,
                              max_budget_usd=None, permission_mode="auto")
        self.assertEqual(cmd[:3], ["opencode", "run", "--pure"])
        self.assertIn("--format", cmd)
        self.assertIn("--auto", cmd); self.assertIn("opencode/x-preview-f-free", cmd)
        resumed = d.build_command(role="builder", prompt="continue", session_id="s2", cwd=Path("/tmp"),
                                  max_turns=5, allowed_commands=[], deny_paths=[], model=None,
                                  json_schema=None, max_budget_usd=None, permission_mode="auto",
                                  resume_session_id="O1", attach_url="http://127.0.0.1:43210")
        self.assertEqual(resumed[resumed.index("--session") + 1], "O1")
        self.assertEqual(resumed[resumed.index("--attach") + 1], "http://127.0.0.1:43210")
        lines = [
            json.dumps({"type": "step_start", "sessionID": "O1", "part": {"type": "step-start"}}),
            json.dumps({"type": "tool_use", "sessionID": "O1", "part": {"tool": "write", "state": {
                "status": "completed", "input": {"filePath": "/tmp/a.txt"}, "output": "ok", "metadata": {}}}}),
            json.dumps({"type": "tool_use", "sessionID": "O1", "part": {"tool": "task", "state": {
                "status": "completed", "input": {"description": "R01 inspect", "prompt": "R01 inspect",
                    "subagent_type": "swarm-researcher", "background": True},
                "output": "started", "metadata": {"sessionId": "CHILD1", "background": True}}}}),
            json.dumps({"type": "text", "sessionID": "O1", "part": {"text": "{\"overall\":\"PASS\"}"}}),
            json.dumps({"type": "step_finish", "sessionID": "O1", "part": {"reason": "stop", "cost": 0,
                "tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 4, "write": 0}}}}),
        ]
        parsed = d.parse_stream(lines)
        self.assertEqual(parsed["session_id"], "O1")
        self.assertEqual(parsed["actions"][0]["tool"], "Write")
        self.assertEqual(parsed["actions"][1]["tool"], "task")
        self.assertEqual(parsed["actions"][1]["child_session_id"], "CHILD1")
        self.assertTrue(parsed["actions"][1]["input"]["background"])
        summary = d.summarize_result(parsed["result"])
        self.assertEqual(summary["structured_output"], {"overall": "PASS"})
        self.assertEqual(summary["usage"]["cache_read_tokens"], 4)
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["finish_reason"], "stop")

    def test_opencode_nested_provider_error_is_preserved(self):
        d = OpenCodeDriver()
        lines = [json.dumps({
            "type": "error", "sessionID": "O2",
            "error": {"name": "APIError", "data": {
                "message": "Provider finish_reason: network_error",
                "isRetryable": True,
                "metadata": {"code": "ProviderResponseStreamError"},
            }},
        })]
        summary = d.summarize_result(d.parse_stream(lines)["result"])
        self.assertTrue(summary["is_error"])
        self.assertIn("network_error", summary["text"])
        self.assertIn("ProviderResponseStreamError", summary["provider_error_text"])

    def test_opencode_stream_is_human_readable_in_watch(self):
        text_event = {"type": "text", "part": {"text": "Inspecting the Skyline scene before editing."}}
        self.assertEqual(
            render_event(text_event, "builder", verbose=False, quiet=True),
            ["🔨 Inspecting the Skyline scene before editing."],
        )
        tool_event = {"type": "tool_use", "part": {"tool": "write", "state": {
            "status": "completed", "input": {"filePath": "/tmp/proof.txt"}, "output": "written"}}}
        self.assertEqual(
            render_event(tool_event, "builder", verbose=True),
            ["🔨 → write /tmp/proof.txt", "      ↳ written"],
        )
        self.assertEqual(render_event(tool_event, "builder", verbose=False, quiet=True), [])

    def test_chain_stop_marker_is_private_and_project_specific(self):
        from longrun.paths import chain_stop_marker
        a = chain_stop_marker(Path("/tmp/project-a"))
        b = chain_stop_marker(Path("/tmp/project-b"))
        self.assertNotEqual(a, b)
        self.assertIn("chain-stops", str(a))
        self.assertNotIn("project-a/.longrun", str(a))

    def test_child_env_uses_credential_only_codex_home(self):
        from longrun.controller import _child_env
        from longrun.store import RunStore
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("LONGRUN_HOME")
            os.environ["LONGRUN_HOME"] = td
            try:
                st = RunStore.create(Path(td), "software", None, {})
                env = _child_env(st, "session", "planner", 60, driver_name="codex")
                self.assertEqual(env["CODEX_HOME"], str(st.tmp_dir / "codex-home"))
                self.assertFalse((st.tmp_dir / "codex-home" / "config.toml").exists())
            finally:
                if old is None: os.environ.pop("LONGRUN_HOME", None)
                else: os.environ["LONGRUN_HOME"] = old

    def test_opencode_child_env_is_scoped_and_denies_dangerous_actions(self):
        from longrun.controller import _child_env
        from longrun.store import RunStore
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("LONGRUN_HOME"); os.environ["LONGRUN_HOME"] = td
            try:
                st = RunStore.create(Path(td), "software", None, {})
                env = _child_env(st, "session", "builder", 60, driver_name="opencode")
                permission = json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]
                self.assertEqual(permission["bash"]["git push*"], "deny")
                self.assertNotIn("nohup *", permission["bash"])
                self.assertNotIn("setsid *", permission["bash"])
                self.assertEqual(permission["bash"]["launchctl *"], "deny")
                self.assertEqual(
                    permission["bash"]["env -u LONGRUN_SESSION_MARKER nohup setsid *"],
                    "allow",
                )
                self.assertEqual(permission["bash"]["*LONGRUN_SESSION_MARKER*"], "deny")
                self.assertEqual(permission["external_directory"], "deny")
                self.assertEqual(env["OPENCODE_AUTO_SHARE"], "false")
                self.assertNotIn("CODEX_HOME", env)
            finally:
                if old is None: os.environ.pop("LONGRUN_HOME", None)
                else: os.environ["LONGRUN_HOME"] = old


if __name__ == "__main__":
    unittest.main()
