import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


def sh(cwd, *args):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


class PruneSafetyTest(unittest.TestCase):
    """`prune` deletes run workspaces to reclaim build caches (35.7 GB on one machine, which was about to
    hit a 38 GiB free-space floor). It must never delete the workspace of a run whose work has not landed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "config", "user.name", "t")
        (self.repo / "a.txt").write_text("base\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "base")
        self._saved = os.environ.get("LONGRUN_HOME")
        os.environ["LONGRUN_HOME"] = str(self.home / "lrhome")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LONGRUN_HOME", None)
        else:
            os.environ["LONGRUN_HOME"] = self._saved
        self.tmp.cleanup()

    def _make_run(self, run_id, status, with_unmerged_commit):
        from longrun.paths import runs_root
        d = runs_root() / run_id
        d.mkdir(parents=True)
        ws = d / "workspace"
        sh(self.repo, "git", "worktree", "add", "-q", "-b", f"longrun/{run_id[:8]}", str(ws), "HEAD")
        (ws / "big.bin").write_text("x" * 5000)
        if with_unmerged_commit:
            sh(ws, "git", "add", "-A")
            sh(ws, "git", "commit", "-qm", "work that never landed")
        (d / "state.json").write_text(json.dumps({"run_id": run_id, "status": status,
                                                   "project_root": str(self.repo)}))
        return ws

    def _prune(self, dry_run=False):
        from longrun.cli import cmd_prune
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_prune(SimpleNamespace(project=str(self.repo), dry_run=dry_run))
        return buf.getvalue()

    def test_keeps_workspace_whose_branch_has_unmerged_work(self):
        ws = self._make_run("aaaa1111-0000-0000-0000-000000000000", "PASSED", with_unmerged_commit=True)
        out = self._prune()
        self.assertTrue(ws.is_dir(), f"prune deleted unlanded work.\n{out}")
        self.assertIn("kept", out)

    def test_deletes_workspace_of_a_finished_run_with_nothing_unmerged(self):
        ws = self._make_run("bbbb2222-0000-0000-0000-000000000000", "PASSED", with_unmerged_commit=False)
        out = self._prune()
        self.assertFalse(ws.is_dir(), f"prune did not reclaim a landed run.\n{out}")

    def test_leaves_a_run_that_is_still_going(self):
        ws = self._make_run("cccc3333-0000-0000-0000-000000000000", "RUNNING", with_unmerged_commit=False)
        self._prune()
        self.assertTrue(ws.is_dir(), "prune must not touch a run that has not reached a terminal state")

    def test_dry_run_deletes_nothing(self):
        ws = self._make_run("dddd4444-0000-0000-0000-000000000000", "PASSED", with_unmerged_commit=False)
        out = self._prune(dry_run=True)
        self.assertTrue(ws.is_dir())
        self.assertIn("would free", out)

    def test_does_not_prune_a_different_project(self):
        ws = self._make_run("eeee5555-0000-0000-0000-000000000000", "PASSED", with_unmerged_commit=False)
        state = ws.parent / "state.json"
        data = json.loads(state.read_text())
        data["project_root"] = str(self.home / "other-repo")
        state.write_text(json.dumps(data))
        self._prune()
        self.assertTrue(ws.is_dir(), "project-scoped prune must not delete another repository's workspace")


if __name__ == "__main__":
    unittest.main()
