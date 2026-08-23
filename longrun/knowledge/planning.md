# Planning a macro outcome
- State the observable end state a stranger could check without reading the code.
- 2–6 criteria. Each: statement, kind (functional|user_facing|visual|player_facing|docs|data), evidence kinds, deterministic checks where possible, evaluator_policy.
- Non-goals and constraints prevent scope creep; allowed_replace_remove lists what the builder may delete/replace.
- Budgets: wall time, child timeout, rounds<=6, repairs<=2, fresh restarts<=1.
- Work-package floor: every builder task links to a criterion, required evidence, a demonstrated blocker, or a regression from this run.

## Independent owner-intent review before freeze

`longrun go` gives the verbatim owner goal and the proposed contract to a fresh read-only strategic session before any builder starts.

- `REJECT` means the contract diluted, omitted, or substituted a material owner instruction. The planner gets the concrete mismatch and may correct its draft within the bounded planning attempts.
- `OWNER_CONFIRMATION_REQUIRED` challenges the owner instruction itself and pauses the run. This is allowed only at confidence `>= 0.90`, for a concrete material harm to correctness, safety, legal/platform feasibility, material cost, or the owner's core product goal, with at least two independent authoritative sources and precise locators. Taste, generic best-practice advice, reversible experiments, and ordinary tradeoffs do not qualify.
- The owner remains final authority. A supervising client that receives a reaffirmation relaunches with `OWNER REAFFIRMED AFTER REVIEW [objection_key]`; the reviewer must not raise the same instruction/reason again or evade the override by renaming the key.

The pause happens before contract freeze and build work. Its structured evidence is retained in `owner-confirmation-required.json` so the owner can correct the goal or knowingly reaffirm it without losing the reason for the interruption.
