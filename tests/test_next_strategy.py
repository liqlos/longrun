from pathlib import Path

CONTRACT = {"outcome_id": "o", "contract_version": 1, "adapter": "software", "observable_end_state": "x" * 30,
            "criteria": [{"id": "C1-a", "statement": "s", "kind": "functional", "evidence_requirements": ["check"],
                          "deterministic_checks": [], "evaluator_policy": "llm_required"}],
            "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
            "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800, "max_rounds": 3,
                        "max_repairs": 1, "max_fresh_restarts": 1}, "adapter_config": {}}
KW = dict(contract=CONTRACT, state={"criteria": {}}, round_no=2, is_repair=True, findings=[], capsule=None,
          adapter_fragment="", workspace=Path("/w"), run_id="r" * 8, changed_strategy_required=False)


def test_evaluator_recommendation_reaches_the_next_builder_round():
    from longrun.prompts import builder_prompt
    assert "evaluator's own recommendation" not in builder_prompt(**KW)
    txt = builder_prompt(**KW, next_strategy="Photograph the feel layer before adding more logging fields.")
    assert "Photograph the feel layer before adding more logging fields." in txt
    assert "do not silently repeat what it says failed" in txt


def test_recommendation_is_truncated_not_dropped():
    from longrun.prompts import builder_prompt
    txt = builder_prompt(**KW, next_strategy="z" * 5000)
    assert "z" * 2000 in txt and "z" * 2001 not in txt
