from __future__ import annotations
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from longrun.loopguard import analyze_stream


class TestLoopGuard(unittest.TestCase):
    def test_repeated_pairs(self):
        acts = [{"tool": "Bash", "input": {"command": "pytest"}, "result": "FAILED x", "is_error": True}] * 4
        self.assertTrue(analyze_stream(acts).fired)

    def test_repeated_errors_different_commands(self):
        def acts(n):
            return [{"tool": "Bash", "input": {"command": f"pytest -k t{i}"}, "result": "ImportError: no module foo",
                     "is_error": True} for i in range(n)]
        self.assertTrue(analyze_stream(acts(8)).repeated_error_signatures >= 1)
        # Five identical failures is the ordinary run-guard / fix / run-guard-again cycle the harness asks for,
        # not a loop; killing the session there threw away a third of the rounds it killed.
        self.assertEqual(analyze_stream(acts(5)).repeated_error_signatures, 0)

    def test_alternating_edit_revert(self):
        a = {"tool": "Edit", "input": {"file_path": "f.py", "old_string": "a", "new_string": "b"}, "file": "f.py", "result": "ok"}
        b = {"tool": "Edit", "input": {"file_path": "f.py", "old_string": "b", "new_string": "a"}, "file": "f.py", "result": "ok"}
        self.assertTrue(analyze_stream([a, b, a, b]).alternating_edit_revert >= 1)

    def test_healthy_stream(self):
        acts = [{"tool": "Read", "input": {"file_path": f"f{i}.py"}, "result": f"content {i}", "is_error": False} for i in range(20)]
        acts += [{"tool": "Bash", "input": {"command": "pytest"}, "result": "3 passed", "is_error": False}]
        self.assertFalse(analyze_stream(acts).fired)


if __name__ == "__main__":
    unittest.main()
