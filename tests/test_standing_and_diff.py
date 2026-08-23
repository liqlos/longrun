import subprocess
import tempfile
import unittest
from pathlib import Path

from longrun import gitutil as G
from longrun.adapters import load_adapter
from longrun.contract import ContractError, validate_contract


def _base_contract(criteria):
    return {"outcome_id": "O1", "title": "t", "observable_end_state": "a stranger sees the thing working now",
            "adapter": "software", "criteria": criteria,
            "budgets": {"wall_time_seconds": 600, "child_timeout_seconds": 60, "max_rounds": 3,
                        "max_repairs": 1, "max_fresh_restarts": 0}}


def _crit(cid, kind="functional"):
    return {"id": cid, "statement": "something observable happens here", "kind": kind,
            "evidence_requirements": ["check"], "deterministic_checks": [], "evaluator_policy": "llm_required"}


class StandingChecksTest(unittest.TestCase):
    """Nine consecutive contracts each spent a criterion re-asking "does the shift still play", in nine
    slightly different wordings — 35% of one night's criteria went to things that were not the outcome. A
    regression belongs to the project, runs every round and blocks the run; it is not an outcome."""

    def test_a_project_declares_its_standing_suite(self):
        a = load_adapter("vr_visual", {"standing_checks": [{"cmd": "python3 scripts/smoke.py", "timeout_seconds": 120}]})
        self.assertEqual(len(a.standing_checks), 1)
        self.assertEqual(a.standing_checks[0]["kind"], "check")

    def test_there_is_no_standing_suite_unless_declared(self):
        self.assertEqual(load_adapter("vr_visual", None).standing_checks, [])

    def test_it_is_published_in_the_descriptor(self):
        self.assertIn("standing_checks", load_adapter("vr_visual", None).to_json())


class DocsCriterionPolicyTest(unittest.TestCase):
    """How many documentation criteria a contract may carry is a project's policy, not a structural invariant,
    and this validator is shared by every project. The planner is told the rule; the validator does not enforce
    it, so breaking it costs guidance rather than a hard rejection and a planner retry."""

    def test_the_shared_validator_does_not_impose_a_cap(self):
        validate_contract(_base_contract([_crit("C1-diary", "docs"), _crit("C2-notes", "docs")]))

    def test_the_planner_is_told_the_rule_instead(self):
        from longrun.planner import planner_prompt
        p = planner_prompt(goal="g", project_root=Path("/tmp/x"), adapter_name="software", run_id="r" * 8,
                           project_hints=["README.md"], adapter_fragment="", prior_error=None)
        self.assertIn("documentation criterion", p)


class DiffExclusionTest(unittest.TestCase):
    """A regenerated Unity scene took 22–91% (median ~45%) of the evaluator's 60k diff window, and a
    one-material colour change moved 131,340 lines of it — so the evaluator could not read the code it was
    judging. The --stat is computed before exclusion, so the file is still named and counted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + a, cwd=self.repo, check=True, capture_output=True)
        (self.repo / "code.cs").write_text("class A {}\n")
        (self.repo / "scene.unity").write_text("original\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True, capture_output=True)
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                                   text=True).stdout.strip()
        (self.repo / "code.cs").write_text("class A { void B() {} }\n")
        (self.repo / "scene.unity").write_text("GENERATED_NOISE\n" * 500)

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_exclusion_the_generated_file_dominates(self):
        d = G.diff_text(self.repo, self.base)
        self.assertIn("GENERATED_NOISE", d)

    def test_excluding_it_withholds_the_body(self):
        d = G.diff_text(self.repo, self.base, exclude_globs=["*.unity"])
        self.assertNotIn("GENERATED_NOISE", d)

    def test_the_excluded_file_is_still_named_in_the_stat(self):
        d = G.diff_text(self.repo, self.base, exclude_globs=["*.unity"])
        self.assertIn("scene.unity", d)

    def test_the_code_being_judged_survives(self):
        d = G.diff_text(self.repo, self.base, exclude_globs=["*.unity"])
        self.assertIn("void B()", d)

    def test_the_reader_is_told_what_was_withheld(self):
        d = G.diff_text(self.repo, self.base, exclude_globs=["*.unity"])
        self.assertIn("withheld from this diff", d)

    def test_vr_visual_excludes_scenes_by_default(self):
        self.assertIn("*.unity", load_adapter("vr_visual", None).diff_exclude_globs)

    def test_a_project_can_override_the_exclusions(self):
        a = load_adapter("vr_visual", {"diff_exclude_globs": ["*.asset"]})
        self.assertEqual(a.diff_exclude_globs, ["*.asset"])


if __name__ == "__main__":
    unittest.main()
