from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from longrun import gitutil as G


class LargeGitFileTest(unittest.TestCase):
    def test_large_file_hash_notices_an_edit_outside_the_old_samples(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "large.bin"
            with path.open("wb") as fh:
                fh.seek(70 * 1024 * 1024 - 1); fh.write(b"\0")
                fh.seek(10 * 1024 * 1024); fh.write(b"A")
            first = hashlib.sha256(); G._hash_worktree_file(first, path)
            with path.open("r+b") as fh:
                fh.seek(10 * 1024 * 1024); fh.write(b"B")
            second = hashlib.sha256(); G._hash_worktree_file(second, path)
            self.assertNotEqual(first.hexdigest(), second.hexdigest())

    def test_large_changed_file_is_named_without_materializing_its_diff(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            large = repo / "scene.unity"
            large.write_bytes(b"base\n")
            subprocess.run(["git", "add", "scene.unity"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)
            base = G.head(repo)
            with large.open("wb") as fh:
                fh.seek(17 * 1024 * 1024); fh.write(b"changed\n")
            diff = G.diff_text(repo, base, max_bytes=1000)
            self.assertIn("scene.unity", diff)
            self.assertIn("withheld from this diff", diff)
            self.assertLess(len(diff), 5000)

    def test_save_patch_streams_to_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            source = repo / "data.bin"; source.write_bytes(b"base")
            subprocess.run(["git", "add", "data.bin"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)
            source.write_bytes(b"changed")
            patch = repo / "change.patch"
            self.assertTrue(G.save_patch(repo, G.head(repo), patch))
            self.assertIn("data.bin", patch.read_text(errors="replace"))


if __name__ == "__main__":
    unittest.main()
