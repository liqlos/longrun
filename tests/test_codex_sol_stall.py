import sys
import tempfile
import time
import unittest
from pathlib import Path

from longrun.controller import (_codex_sol_stall_action,
                                _strategic_initial_progress_timeout_seconds)
from longrun.process import ChildRunner


class CodexSolStallTest(unittest.TestCase):
    def test_contract_repair_gets_a_longer_initial_progress_window(self):
        self.assertEqual(_strategic_initial_progress_timeout_seconds("contract_repair"), 300)
        self.assertEqual(_strategic_initial_progress_timeout_seconds("planner"), 90)
        self.assertEqual(_strategic_initial_progress_timeout_seconds("evaluator"), 90)

    def test_sol_retries_once_then_stops_without_a_fallback(self):
        self.assertEqual(_codex_sol_stall_action("gpt-5.6-sol", True, 0), "retry_same_sol")
        self.assertEqual(_codex_sol_stall_action("gpt-5.6-sol", True, 1), "stop")
        self.assertIsNone(_codex_sol_stall_action("gpt-5.6-terra", True, 0))

    def test_stream_idle_timeout_fires_after_output_stops(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seen = []
            started = time.monotonic()
            result = ChildRunner().run(
                [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(5)"],
                cwd=root, env={}, timeout_s=5, stdout_path=root / "out", stderr_path=root / "err",
                on_stdout_line=seen.append, idle_timeout_s=1.0)
            self.assertEqual(seen, ["started"])
            self.assertTrue(result.timed_out)
            self.assertTrue(result.idle_timed_out)
            self.assertLess(time.monotonic() - started, 3.5)

    def test_idle_heartbeat_reports_observable_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            heartbeats = []
            result = ChildRunner().run(
                [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(.35)"],
                cwd=root, env={}, timeout_s=2, stdout_path=root / "out", stderr_path=root / "err",
                on_stdout_line=lambda line: None, idle_timeout_s=1, idle_heartbeat_s=0.1,
                on_idle_heartbeat=heartbeats.append)
            self.assertEqual(result.exit_code, 0)
            self.assertGreaterEqual(len(heartbeats), 1)
            progress = next(h for h in heartbeats if h["stream_lines"])
            self.assertEqual(progress["stream_lines"], 1)
            self.assertGreater(progress["stream_bytes"], 0)
            self.assertGreater(progress["idle_s"], 0)

    def test_stream_progress_resets_the_idle_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seen = []
            result = ChildRunner().run(
                [sys.executable, "-c",
                 "import time; [(print(i, flush=True), time.sleep(.3)) for i in range(6)]"],
                cwd=root, env={}, timeout_s=3, stdout_path=root / "out", stderr_path=root / "err",
                on_stdout_line=seen.append, idle_timeout_s=0.8)
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(len(seen), 6)

    def test_initial_progress_timeout_ignores_lifecycle_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = ChildRunner().run(
                [sys.executable, "-c", "import time; print('thread.started', flush=True); print('turn.started', flush=True); time.sleep(5)"],
                cwd=root, env={}, timeout_s=5, stdout_path=root / "out", stderr_path=root / "err",
                on_stdout_line=lambda line: None, initial_progress_timeout_s=1.0,
                is_initial_progress_line=lambda line: line == "work")
            self.assertTrue(result.timed_out)
            self.assertTrue(result.initial_progress_timed_out)
            self.assertFalse(result.idle_timed_out)

    def test_initial_progress_timeout_clears_after_first_substantive_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = ChildRunner().run(
                [sys.executable, "-c", "import time; print('thread.started', flush=True); time.sleep(.05); print('work', flush=True); time.sleep(.2)"],
                cwd=root, env={}, timeout_s=1, stdout_path=root / "out", stderr_path=root / "err",
                on_stdout_line=lambda line: None, initial_progress_timeout_s=1.5,
                is_initial_progress_line=lambda line: line == "work")
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.initial_progress_timed_out)


if __name__ == "__main__":
    unittest.main()
