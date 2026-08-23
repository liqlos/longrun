import subprocess
import tempfile
import unittest
from pathlib import Path

from longrun import gitutil as G


def sh(cwd, *args):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


class LandingTest(unittest.TestCase):
    """A passed outcome that cannot fast-forward into the project must be visible, never silent:
    its commits stay on the branch and `unmerged_run_branches` reports them so the chain can refuse to go on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "config", "user.name", "t")
        (self.repo / "a.txt").write_text("base\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _branch_with_commit(self, name, filename):
        sh(self.repo, "git", "branch", name)
        sh(self.repo, "git", "checkout", "-q", name)
        (self.repo / filename).write_text("work\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", f"work on {name}")
        sh(self.repo, "git", "checkout", "-q", "main")

    def test_clean_fast_forward_lands_and_leaves_nothing_stranded(self):
        self._branch_with_commit("longrun/aaaa1111", "b.txt")
        ok, _ = G.fast_forward_into(self.repo, "longrun/aaaa1111")
        self.assertTrue(ok)
        self.assertEqual(G.unmerged_run_branches(self.repo), [])

    def test_diverged_history_refuses_and_is_reported_as_stranded(self):
        self._branch_with_commit("longrun/bbbb2222", "b.txt")
        # the project moves on independently -> no fast-forward is possible any more
        (self.repo / "c.txt").write_text("owner edit\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "owner commit on main")
        ok, _ = G.fast_forward_into(self.repo, "longrun/bbbb2222")
        self.assertFalse(ok)
        self.assertEqual(G.unmerged_run_branches(self.repo), [("longrun/bbbb2222", 1)])

    def test_dirty_project_tree_refuses_to_merge(self):
        self._branch_with_commit("longrun/cccc3333", "b.txt")
        (self.repo / "a.txt").write_text("uncommitted change\n")
        ok, out = G.fast_forward_into(self.repo, "longrun/cccc3333")
        self.assertFalse(ok)
        self.assertIn("uncommitted", out)


if __name__ == "__main__":
    unittest.main()
