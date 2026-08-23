import subprocess
import tempfile
import unittest
from pathlib import Path

from longrun import gitutil as G


class GitSafetyTest(unittest.TestCase):
    def test_dirty_snapshot_diff_excludes_unchanged_owner_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
            (repo / "owner.txt").write_text("committed\n")
            (repo / "run.txt").write_text("before\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            base = G.head(repo)
            (repo / "owner.txt").write_text("owner pre-existing change\n")
            snapshot = repo / ".snapshot"
            G.snapshot_dirty_baseline(repo, base, snapshot)
            (repo / "run.txt").write_text("after\n")

            diff = G.diff_text_from_dirty_baseline(repo, base, snapshot,
                                                   exclude_globs=[".snapshot/**"])
            self.assertNotIn("owner pre-existing change", diff)
            self.assertIn("+after", diff)

    def test_diff_text_names_and_hashes_untracked_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            (repo / "new-result.bin").write_bytes(b"new result")

            diff = G.diff_text(repo, G.head(repo))
            self.assertIn("?? new-result.bin", diff)
            self.assertIn("content-hash=", diff)

    def test_hard_reset_preserves_untracked_builder_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            rev = G.head(repo)
            tracked.write_text("changed\n")
            untracked = repo / "generated.glb"
            untracked.write_bytes(b"builder artifact")

            self.assertTrue(G.hard_reset(repo, rev))
            self.assertEqual(tracked.read_text(), "base\n")
            self.assertEqual(untracked.read_bytes(), b"builder artifact")


if __name__ == "__main__":
    unittest.main()
