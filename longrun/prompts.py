"""Compact role prompts. Detailed rules are routed by phase from knowledge/ — never injected wholesale."""
from __future__ import annotations
import json
from pathlib import Path

from .contract import contract_summary
from .planner import default_hints

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"


def knowledge_ref(phase: str) -> str:
    p = KNOWLEDGE_DIR / f"{phase}.md"
    return f"(Detailed rules for this phase, read only if needed: {p})" if p.is_file() else ""


def builder_prompt(*, contract: dict, state: dict, round_no: int, is_repair: bool, findings: list[dict],
                   capsule: dict | None, adapter_fragment: str, workspace: Path, run_id: str,
                   changed_strategy_required: bool, next_strategy: str | None = None) -> str:
    from .config import load as load_owner_config
    from .swarm import config_from_contract, prompt_fragment as swarm_prompt_fragment
    crit_status = {k: v.get("status") for k, v in state.get("criteria", {}).items()}
    owner_policy = str(load_owner_config().get("owner_policy") or "").strip()
    open_ids = [c["id"] for c in contract["criteria"] if crit_status.get(c["id"]) != "PASS"]
    if state.get("isolation") == "worktree":
        commit_policy = (
            "- Workspace policy: this run uses an isolated worktree. Commit the run's work when a coherent step is "
            "done (`git add -A && git commit -m ...`); never push or touch other branches."
        )
    else:
        commit_policy = (
            "- Workspace policy: this run works in place and may contain pre-existing owner changes. Preserve them. "
            "Do not stage or commit anything, and never run `git add -A`; never push or touch other branches."
        )
    L = [
        f"You are the BUILDER for longrun run {run_id[:8]}, round {round_no}{' (targeted repair)' if is_repair else ''}. "
        f"Workspace: {workspace}. Work only inside it.",
        "",
        contract_summary(contract),
        f"Current status: {json.dumps(crit_status)}. Open criteria: {open_ids}.",
        "",
        "How this works:",
        "- One coherent outcome per run. Move the open criteria; do nothing that lacks a causal link to a criterion, "
        "required evidence, a demonstrated blocker, or a regression this run introduced. No standalone cleanup/refactor/docs/tooling.",
        "- The contract is the task, not a starting point for a new review. For an implementation outcome, inspect only far enough "
        "to choose the smallest causal change, then build it. Keep verified facts, hypotheses, and recommendations distinct; never "
        "promote your own recommendation into a new requirement, priority, gate, or process change.",
        "- If investigation exposes a rejected or dead route, remove it from the active path when a criterion requires that; do not "
        "polish or generalise the dead route unless the contract explicitly asks for it. Record useful non-blocking observations with "
        "`longrun observe --note '...'` and continue the accepted outcome.",
        "- You cannot mark anything done or PASS. Submit candidate evidence and end the session; a fresh, independent evaluator "
        "launched by the controller decides per criterion.",
        "- Evidence: `longrun evidence submit --criterion <id> --kind <check|test|build|log|screenshot|video|capture_manifest|http|metric|artifact|doc> "
        "--summary '<what it shows>' [--cmd '<exact command>' --exit <code>] [--artifact <path>]...`  Submit the exact commands you ran with exit codes. "
        "Evidence is bound to the current revision; if you edit again afterwards, resubmit.",
        "- Within 24 model-controlled turns, produce a new controller-hashed artifact/capture bound to an open criterion, or record the exact blocker and end the session. Submit evidence immediately after each finished batch checkpoint; do not save all evidence work for the end. A foreground Unity/GPU/download command may be silent until it exits and is governed by its child timeout, not by a fake wall-clock progress signal.",
        "- Provider readiness is a bounded operation: at most three checks and ten minutes total for one offer. Then destroy/release the owned resource and try at most one materially different eligible offer. A second readiness failure is a blocker for this batch; do not spend the wait reading unrelated subsystems.",
        "- Blockers: `longrun observe --blocker '<what blocks, with proof (command+output)>'`. Observations: `longrun observe --note '...'`.",
        commit_policy,
        "- Never edit anything under ~/.local/share/longrun. Never claim completion in prose; the ledger is the only channel.",
        "- Narrate for the owner, who reads your session live: before each burst of tool calls write ONE short sentence in plain language — what you are about to do and why; after a result that changes your plan, one sentence on what you learned. No essays, no status theatre; just enough that a human following along understands your reasoning.",
        "- Nobody will answer questions during this run. Do not ask; decide with the project rules and knowledge base, record the choice, and continue. Only an irreducible owner-only choice goes into the project's owner-questions section — and even then keep working on everything else.",
        "- Treat one SSH reset, refused proxy, timeout, or transient HTTP failure as recoverable. Retry only inside the numeric readiness bound above; never start a detached local poller or daemon. Longrun removes every process carrying the session marker when the session ends. The sole exception is one repository-owned, receipt-bound, ownership-scoped deadline/cost watchdog for a paid resource: launch it in a new process session and explicitly remove only the cleanup marker from that child's environment (for example `env -u LONGRUN_SESSION_MARKER nohup setsid ...`). Both steps are required: `setsid` alone survives process-group cleanup but is still terminated by marker cleanup. Record its PID and initial receipt before payload work. No other detached process is allowed.",
    ]
    if owner_policy:
        L += ["", "Persistent owner policy (explicit authority; it overrides stale worklog assumptions about what is out of scope):", owner_policy]
    hints = [h for h in default_hints(workspace) if h != "the repository top level"]
    if hints:
        L += ["", "Project rules and knowledge base (read the priority rule before treating any fact there as a constraint; "
                  "these outrank your priors about the domain): " + ", ".join(hints)]
    if adapter_fragment:
        L += ["", "Domain notes: " + adapter_fragment]
    swarm_fragment = swarm_prompt_fragment(config_from_contract(contract))
    if swarm_fragment:
        L += ["", "Prompt-driven OpenCode swarm protocol:", swarm_fragment]
    if findings:
        L += ["", "Evaluator findings from the last evaluation (fix these; do not argue with them in prose):"]
        for f in findings[:12]:
            L.append(f"- {f.get('id')}: {f.get('verdict')} — {f.get('reason', '')[:400]}")
    if next_strategy:
        L += ["", "The evaluator's own recommendation for this round (it judged the last one and saw the evidence; "
                  "it is not an order, but do not silently repeat what it says failed):", f"  {next_strategy[:2000]}"]
    if changed_strategy_required:
        L += ["", "STRATEGY CHANGE REQUIRED: the previous approach stagnated (same failure signature). Before editing, write one line via "
              "`longrun observe --note 'HYPOTHESIS: <new hypothesis, materially different from: ...>'` and pursue that. Repeating the prior approach counts as stagnation."]
    if capsule:
        L += ["", "Failure capsule from the interrupted trajectory (compact; the full transcript is intentionally withheld):",
              json.dumps(capsule, indent=1)[:6000]]
    L += ["", knowledge_ref("builder")]
    return "\n".join(x for x in L if x is not None)


def citation_table(contract: dict, evidence_manifest: list[dict]) -> str:
    """Per criterion, the evidence ids it may legally cite and which of them are of an admissible kind.

    The validator enforces both rules (evaluator.py: cited evidence must be bound to the criterion, and the
    cited kinds must intersect its evidence_requirements), but the prompt used to hand over a flat manifest and
    ask the evaluator to do the join itself, in JSON, in one shot. Two verdicts out of fifteen were thrown away
    whole on that join — $10.35 of judgement discarded on a citation technicality, each also buying a repair
    round. Doing the join here costs nothing and removes the failure mode."""
    L = []
    for x in contract["criteria"]:
        req = set(x.get("evidence_requirements") or [])
        legal = [e for e in evidence_manifest if x["id"] in e["criterion_ids"]]
        if not legal:
            L.append(f"  {x['id']}: nothing on the ledger is bound to this criterion — it cannot PASS.")
            continue
        marks = [f"{e['id']} ({e['kind']} {'OK' if e['kind'] in req else 'INADMISSIBLE'})" for e in legal]
        L.append(f"  {x['id']} needs one of {sorted(req)} — may cite: " + ", ".join(marks))
    return ("Admissible citations (computed by the controller; citing anything else invalidates the whole verdict, "
            "and a PASS must include at least one OK-marked id):\n" + "\n".join(L))


def evaluator_prompt(*, contract: dict, contract_hash: str, run_id: str, revision: str, baseline: dict,
                     evidence_manifest: list[dict], diff: str, adapter_fragment: str, workspace: Path,
                     deterministic_results: list[dict], standing_results: list[dict] | None = None,
                     referents: list[dict] | None = None, retry_error: str | None = None) -> str:
    L = [
        f"You are the INDEPENDENT EVALUATOR for longrun run {run_id}. You are read-only. You have not seen the builder's "
        f"session and you owe it nothing. Judge evidence, not narrative.",
        "",
        contract_summary(contract),
        f"contract_hash: {contract_hash}",
        f"evaluated_revision: {revision}",
        f"workspace (read-only): {workspace}",
        f"baseline: {json.dumps(baseline)[:1500]}",
        "",
        "Evidence manifest (only current-revision, current-contract records; cite by id). Cite ONLY ids listed here — records you may find on disk under evidence/ that are not in this list are stale (older revision or contract) and citing them invalidates your whole verdict:",
        json.dumps(evidence_manifest, indent=1)[:40000],
        "",
        citation_table(contract, evidence_manifest),
        "",
        "Deterministic check results run by the controller. `at_baseline` is the same command's result at the frozen "
        "revision, before the builder touched anything — a check that was already passing then proves nothing about "
        "this round's work:",
        json.dumps(deterministic_results, indent=1)[:12000],
        "",
        "Diff vs baseline (may be truncated):", diff[:60000],
        "",
        "Rules:",
        "- Every criterion starts FAIL. PASS only when fresh evidence bound to THAT criterion, at THIS revision, demonstrates the statement. "
        "Cite evidence ids that are linked to the criterion; unrelated or aggregate evidence cannot close a criterion.",
        "- A check that was already green at the frozen baseline cannot be the sole support for a PASS: it shows the state "
        "of the project before this run, not what the run achieved. Cite a fresh artifact alongside it.",
        "- user_facing/visual/player_facing criteria cannot PASS on tests/checks alone; they need the required evidence kind (open screenshots/artifacts and look).",
        "- INSUFFICIENT_EVIDENCE when the claim may be true but nothing on the ledger shows it. OWNER_JUDGMENT_REQUIRED only for irreducibly subjective calls the contract assigns to the owner.",
        "- Verify cheaply where you can (open files, run the read-only commands you are allowed). Do not trust summaries. Do not write files.",
        "- failure_signature: a short stable label for the main reason the outcome is not met (same wording if the same problem recurs). recommended_next_strategy: concrete, one paragraph.",
        "- While you inspect, narrate briefly for the owner (one plain sentence before each burst of tool calls: what you are checking and why). The FINAL message must be exactly one JSON object matching the schema; no prose after it.",
    ]
    if referents:
        L += ["", "Referent check, computed by the controller: for every literal a criterion quotes, the tracked "
                  "files that contain it. A literal that resolves nowhere, or only in a log or in the single line "
                  "that writes that log, names nothing in the product — a criterion about such a thing has not been "
                  "met however cleanly the run reports it. This is context, not a verdict; a name built at runtime "
                  "can legitimately resolve nowhere.", json.dumps(referents, indent=1)[:6000]]
    if standing_results:
        L += ["", "The project's standing regression suite, run by the controller. These are NOT criteria — do not "
                  "spend a verdict on them; they are context for whether the outcome was reached without breaking "
                  "what already worked:", json.dumps(standing_results, indent=1)[:6000]]
    hints = [h for h in default_hints(workspace) if h != "the repository top level"]
    if hints:
        L += ["", "The project's own rules and craft knowledge, for HOW to look — not for what to require: " + ", ".join(hints) +
                  ". The contract remains the only standard; these do not add requirements to it. Use them where a judgement is "
                  "about craft (what makes a frame read, what a material or a silhouette should do) so the verdict rests on the "
                  "project's knowledge rather than on your priors, and name the card or rule you used."]
    if adapter_fragment:
        L += ["", "Domain notes: " + adapter_fragment]
    if retry_error:
        L += ["", "YOUR PREVIOUS VERDICT WAS REJECTED BY THE VALIDATOR AND DISCARDED:", f"  {retry_error}",
              "Return the same judgement, corrected only in its citations. Do not change any criterion's verdict "
              "to something more favourable than you first concluded; if a criterion has no admissible evidence, "
              "the honest answer is INSUFFICIENT_EVIDENCE, not a different citation."]
    L += ["", knowledge_ref("evaluator")]
    return "\n".join(L)


RESTART_DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "rationale", "keep_paths", "discard_paths", "new_hypothesis"],
    "properties": {"decision": {"type": "string", "enum": ["APPLY", "PARTIALLY_APPLY", "DISCARD"]},
                   "rationale": {"type": "string"}, "keep_paths": {"type": "array", "items": {"type": "string"}},
                   "discard_paths": {"type": "array", "items": {"type": "string"}}, "new_hypothesis": {"type": "string"}},
}


def restart_manager_prompt(*, contract: dict, capsule: dict, diff_stat: str, workspace: Path) -> str:
    return "\n".join([
        "You are the FRESH-CONTEXT RESTART MANAGER. A prior trajectory on this outcome stagnated and was terminated. "
        "You see only the immutable contract, a compact failure capsule and the diff summary — never the transcript.",
        "", contract_summary(contract), "", "Failure capsule:", json.dumps(capsule, indent=1)[:8000], "",
        "Diff stat of the interrupted work vs the run start revision:", diff_stat[:6000], "",
        f"Workspace (read-only for you): {workspace}",
        "Decide: APPLY (keep the interrupted diff and continue), PARTIALLY_APPLY (keep only keep_paths, discard discard_paths), "
        "or DISCARD (reset to the start revision). Give a materially new hypothesis for the next builder. Return exactly one JSON object.",
        knowledge_ref("restart"),
    ])
