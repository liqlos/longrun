---
name: longrun
description: Run a long autonomous development task through the global longrun harness. Use whenever the user asks to "continue the project" / "продолжай (проект|разработка)" / "работай дальше сам" / "доделай до конца" / "ship|finish|implement X autonomously" / "work for N hours", or otherwise asks for multi-hour unattended progress on a repository. Do NOT use for ordinary questions, single edits, reviews, or anything answerable in one turn.
---

# longrun — one sentence in, bounded autonomous run out

The harness is global (`~/.local/bin/longrun`) and already configured by the owner: builder runs with `bypassPermissions`, model and driver routing are resolved from the project's `.longrun/config.json` plus `~/.local/share/longrun/models.json`, and isolation is chosen automatically (worktree if the repo is clean, in place if it is dirty). **Do not hard-code an old model name; verify the effective route with `longrun models --driver <driver>`. The user should never have to pass flags or answer small questions.**

## What to do when the request matches

1. Identify the repository (cwd or the project the user names) and, if the repo has product-specific rules (`AGENTS.md`, `CLAUDE.md`, `.longrun/config.json`), read them — the planner will too.
2. Run, from the repo root, in the background and keep the session responsive:
   ```
   nohup longrun go --project . --goal "<the user's request, verbatim, plus the product name if the repo has several>" --chain 3 >> longrun.out 2>&1 &
   ```
   Use `nohup … &` for anything meant to outlive the terminal (an overnight `--chain 0`): the current controller preserves `nohup`'s ignored SIGHUP, while an attached run still shuts down cleanly when its terminal closes. Plain `longrun go …` is fine only while you sit and watch it.
   `go` = a fresh strategic planner writes ONE reviewable production batch (one observable stage boundary, one reality test, 1–4 externally checkable criteria, later required stages explicitly deferred) → independent owner-intent review → baseline frozen before any edit → builder rounds → independent read-only evaluator → bounded repairs/restart → terminal status. The effective strategic model comes from `longrun models`, not this skill. `--chain N` (0 = unlimited, until `longrun stop`) advances after PASSED/PARTIAL_PASS; after a bounded failure (FAILED/RESET_RECOMMENDED/BLOCKED/BUDGET_EXHAUSTED) it continues exactly once with a changed approach — a second consecutive failure or an immediate repeat of the same failure signature honestly stops the chain instead of replanning the same thing. STOPPED/INTERRUPTED/OWNER_JUDGMENT_REQUIRED always stop it.
3. Watch with `longrun status --run <id>` (`longrun runs` lists ids); `longrun watch -q` run from the project directory follows the whole chain live, outcome after outcome, without an id; `longrun progress --run <id>` replays one outcome in readable prose afterwards. Do not nest `/goal`, ralph, shell loops or a second orchestrator around it — a `/goal` stop-condition such as "работай много часов" can never be satisfied by one turn, so its Stop hook rejects the end of every turn and cancels the wakeup just scheduled: measured on 2026-08-19, a session asked for 1500s and was re-invoked 9–14s later, ~46 full-context turns in 16 minutes while the harness itself sat idle in planning. Do not judge captures/results yourself; do not "help" the builder from this session.
4. When it ends, report: terminal status per run, criteria PASS/FAIL with the evaluator's reasons, artifacts/captures to look at (paths), what landed in the project's owner-questions section, and anything the harness itself did wrong. Then stop.
