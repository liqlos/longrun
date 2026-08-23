"""The planner is told where a product's own knowledge lives.

The harness stays project-agnostic: it does not know Skyline Riveters, only the shape
`projects/<product>/docs/project_specific_knowledge/INDEX.md`. Any product that keeps a subject base
beside its own sources gets it pointed at; a product without one is unaffected.
"""
import tempfile
import unittest
from pathlib import Path

from longrun.planner import default_hints


class SubjectKnowledgeHintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "projects" / "someproduct" / "docs"
        self.docs.mkdir(parents=True)
        (self.docs / "PROJECT_STATE.md").write_text("state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_products_subject_base_is_offered_to_the_planner(self):
        base = self.docs / "project_specific_knowledge"
        base.mkdir()
        (base / "INDEX.md").write_text("# subject")
        hints = default_hints(self.root)
        self.assertTrue(any("project_specific_knowledge/INDEX.md" in h for h in hints), hints)

    def test_the_hint_tells_the_planner_to_read_the_priority_rule(self):
        base = self.docs / "project_specific_knowledge"
        base.mkdir()
        (base / "INDEX.md").write_text("# subject")
        hint = next(h for h in default_hints(self.root) if "project_specific_knowledge" in h)
        self.assertIn("priority rule", hint)

    def test_a_product_without_one_is_unaffected(self):
        hints = default_hints(self.root)
        self.assertFalse(any("project_specific_knowledge" in h for h in hints), hints)
        self.assertTrue(any("PROJECT_STATE.md" in h for h in hints), hints)


if __name__ == "__main__":
    unittest.main()
