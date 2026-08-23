import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from longrun.controller import (_infra_failure, _non_retryable_provider_error,
                                _retryable_provider_transport_error,
                                _opencode_recovery_reason, _run_check, _session_timeout_seconds)
from tests.helpers import LongrunTestCase


def res(exit_code=1, dur=6.0, timed_out=False, interrupted=False):
    return SimpleNamespace(exit_code=exit_code, duration_s=dur, timed_out=timed_out, interrupted=interrupted)


class InfraFailureTest(unittest.TestCase):
    def test_check_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _run_check({"cmd": "sleep 30 & echo $! > child.pid; wait", "timeout_seconds": 1}, root, 2)
            self.assertTrue(result["timed_out"])
            child = int((root / "child.pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_each_retry_timeout_is_capped_by_current_wall_budget(self):
        budgets = {"child_timeout_seconds": 5400, "evaluator_timeout_seconds": 900}
        self.assertEqual(_session_timeout_seconds(10_000, budgets, "builder", False, now=8_000), 2000)
        self.assertEqual(_session_timeout_seconds(10_000, budgets, "builder", False, now=9_960), 40)
        self.assertEqual(_session_timeout_seconds(10_000, budgets, "builder", False, now=9_971), 0)
        self.assertEqual(_session_timeout_seconds(10_000, budgets, "evaluator", True, now=9_960), 40)
        self.assertEqual(_session_timeout_seconds(10_000, budgets, "evaluator", True, now=8_000), 900)

    def test_usage_limit_with_reset_time_waits_until_reset(self):
        h = (time.localtime().tm_hour + 1) % 24
        ap = "am" if h < 12 else "pm"; hh = h % 12 or 12
        infra, wait = _infra_failure(res(), 0, f"You've hit your session limit · resets {hh}:00{ap} (Europe/Belgrade)")
        self.assertTrue(infra)
        lt = time.localtime()
        expect = (60 - lt.tm_min) * 60 - lt.tm_sec + 90
        self.assertTrue(abs(wait - expect) < 5, (wait, expect))

    def test_short_zero_action_crash_is_infra(self):
        infra, wait = _infra_failure(res(), 0, "some stderr")
        self.assertTrue(infra); self.assertEqual(wait, 120)

    def test_deterministic_provider_request_errors_are_not_retryable(self):
        self.assertTrue(_non_retryable_provider_error(
            '{"code":"invalid_json_schema","status":400}'))
        self.assertTrue(_non_retryable_provider_error(
            '{"type":"invalid_request_error","status":400}'))
        self.assertFalse(_non_retryable_provider_error(
            '{"type":"rate_limit_error","status":429}'))
        self.assertFalse(_non_retryable_provider_error(
            '{"type":"server_error","status":529}'))
        self.assertFalse(_non_retryable_provider_error("ECONNRESET while fetching response"))

    def test_retryable_opencode_stream_error_is_detected(self):
        self.assertTrue(_retryable_provider_transport_error(
            "Provider finish_reason: network_error ProviderResponseStreamError"))
        self.assertFalse(_retryable_provider_transport_error("invalid_json_schema"))

    def test_transient_provider_and_network_failures_remain_retryable(self):
        for message in (
            '{"type":"rate_limit_error","status":429}',
            '{"type":"server_error","status":529}',
            "ECONNRESET while fetching response",
            "getaddrinfo ENOTFOUND api.openai.com",
        ):
            infra, wait = _infra_failure(res(), 0, message)
            self.assertTrue(infra, message)
            self.assertGreater(wait, 0, message)
        self.assertEqual(_infra_failure(
            res(timed_out=True), 0, "", produced_evidence=False), (True, 0))

    def test_real_work_is_not_infra(self):
        self.assertEqual(_infra_failure(res(exit_code=1, dur=300), 12, "Error: tests failed"), (False, 0))
        self.assertEqual(_infra_failure(res(exit_code=0), 0, ""), (False, 0))
        self.assertEqual(_infra_failure(res(timed_out=True), 0, "", produced_evidence=False), (True, 0))

    def test_timeout_after_real_work_is_not_infra(self):
        self.assertEqual(_infra_failure(res(timed_out=True), 12, "", produced_evidence=False), (False, 0))
        self.assertEqual(_infra_failure(res(timed_out=True), 0, "", produced_evidence=True), (False, 0))

    def test_opencode_abnormal_end_recovers_even_after_actions(self):
        summary = {"is_error": False, "num_turns": 8, "terminal": False,
                   "finish_reason": "tool-calls", "text": "working"}
        self.assertEqual(
            _opencode_recovery_reason(res(exit_code=1, dur=300), summary, produced_evidence=False),
            "process_exit_1",
        )
        self.assertEqual(
            _opencode_recovery_reason(res(exit_code=0, dur=300), summary, produced_evidence=False),
            "premature_eof",
        )

    def test_opencode_clean_stop_or_submitted_evidence_does_not_recover(self):
        clean = {"is_error": False, "num_turns": 8, "terminal": True,
                 "finish_reason": "stop", "text": "done"}
        self.assertIsNone(_opencode_recovery_reason(res(exit_code=0), clean, produced_evidence=False))
        broken = {**clean, "terminal": False, "finish_reason": "length"}
        self.assertIsNone(_opencode_recovery_reason(res(exit_code=0), broken, produced_evidence=True))


class ProviderRequestIntegrationTest(LongrunTestCase):
    def test_invalid_schema_provider_error_launches_once_without_infra_wait(self):
        from longrun.controller import (NonRetryableProviderRequestError,
                                        create_run, launch_session)

        repo = self.repo()
        store = create_run(repo, "software", "codex", isolation="none", allow_dirty=True)
        with store.transaction() as state:
            state["deadline_epoch"] = time.time() + 60

        class FakeRunner:
            on_child_start = None

            def __init__(self):
                self.calls = 0

            def run(self, _cmd, **kwargs):
                self.calls += 1
                message = json.dumps({
                    "type": "error",
                    "error": {"type": "invalid_request_error", "code": "invalid_json_schema",
                              "message": "Missing first_progress_deadline_seconds"},
                    "status": 400,
                })
                kwargs["stdout_path"].write_text(
                    json.dumps({"type": "thread.started", "thread_id": "T1"}) + "\n" +
                    json.dumps({"type": "error", "message": message}) + "\n")
                kwargs["stderr_path"].write_text("")
                return SimpleNamespace(exit_code=1, duration_s=0.1, timed_out=False,
                                       interrupted=False, idle_timed_out=False,
                                       initial_progress_timed_out=False)

        runner = FakeRunner()
        schema = {"type": "object", "additionalProperties": False,
                  "required": [], "properties": {}}
        with self.assertRaises(NonRetryableProviderRequestError):
            launch_session(store, runner, role="planner", prompt="plan",
                           json_schema=schema, max_turns=1)
        self.assertEqual(runner.calls, 1)
        self.assertFalse(any(event["kind"] == "session.infra_wait"
                             for event in store.events()))


if __name__ == "__main__":
    unittest.main()
