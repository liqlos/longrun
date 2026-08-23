import tempfile
import unittest
from pathlib import Path


class StartupBatchPolicyTest(unittest.TestCase):
    def test_cumulative_delta_gate_separates_baseline_resubmit_from_late_evidence_repair(self):
        from longrun.controller import _has_cumulative_run_delta

        self.assertFalse(_has_cumulative_run_delta("frozen+owner-dirty", "frozen+owner-dirty"))
        self.assertTrue(_has_cumulative_run_delta("frozen+run-artifact", "frozen+owner-dirty"))

    def test_contract_schema_accepts_one_reviewable_criterion(self):
        from longrun.planner import CONTRACT_SPEC_SCHEMA

        criteria = CONTRACT_SPEC_SCHEMA["properties"]["criteria"]
        self.assertEqual(criteria["minItems"], 1)
        self.assertEqual(criteria["maxItems"], 4)

    def test_every_codex_output_schema_is_strict_recursively(self):
        from longrun.drivers.codex import validate_strict_output_schema
        from longrun.evaluator import EVALUATOR_JSON_SCHEMA
        from longrun.planner import CONTRACT_SPEC_SCHEMA, INTENT_REVIEW_SCHEMA
        from longrun.prompts import RESTART_DECISION_SCHEMA

        for schema in (CONTRACT_SPEC_SCHEMA, INTENT_REVIEW_SCHEMA,
                       EVALUATOR_JSON_SCHEMA, RESTART_DECISION_SCHEMA):
            validate_strict_output_schema(schema)

    def test_builder_progress_budget_is_short_and_explicit(self):
        from longrun.contract import DEFAULT_BUDGETS

        self.assertEqual(DEFAULT_BUDGETS["first_progress_deadline_seconds"], 0)
        self.assertEqual(DEFAULT_BUDGETS["max_turns_without_progress"], 24)
        self.assertEqual(DEFAULT_BUDGETS["max_turns_per_session"], 60)
        self.assertEqual(DEFAULT_BUDGETS["max_repairs"], 1)

    def test_planner_and_builder_prompts_enforce_batch_boundaries(self):
        from longrun.contract import DEFAULT_BUDGETS
        from longrun.planner import CONTRACT_SPEC_SCHEMA, intent_review_prompt, planner_prompt
        from longrun.prompts import builder_prompt

        budgets = CONTRACT_SPEC_SCHEMA["properties"]["budgets"]["properties"]
        self.assertIn("first_progress_deadline_seconds", budgets)
        self.assertIn("max_turns_without_progress", budgets)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            planner = planner_prompt(
                goal="continue", project_root=root, adapter_name="software",
                adapter_fragment="", prior_error=None,
                project_hints=["README.md"], run_id="12345678")
            self.assertIn("smallest reviewable production batch", planner)
            self.assertIn("Earlier production stages may be sequential batch outcomes", planner)
            self.assertIn("Device performance or owner-on-device acceptance belongs in a separate outcome", planner)
            contract = {
                "outcome_id": "o", "contract_version": 1, "adapter": "software",
                "title": "t", "observable_end_state": "one reviewable batch exists",
                "batch": {"boundary": "one generated asset exists", "reality_test": "open the generated asset",
                          "estimated_seconds": 1800, "max_foreground_seconds": 1200,
                          "deferred_required_outcomes": []},
                "criteria": [], "constraints": [], "non_goals": [],
                "allowed_replace_remove": [], "allowed_commands": [],
                "budgets": DEFAULT_BUDGETS, "adapter_config": {},
            }
            builder = builder_prompt(
                contract=contract, state={"criteria": {}}, round_no=1,
                is_repair=False, findings=[], capsule=None, adapter_fragment="",
                workspace=root, run_id="12345678", changed_strategy_required=False)
            self.assertIn("Within 24 model-controlled turns", builder)
            self.assertIn("foreground Unity/GPU/download command may be silent", builder)
            self.assertIn("at most three checks and ten minutes total for one offer", builder)
            self.assertIn("never start a detached local poller or daemon", builder)
            self.assertIn("removes every process carrying the session marker", builder)
            reviewer = intent_review_prompt(
                goal="continue", spec=contract, project_root=root,
                chain_context={"continues_after_pass": True})
            self.assertIn("REJECT a roadmap-shaped contract", reviewer)
            self.assertIn("30–120 minutes", reviewer)

    def test_progress_deadline_fires_on_time_or_turns(self):
        from longrun.controller import _progress_deadline_reason

        self.assertIsNone(_progress_deadline_reason(
            elapsed_s=899, turns_without_progress=23, deadline_s=900, turn_limit=24))
        self.assertIn("24 turns", _progress_deadline_reason(
            elapsed_s=10, turns_without_progress=24, deadline_s=900, turn_limit=24))
        self.assertIn("900 seconds", _progress_deadline_reason(
            elapsed_s=900, turns_without_progress=2, deadline_s=900, turn_limit=24))

    def test_progress_fingerprint_rejects_ledger_churn_and_failed_checks(self):
        from longrun.controller import _evidence_progress_fingerprint

        artifact = {
            "id": "E1", "recorded_at": "first", "summary": "first wording",
            "kind": "artifact", "criterion_ids": ["C1"], "revision": "abc",
            "artifacts": [{"path": "/a", "copy": "/copy/a", "sha256": "1" * 64}],
            "data": {}, "stdout_sha256": None, "command": None, "exit_code": None,
        }
        duplicate = {
            **artifact, "id": "E2", "recorded_at": "later", "summary": "new wording",
            "criterion_ids": ["C2"],
            "artifacts": [{"path": "/renamed", "copy": "/copy/b", "sha256": "1" * 64}],
        }
        changed = {
            **duplicate,
            "artifacts": [{"path": "/renamed", "copy": "/copy/c", "sha256": "2" * 64}],
        }
        self.assertEqual(
            _evidence_progress_fingerprint(artifact),
            _evidence_progress_fingerprint(duplicate))
        self.assertNotEqual(
            _evidence_progress_fingerprint(artifact),
            _evidence_progress_fingerprint(changed))
        self.assertIsNone(_evidence_progress_fingerprint({
            "kind": "artifact", "artifacts": [], "summary": "paper result"}))
        self.assertIsNone(_evidence_progress_fingerprint({
            "kind": "test", "command": "pytest", "exit_code": 1,
            "stdout_sha256": "3" * 64}))

    def test_progress_fingerprint_rejects_self_reported_successful_tests(self):
        from longrun.controller import _evidence_progress_fingerprint

        first = {"kind": "test", "command": "pytest tests/a.py", "exit_code": 0,
                 "stdout_sha256": "a" * 64, "revision": "abc"}
        second = {**first, "command": "pytest tests/b.py"}
        self.assertIsNone(_evidence_progress_fingerprint(first))
        self.assertIsNone(_evidence_progress_fingerprint(second))

    def test_auto_planner_requires_structural_batch_boundary(self):
        from longrun.contract import ContractError
        from longrun.planner import validate_planner_spec

        with self.assertRaisesRegex(ContractError, "structural batch boundary"):
            validate_planner_spec({
                "outcome_id": "feature", "observable_end_state": "a concrete feature is usable now",
                "criteria": [{"id": "C1", "statement": "the feature works", "evaluator_policy": "llm_required"}],
            })

    def test_schema_v2_contract_rejects_missing_batch_and_more_than_four_criteria(self):
        from longrun.contract import ContractError, new_contract

        criterion = {"id": "C1", "statement": "one concrete result exists", "kind": "functional",
                     "evidence_requirements": ["artifact"], "deterministic_checks": [],
                     "evaluator_policy": "llm_required"}
        with self.assertRaisesRegex(ContractError, "batch is required"):
            new_contract(run_id="r", project_root="/tmp", adapter="software",
                         observable_end_state="one concrete result exists now", criteria=[criterion])
        batch = {"boundary": "one implementation stage", "reality_test": "open the resulting artifact",
                 "estimated_seconds": 600, "max_foreground_seconds": 60,
                 "deferred_required_outcomes": []}
        many = [{**criterion, "id": f"C{i}"} for i in range(1, 6)]
        with self.assertRaisesRegex(ContractError, "at most four criteria"):
            new_contract(run_id="r", project_root="/tmp", adapter="software",
                         observable_end_state="one concrete result exists now", criteria=many,
                         batch=batch, budgets={"child_timeout_seconds": 60})

    def test_batch_runtime_summary_exposes_boundary_reality_and_deferred_scope(self):
        from longrun.contract import contract_summary, new_contract

        contract = new_contract(
            run_id="r", project_root="/tmp", adapter="software",
            observable_end_state="one concrete result exists now",
            criteria=[{"id": "C1", "statement": "one concrete result exists", "kind": "functional",
                       "evidence_requirements": ["artifact"], "deterministic_checks": [],
                       "evaluator_policy": "llm_required"}],
            batch={"boundary": "raw asset generation complete", "reality_test": "open neutral asset render",
                   "estimated_seconds": 1200, "max_foreground_seconds": 600,
                   "deferred_required_outcomes": ["process the full cohort"]},
            budgets={"child_timeout_seconds": 600})
        summary = contract_summary(contract)
        self.assertIn("Batch boundary: raw asset generation complete", summary)
        self.assertIn("Reality test: open neutral asset render", summary)
        self.assertIn("maximum one foreground operation: 600s", summary)
        self.assertIn("process the full cohort", summary)

    def test_max_foreground_operation_must_fit_child_timeout(self):
        from longrun.contract import ContractError, new_contract

        with self.assertRaisesRegex(ContractError, "must not exceed child_timeout_seconds"):
            new_contract(
                run_id="r", project_root="/tmp", adapter="software",
                observable_end_state="one concrete result exists now",
                criteria=[{"id": "C1", "statement": "one concrete result exists", "kind": "functional",
                           "evidence_requirements": ["artifact"], "deterministic_checks": [],
                           "evaluator_policy": "llm_required"}],
                batch={"boundary": "raw asset generation complete", "reality_test": "open neutral asset render",
                       "estimated_seconds": 1200, "max_foreground_seconds": 601,
                       "deferred_required_outcomes": []},
                budgets={"child_timeout_seconds": 600})

    def test_manifest_hash_ignores_narrative_churn_but_keeps_criterion_links(self):
        from longrun.evidence import manifest_hash

        base = {"id": "E1", "record_dir": "/one", "summary": "first wording",
                "submitted_by": "builder-a", "kind": "artifact", "criterion_ids": ["C1"],
                "revision": "rev", "command": None, "exit_code": None,
                "artifacts": [{"path": "/a", "sha256": "1" * 64}], "stdout_tail": None}
        narrative_churn = {**base, "id": "E2", "record_dir": "/two",
                           "summary": "different wording", "submitted_by": "builder-b",
                           "stdout_tail": "different narrative stdin"}
        relinked = {**narrative_churn, "criterion_ids": ["C2"]}
        first = manifest_hash([base], "diff", "contract", "rev")
        self.assertEqual(first, manifest_hash([narrative_churn], "diff", "contract", "rev"))
        self.assertNotEqual(first, manifest_hash([relinked], "diff", "contract", "rev"))

        log = {**base, "kind": "log", "artifacts": [], "command": None,
               "exit_code": None, "stdout_tail": "first story"}
        rewritten_log = {**log, "id": "E3", "summary": "new story",
                         "command": "printf a different story", "exit_code": 0,
                         "stdout_tail": "different unverified story"}
        self.assertEqual(
            manifest_hash([log], "diff", "contract", "rev"),
            manifest_hash([rewritten_log], "diff", "contract", "rev"))
        red = [{"criterion": "C1", "cmd": "probe", "exit_code": 1,
                "expected_exit": 0, "passed": False, "timed_out": False}]
        green = [{**red[0], "exit_code": 0, "passed": True}]
        self.assertNotEqual(
            manifest_hash([log], "diff", "contract", "rev", deterministic_results=red),
            manifest_hash([rewritten_log], "diff", "contract", "rev", deterministic_results=green))


if __name__ == "__main__":
    unittest.main()
