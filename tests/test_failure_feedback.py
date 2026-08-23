from pathlib import Path


def _batch():
    return {"boundary": "one production stage completes", "reality_test": "exercise that stage end to end",
            "estimated_seconds": 900, "max_foreground_seconds": 60,
            "deferred_required_outcomes": []}


def test_failed_regex_keeps_diagnostic_details(tmp_path):
    from longrun.controller import _run_check

    result = _run_check(
        {
            "cmd": "printf 'PASS baseline safety\\n'",
            "expect_exit": 0,
            "expect_stdout_regex": r"PASS.*baseline.*undisturbed",
            "timeout_seconds": 10,
        },
        tmp_path,
        10,
    )

    assert result["passed"] is False
    assert result["exit_code"] == 0
    assert result["expected_exit"] == 0
    assert result["regex_matched"] is False
    assert result["stdout"] == "PASS baseline safety\n"


def test_prompts_prevent_blind_regex_and_one_shot_network_blockers(tmp_path):
    from longrun.planner import planner_prompt
    from longrun.prompts import builder_prompt

    planner = planner_prompt(
        goal="continue",
        project_root=tmp_path,
        adapter_name="software",
        adapter_fragment="",
        prior_error=None,
        project_hints=["README.md"],
        run_id="12345678",
    )
    assert "Never invent an expect_stdout_regex" in planner
    assert "During narration NEVER emit JSON" in planner
    assert "owner's explicit deliverable" in planner
    assert "fresh explicit owner instruction supersedes" in planner
    assert "explicitly named by the owner is part of the deliverable" in planner
    assert "never weaken it to 'when useful'" in planner
    assert "it cannot satisfy a request for a generated final asset" in planner
    assert "a reference image is not a generated mesh" in planner
    assert "fresh current-revision named-view capture evidence" in planner
    assert "check geometric visibility from that view's real camera transform" in planner
    assert "Never require a low or below-player object in an upward-looking view" in planner
    assert "smallest reviewable production batch" in planner
    assert "Earlier production stages may be sequential batch outcomes" in planner
    assert "Keep a named cohort complete at the boundary where the user experiences it" in planner
    assert "must not claim the later player-visible milestone" in planner
    assert "Device performance or owner-on-device acceptance belongs in a separate outcome" in planner
    assert "rejected visual/reference/generation attempt" in planner
    assert "never for an audit, prompt brief, rejection report, contact sheet" in planner
    assert "explicitly deferred required follow-up outcomes" in planner
    assert "Do not use owner_judgment merely because visual quality is subjective" in planner

    contract = {
        "outcome_id": "o",
        "contract_version": 1,
        "adapter": "software",
        "title": "t",
        "observable_end_state": "the live service works for a stranger",
        "criteria": [],
        "constraints": [],
        "non_goals": [],
        "allowed_replace_remove": [],
        "allowed_commands": [],
        "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 900, "max_rounds": 3,
                    "max_repairs": 2, "max_fresh_restarts": 1},
        "adapter_config": {},
    }
    builder = builder_prompt(
        contract=contract,
        state={"criteria": {}},
        round_no=1,
        is_repair=False,
        findings=[],
        capsule=None,
        adapter_fragment="",
        workspace=Path(tmp_path),
        run_id="12345678",
        changed_strategy_required=False,
    )
    assert "Within 24 model-controlled turns" in builder
    assert "at most three checks and ten minutes total for one offer" in builder
    assert "never start a detached local poller or daemon" in builder
    assert "env -u LONGRUN_SESSION_MARKER nohup setsid" in builder
    assert "`setsid` alone" in builder


def test_intent_review_allows_only_chain_backed_sequential_deferral(tmp_path):
    from longrun.planner import intent_review_prompt

    spec = {"criteria": [], "non_goals": ["Family two is deferred as a required follow-up outcome."]}
    continuing = intent_review_prompt(
        goal="Build family one, then family two", spec=spec, project_root=tmp_path,
        chain_context={"continues_after_pass": True})
    standalone = intent_review_prompt(
        goal="Build family one, then family two", spec=spec, project_root=tmp_path,
        chain_context={"continues_after_pass": False})
    assert "continuing multi-outcome chain" in continuing
    assert "Do NOT reject an explicit later sequential stage merely because it is deferred" in continuing
    assert "no guaranteed later outcome" in standalone


def test_vr_visual_requires_recovery_and_cross_view_identity():
    from longrun.adapters.vr_visual import VrVisualAdapter

    assert "reference -> mesh file -> project import -> live scene/prefab binding" in VrVisualAdapter.builder_guidance
    assert "same material failure signature" in VrVisualAdapter.builder_guidance
    assert "Before spending a Simulator cycle" in VrVisualAdapter.builder_guidance
    assert "repeated placement-by-screenshot is a contract/visibility defect" in VrVisualAdapter.builder_guidance
    assert "provider request and successful result records" in VrVisualAdapter.builder_guidance
    assert "retexture-only result cannot stand in for required generated geometry" in VrVisualAdapter.builder_guidance
    assert "Never replace those with an old deterministic atlas" in VrVisualAdapter.builder_guidance
    assert "representative asset view" in VrVisualAdapter.builder_guidance
    assert "provider-to-engine calibration as an ordering gate" in VrVisualAdapter.builder_guidance
    assert "do not multiply the defect across the scene" in VrVisualAdapter.builder_guidance
    assert "finish and neutral-review the entire named cohort before any Simulator run" in VrVisualAdapter.builder_guidance
    assert "never feed them to reconstruction" in VrVisualAdapter.builder_guidance
    assert "inspect every cited angle" in VrVisualAdapter.evaluator_guidance
    assert "facade-card extrusion" in VrVisualAdapter.evaluator_guidance
    assert "standalone reference, turntable, asset file" in VrVisualAdapter.evaluator_guidance
    assert "do not PASS from filenames or provenance prose alone" in VrVisualAdapter.evaluator_guidance
    assert "Authored proxy geometry does not satisfy required generated geometry" in VrVisualAdapter.evaluator_guidance
    assert "proves presence, not visual improvement" in VrVisualAdapter.evaluator_guidance
    assert "old or generic materials erase the generated look" in VrVisualAdapter.evaluator_guidance
    assert "representative provider-to-engine calibration is absent" in VrVisualAdapter.evaluator_guidance
    assert "representative subset is integrated" in VrVisualAdapter.evaluator_guidance
    assert "must render the downloaded provider bytes without transcoding" in VrVisualAdapter.builder_guidance
    assert "Near-duplicate angles do not supply missing side or back coverage" in VrVisualAdapter.builder_guidance
    assert "Reject a supposed raw/provider comparison if the candidate was transcoded" in VrVisualAdapter.evaluator_guidance


def test_interim_planner_json_is_not_a_contract():
    import pytest
    from longrun.contract import ContractError
    from longrun.planner import validate_planner_spec

    interim = {
        "outcome_id": "planning-in-progress",
        "observable_end_state": "I am reading the backlog to choose the next product outcome.",
        "criteria": [
            {"id": "C1-read", "statement": "Repository rules are being inspected.",
             "kind": "docs", "evaluator_policy": "owner_judgment"},
            {"id": "C2-plan", "statement": "The final response will contain a contract.",
             "kind": "docs", "evaluator_policy": "owner_judgment"},
        ],
    }
    with pytest.raises(ContractError, match="interim progress"):
        validate_planner_spec(interim)


def test_real_product_contract_survives_planner_filter():
    from longrun.planner import validate_planner_spec

    validate_planner_spec({
        "outcome_id": "live-search-restored",
        "observable_end_state": "A stranger receives five date-anchored search results from the live endpoint.",
        "batch": _batch(),
        "criteria": [
            {"id": "C1-live", "statement": "The endpoint answers a health probe and five searches.",
             "kind": "functional", "evaluator_policy": "llm_required"},
        ],
    })


def test_planner_rejects_unittest_filesystem_path_that_cannot_collect():
    import pytest
    from longrun.contract import ContractError
    from longrun.planner import validate_planner_spec

    spec = {
        "outcome_id": "raw-cohort",
        "observable_end_state": "A stranger can inspect the completed raw cohort.",
        "batch": _batch(),
        "criteria": [{
            "id": "C1-tests",
            "statement": "The production runner regression test passes.",
            "kind": "functional",
            "evidence_requirements": ["test"],
            "deterministic_checks": [{
                "cmd": "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest .longrun/pipeline/test_runner.py"
            }],
            "evaluator_policy": "deterministic_only",
        }],
    }

    with pytest.raises(ContractError, match="filesystem path"):
        validate_planner_spec(spec)

    spec["criteria"][0]["deterministic_checks"][0]["cmd"] = (
        "PYTHONDONTWRITEBYTECODE=1 python3 .longrun/pipeline/test_runner.py"
    )
    validate_planner_spec(spec)


def test_mandatory_owner_method_cannot_be_diluted_or_omitted():
    import pytest
    from longrun.contract import ContractError
    from longrun.planner import validate_planner_spec

    base = {
        "outcome_id": "city-assets",
        "observable_end_state": "Four generated city families are visibly integrated in the live game.",
        "batch": _batch(),
        "criteria": [{
            "id": "C1-assets",
            "statement": "Four Blender-authored families are imported; Meshy-7 may be used where it improves them.",
            "kind": "functional",
            "evidence_requirements": ["artifact"],
            "deterministic_checks": [{"cmd": "true"}],
            "evaluator_policy": "deterministic_only",
        }],
    }
    with pytest.raises(ContractError, match="mandatory owner method Meshy-7"):
        validate_planner_spec(base, "MUST USE Meshy-7 for every final family")

    good = dict(base)
    good["criteria"] = [{
        **base["criteria"][0],
        "statement": "Every final family is generated by Meshy-7 and has a provider task request/response receipt plus returned raw mesh hash traced to the imported artifact.",
        "evidence_requirements": ["artifact", "log"],
    }]
    validate_planner_spec(good, "MUST USE Meshy-7 for every final family")
    validate_planner_spec(good, "MUST USE real Meshy-7 generated geometry")
    validate_planner_spec(good, "MUST USE the official Meshy-7 route")


def test_ordinary_goal_does_not_require_provider_provenance():
    from longrun.planner import validate_planner_spec

    validate_planner_spec({
        "outcome_id": "ordinary-feature",
        "observable_end_state": "A stranger can use the completed feature.",
        "batch": _batch(),
        "criteria": [{
            "id": "C1-feature",
            "statement": "The completed feature works end to end.",
            "kind": "functional",
            "evidence_requirements": ["test"],
            "deterministic_checks": [{"cmd": "true"}],
            "evaluator_policy": "deterministic_only",
        }],
    }, "Continue the project")


def test_must_use_one_instance_is_not_parsed_as_provider_one():
    from longrun.planner import validate_planner_spec

    validate_planner_spec({
        "outcome_id": "bounded-rental",
        "observable_end_state": "The benchmark completes on one owned rental.",
        "batch": _batch(),
        "criteria": [{
            "id": "C1-rental",
            "statement": "Exactly one owned A100 instance runs the benchmark and is destroyed afterward.",
            "kind": "functional",
            "evidence_requirements": ["artifact", "log"],
            "deterministic_checks": [{"cmd": "true"}],
            "evaluator_policy": "llm_required",
        }],
    }, "MUST use one new verified A100 instance")


def test_independent_intent_review_rejects_material_dilution():
    import pytest
    from longrun.contract import ContractError
    from longrun.planner import validate_intent_review

    with pytest.raises(ContractError, match="independent owner-intent review rejected"):
        validate_intent_review({
            "verdict": "REJECT",
            "material_mismatches": [{
                "owner_instruction": "MUST USE Meshy-7",
                "contract_effect": "Meshy is optional when useful",
                "reason": "the contract permits Blender-only completion",
            }],
            "owner_objection": None,
            "summary": "mandatory provider was weakened",
        })


def test_independent_intent_review_passes_without_findings():
    from longrun.planner import validate_intent_review

    validate_intent_review({"verdict": "PASS", "material_mismatches": [], "owner_objection": None,
                            "summary": "intent preserved"})


def test_intent_review_unknown_verdict_fails_closed():
    import pytest
    from longrun.contract import ContractError
    from longrun.planner import validate_intent_review

    with pytest.raises(ContractError, match="unknown verdict"):
        validate_intent_review({"verdict": "MAYBE", "material_mismatches": [],
                                "owner_objection": None, "summary": "ambiguous"})


def _owner_objection_review(confidence=0.96):
    return {
        "verdict": "OWNER_CONFIRMATION_REQUIRED", "material_mismatches": [],
        "owner_objection": {
            "objection_key": "unsafe-owner-route", "owner_instruction": "use the unsafe route",
            "conflict": "it conflicts with authoritative requirements",
            "likely_harm": "the result would be materially invalid", "confidence": confidence,
            "sources": [
                {"source_class": "project_knowledge", "title": "Project KB", "locator": "docs/KB.md:12", "support": "requires A"},
                {"source_class": "official_documentation", "title": "Official docs", "locator": "https://example.test/a", "support": "forbids B"},
            ],
            "question": "This is very likely harmful. Do you still want it?",
        }, "summary": "sourced conflict",
    }


def test_high_confidence_sourced_owner_conflict_pauses():
    import pytest
    from longrun.planner import OwnerConfirmationRequired, validate_intent_review

    with pytest.raises(OwnerConfirmationRequired):
        validate_intent_review(_owner_objection_review(), "use the unsafe route")


def test_weak_owner_conflict_cannot_pause():
    import pytest
    from longrun.contract import ContractError
    from longrun.planner import validate_intent_review

    with pytest.raises(ContractError, match="below 0.90"):
        validate_intent_review(_owner_objection_review(0.75), "use the unsafe route")


def test_owner_reaffirmation_suppresses_same_objection():
    from longrun.planner import validate_intent_review

    result = validate_intent_review(
        _owner_objection_review(),
        "use the unsafe route\nOWNER REAFFIRMED AFTER REVIEW [unsafe-owner-route]",
    )
    assert result == "OWNER_OVERRIDE_APPLIED"
