import unittest

from longrun.evaluator import EvaluatorError, validate_verdict
from longrun.prompts import citation_table

CONTRACT = {
    "criteria": [
        {"id": "C1-visible", "statement": "the derrick reads at a glance", "kind": "visual",
         "evidence_requirements": ["screenshot"], "deterministic_checks": [], "evaluator_policy": "llm_required"},
        {"id": "C2-logged", "statement": "the diary records the cause", "kind": "docs",
         "evidence_requirements": ["doc", "diff"], "deterministic_checks": [], "evaluator_policy": "llm_required"},
    ]
}

MANIFEST = [
    {"id": "Eaaaaaaaaaaaa", "kind": "screenshot", "criterion_ids": ["C1-visible"], "command": None},
    {"id": "Ebbbbbbbbbbbb", "kind": "check", "criterion_ids": ["C2-logged"], "command": "grep -q RECORD log.txt"},
]


def verdict(**crit):
    return {"run_id": "R", "contract_hash": "H", "evaluated_revision": "rev1", "overall": "NEEDS_REWORK",
            "failure_signature": "x", "recommended_next_strategy": "y",
            "criteria": [{"id": k, "verdict": v[0], "evidence_ids": v[1], "reason": "because it is so"}
                         for k, v in crit.items()]}


class CitationTableTest(unittest.TestCase):
    """Two of fifteen verdicts one night were discarded whole on a citation technicality — $10.35 of judgement,
    each also buying a repair round. The rules are mechanical and known to the controller before the session
    starts, so the join belongs in the prompt rather than in the evaluator's head."""

    def test_it_names_the_legal_ids_per_criterion(self):
        t = citation_table(CONTRACT, MANIFEST)
        self.assertIn("C1-visible", t)
        self.assertIn("Eaaaaaaaaaaaa", t)

    def test_it_marks_an_inadmissible_kind(self):
        t = citation_table(CONTRACT, MANIFEST)
        # C2 requires doc/diff; the only record bound to it is a check
        self.assertIn("Ebbbbbbbbbbbb (check INADMISSIBLE)", t)

    def test_it_says_plainly_when_a_criterion_has_nothing(self):
        t = citation_table(CONTRACT, [MANIFEST[0]])
        self.assertIn("nothing on the ledger is bound to this criterion", t)


class BaselineGreenTest(unittest.TestCase):
    """61% of criterion checks over one night's thirteen runs were already green at the frozen revision, before
    any edit, and two runs began with every check passing. The evaluator could not see this: its manifest is
    filtered to the current revision, so a PASS could rest entirely on a check that predated the run."""

    def test_pass_on_a_baseline_green_check_alone_is_rejected(self):
        m = [dict(MANIFEST[1], kind="doc")]          # admissible kind, but the command was green at baseline
        with self.assertRaises(EvaluatorError) as cm:
            validate_verdict(verdict(**{"C1-visible": ("INSUFFICIENT_EVIDENCE", []),
                                        "C2-logged": ("PASS", ["Ebbbbbbbbbbbb"])}),
                             run_id="R", contract_hash="H", evaluated_revision="rev1",
                             contract=CONTRACT, evidence_manifest=[MANIFEST[0]] + m,
                             baseline_green_commands={"grep -q RECORD log.txt"})
        self.assertIn("already passed at the frozen baseline", str(cm.exception))

    def test_the_same_check_passes_when_a_fresh_artifact_is_cited_too(self):
        m = [dict(MANIFEST[1], kind="doc"),
             {"id": "Ecccccccccccc", "kind": "diff", "criterion_ids": ["C2-logged"], "command": None}]
        v = validate_verdict(verdict(**{"C1-visible": ("INSUFFICIENT_EVIDENCE", []),
                                        "C2-logged": ("PASS", ["Ebbbbbbbbbbbb", "Ecccccccccccc"])}),
                             run_id="R", contract_hash="H", evaluated_revision="rev1",
                             contract=CONTRACT, evidence_manifest=[MANIFEST[0]] + m,
                             baseline_green_commands={"grep -q RECORD log.txt"})
        self.assertEqual([c["verdict"] for c in v["criteria"] if c["id"] == "C2-logged"], ["PASS"])

    def test_a_check_that_was_red_at_baseline_stands_on_its_own(self):
        m = [dict(MANIFEST[1], kind="doc")]
        v = validate_verdict(verdict(**{"C1-visible": ("INSUFFICIENT_EVIDENCE", []),
                                        "C2-logged": ("PASS", ["Ebbbbbbbbbbbb"])}),
                             run_id="R", contract_hash="H", evaluated_revision="rev1",
                             contract=CONTRACT, evidence_manifest=[MANIFEST[0]] + m,
                             baseline_green_commands=set())
        self.assertEqual([c["verdict"] for c in v["criteria"] if c["id"] == "C2-logged"], ["PASS"])

    def test_absent_baseline_information_changes_nothing(self):
        m = [dict(MANIFEST[1], kind="doc")]
        v = validate_verdict(verdict(**{"C1-visible": ("INSUFFICIENT_EVIDENCE", []),
                                        "C2-logged": ("PASS", ["Ebbbbbbbbbbbb"])}),
                             run_id="R", contract_hash="H", evaluated_revision="rev1",
                             contract=CONTRACT, evidence_manifest=[MANIFEST[0]] + m)
        self.assertEqual(v["overall"], "NEEDS_REWORK")


if __name__ == "__main__":
    unittest.main()
