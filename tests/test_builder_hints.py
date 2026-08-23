from pathlib import Path


def test_builder_prompt_carries_project_knowledge_hints(tmp_path):
    from longrun.prompts import builder_prompt
    (tmp_path / "AGENTS.md").write_text("rules"); (tmp_path / "docs/game-knowledge").mkdir(parents=True)
    (tmp_path / "docs/game-knowledge/INDEX.md").write_text("kb")
    contract = {"outcome_id": "o", "contract_version": 1, "adapter": "software", "observable_end_state": "x" * 30,
                "criteria": [{"id": "C1-a", "statement": "s", "kind": "functional", "evidence_requirements": ["check"],
                              "deterministic_checks": [], "evaluator_policy": "llm_required"}],
                "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
                "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800, "max_rounds": 3, "max_repairs": 1, "max_fresh_restarts": 1}, "adapter_config": {}}
    txt = builder_prompt(contract=contract, state={"criteria": {}}, round_no=1, is_repair=False, findings=[], capsule=None,
                         adapter_fragment="", workspace=tmp_path, run_id="r" * 8, changed_strategy_required=False)
    assert "AGENTS.md" in txt and "docs/game-knowledge/INDEX.md" in txt


def test_builder_prompt_does_not_turn_review_findings_into_new_scope(tmp_path):
    from longrun.prompts import builder_prompt
    contract = {"outcome_id": "o", "contract_version": 1, "adapter": "software", "observable_end_state": "x" * 30,
                "criteria": [{"id": "C1-a", "statement": "implement the accepted outcome", "kind": "functional",
                              "evidence_requirements": ["check"], "deterministic_checks": [],
                              "evaluator_policy": "llm_required"}],
                "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
                "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800, "max_rounds": 3,
                            "max_repairs": 1, "max_fresh_restarts": 1}, "adapter_config": {}}
    txt = builder_prompt(contract=contract, state={"criteria": {}}, round_no=1, is_repair=False, findings=[],
                         capsule=None, adapter_fragment="", workspace=tmp_path, run_id="r" * 8,
                         changed_strategy_required=False)
    assert "contract is the task, not a starting point for a new review" in txt
    assert "never promote your own recommendation into a new requirement" in txt
    assert "do not polish or generalise the dead route" in txt


def test_builder_prompt_commit_policy_follows_workspace_mode(tmp_path):
    from longrun.prompts import builder_prompt
    contract = {"outcome_id": "o", "contract_version": 1, "adapter": "software", "observable_end_state": "x" * 30,
                "criteria": [{"id": "C1-a", "statement": "s", "kind": "functional",
                              "evidence_requirements": ["check"], "deterministic_checks": [],
                              "evaluator_policy": "llm_required"}],
                "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
                "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800, "max_rounds": 3,
                            "max_repairs": 1, "max_fresh_restarts": 1}, "adapter_config": {}}
    common = dict(contract=contract, round_no=1, is_repair=False, findings=[], capsule=None,
                  adapter_fragment="", workspace=tmp_path, run_id="r" * 8, changed_strategy_required=False)

    isolated = builder_prompt(state={"criteria": {}, "isolation": "worktree"}, **common)
    in_place = builder_prompt(state={"criteria": {}, "isolation": "none"}, **common)

    assert "isolated worktree" in isolated and "git add -A && git commit" in isolated
    assert "works in place" in in_place and "Do not stage or commit anything" in in_place
    assert "never run `git add -A`" in in_place
    assert "git add -A && git commit" not in in_place


def test_evaluator_prompt_grounds_craft_judgement_in_the_project_knowledge(tmp_path):
    from longrun.prompts import evaluator_prompt
    (tmp_path / "AGENTS.md").write_text("rules"); (tmp_path / "docs/game-knowledge").mkdir(parents=True)
    (tmp_path / "docs/game-knowledge/INDEX.md").write_text("kb")
    contract = {"outcome_id": "o", "contract_version": 1, "adapter": "software", "observable_end_state": "x" * 30,
                "criteria": [{"id": "C1-a", "statement": "s", "kind": "visual", "evidence_requirements": ["screenshot"],
                              "deterministic_checks": [], "evaluator_policy": "llm_required"}],
                "constraints": [], "non_goals": [], "allowed_replace_remove": [], "allowed_commands": [],
                "budgets": {"wall_time_seconds": 3600, "child_timeout_seconds": 1800, "max_rounds": 3, "max_repairs": 1, "max_fresh_restarts": 1},
                "adapter_config": {}}
    txt = evaluator_prompt(contract=contract, contract_hash="h" * 8, run_id="r" * 8, revision="rev", baseline={},
                           evidence_manifest=[], diff="", adapter_fragment="", workspace=tmp_path, deterministic_results=[])
    assert "docs/game-knowledge/INDEX.md" in txt
    # the knowledge informs how to look; it must not become a second standard beside the contract
    assert "not for what to require" in txt and "contract remains the only standard" in txt
