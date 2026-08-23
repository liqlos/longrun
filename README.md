# longrun — Long-Horizon Outcome Harness

Longrun is a local orchestration harness for bounded, multi-hour agent work. It turns an owner goal into one
reviewable production batch, freezes a baseline, runs an implementation agent, and lets an independent evaluator
judge explicit criteria from revision-bound evidence. It supports Claude, Codex, and an OpenCode builder route
without installing global agent hooks.

Longrun is **inactive by default**. Nothing happens in an ordinary Claude Code, Codex, or OpenCode session unless
a shell explicitly starts a Longrun command.

## Install

One script, three platform families, no root requirement unless Python 3.11+ must be installed:

```bash
git clone https://github.com/liqlos/longrun.git ~/.local/share/longrun/src
~/.local/share/longrun/src/install.sh
```

| Platform | Package manager used if Python ≥ 3.11 is missing |
|---|---|
| macOS | Homebrew (`python@3.12`) |
| Debian / Ubuntu (22.04, 24.04, newer) | `apt-get` (`python3 python3-venv python3-pip git`) |
| Fedora / RHEL (and clones) | `dnf` or `yum` |

The installer:

- installs the `longrun` CLI **editable from this checkout** — one source of truth; updates are `git pull` +
  re-running the script. It prefers `uv tool install`, otherwise creates a dedicated venv at
  `~/.local/share/longrun/venv` and links `~/.local/bin/longrun`;
- detects which agent CLIs are present — `claude`, `codex`, `opencode` — and wires the longrun skill into each
  one via the shared canonical copy at `~/.agents/skills/longrun/` (`~/.claude/skills`,
  `~/.codex/skills`, `~/.config/opencode/skills`; XDG-aware). Agents that are not installed are skipped with a
  note; installing an agent later only needs a re-run;
- prints a summary and is idempotent — re-running never duplicates anything.

Versioning: single source in `longrun/__init__.py`, surfaced by `longrun --version`.

Manual alternative:

```bash
uv tool install --editable ~/.local/share/longrun/src   # or: pip install --editable ...
```

## Quick start

```bash
cd /path/to/project
longrun init --project . --adapter software --driver opencode   # or claude | codex
longrun doctor
longrun go --project . --goal "Implement and verify the next production outcome" --chain 3
```

Use `longrun status`, `longrun watch -q`, or `longrun progress --run <id>` to inspect work without attaching a
second orchestrator. The OpenCode route keeps implementation on the configured OpenCode builder while strategic
planning and evaluation remain independently routed through Codex.

Model: **one coherent, externally verifiable macro outcome per autonomous run** — not one micro task per turn,
not an unverified one-shot, not an infinite loop.

## Agent support (verified per CLI)

| | Claude Code | Codex | OpenCode |
|---|---|---|---|
| Skill wiring | `~/.claude/skills/longrun` | `~/.codex/skills/longrun` | `~/.config/opencode/skills/longrun` |
| Builder route | session model (`sonnet` tier default) | `gpt-5.6-terra`, full host access by design | `opencode/x-preview-f-free`, `--pure`, no plugins, harness-scoped deny rules |
| Session-scoped hooks | yes (`--settings`) | none, by design | none |
| Stop nudge on empty session | yes (≤ 2 blocks) | absent — caught a round later | absent — caught a round later |
| Optional background swarm | — | — | opt-in `builder_swarm` (below) |
| Ordinary sessions affected | no | no | no |

Details that differ when the builder driver changes are described under
["What differs when the driver is Codex"](#what-differs-when-the-driver-is-codex); the OpenCode route is covered
in [Roles and models](#roles-and-models-owner-rule-the-expensive-model-for-the-rare-directional-call-never-above-medium-effort).

## Chains (one simple policy)

`--chain N` (0 = unlimited, until `longrun stop`):

- **PASSED / PARTIAL_PASS** always advance; accepted progress also resets the failure budget.
- **Any other bounded failure** (FAILED / RESET_RECOMMENDED / BLOCKED / BUDGET_EXHAUSTED) buys exactly
  **one** continuation with a changed approach for the next outcome. A second consecutive failure, or an
  immediate repeat of the same failure signature, honestly stops the chain instead of replanning.
- **Provider-wide terminal conditions** (weekly quota exhausted, rejected request configuration) stop the
  chain immediately — another outcome cannot repair them.
- **STOPPED / INTERRUPTED / OWNER_JUDGMENT_REQUIRED** always stop.

## Trust boundary (what is mechanically enforced)

| Property | Mechanism |
|---|---|
| Ordinary sessions unaffected | Zero global hooks. Run hooks are passed only via `--settings` to controller-launched sessions; `longrun hook *` exits 0 with no output unless a controller-issued HMAC token (bound to run id, session id, role, controller pid, expiry) is valid **and** the session is a registered live child **and** the controller is alive. |
| Builder cannot certify | No `mark-done`. `longrun evidence submit` records *candidate* evidence only. Criterion transitions are applied by the controller from a validated evaluator object. `longrun evaluate` refuses builder/evaluator tokens. State is HMAC-signed with the run key (0600, denied to sessions); tampering → `FAILED`. |
| Default FAIL, criterion-level truth | Every criterion starts `FAIL`. PASS requires evidence ids **bound to that criterion**, of a **required kind**, at the **evaluated revision**, under the **current contract hash**. Aggregate/unrelated evidence, unknown/duplicate/missing criteria, wrong run/hash/revision, extra keys → whole verdict rejected. `user_facing`/`visual`/`player_facing` cannot pass on tests/checks alone (contract validation refuses such contracts). |
| Baseline before edits | `run` freezes the contract (hash) and captures baseline evidence at the start revision **before** any builder session. HEAD moved since creation → refuse; content changed after a builder has already run → refuse. On an in-place run whose project merely moved under the planner (nothing executed yet, plan under 30 min old) the baseline is re-taken at the freeze point and a `baseline.rebased` event records both revisions. Worktree isolation by default. |
| Bounded | wall time, per-child timeout (process-group kill), rounds, `max_repairs ≤ 2`, `max_fresh_restarts ≤ 1`, Stop-hook blocks per session ≤ 2, turns per session. Then `RESET_RECOMMENDED` / `BLOCKED` / `OWNER_JUDGMENT_REQUIRED`. |
| Loop detection | operational (repeated action/observation, repeated error signatures, edit/revert alternation, edits without evidence — kills the child mid-session) + live progress (within 24 model-controlled turns a new controller-hashed artifact/capture must appear; self-reported tests, duplicate ledger entries, renamed copies and failed checks do not reset it) + strategic (no criterion delta twice, same failure signature twice without new hypothesis, repairs not moving the criterion, doc-only work, spend without evidence). Silent foreground Unity/GPU work is governed by its child timeout rather than a false 15-minute wall signal. |
| State safety | run UUID dirs, `flock` + atomic replace + version + stale-write rejection, monotonic event seq, schema version, signals → `INTERRUPTED`, process-group kill plus continuously observed descendant cleanup and session environment-marker cleanup for `setsid`/daemon escapes, no shared mutable state between runs, second in-place run in a repo refused. |
| No wasted evaluation | evaluation skipped when the canonical (result evidence, diff, revision, contract) hash is unchanged; narrative/submitter churn does not buy another evaluator. A real workspace delta is still evaluated with controller-owned checks when the builder omitted manual evidence. |

## Roles and models (owner rule: the expensive model for the rare directional call, never above medium effort)

Two tiers plus a per-role override, resolved per driver from `~/.local/share/longrun/models.json` (`longrun models [--init] [--driver codex|opencode]`):

| tier | roles | Claude default | Codex default | OpenCode run |
|---|---|---|---|---|
| builder | builder (mechanical implementation) | **sonnet, effort medium** | **gpt-5.6-terra, effort medium** | **opencode/x-preview-f-free** |
| strategic | evaluator (and anything else judging) | **opus, effort medium** | **gpt-5.6-sol, effort medium** | routed to Codex gpt-5.6-sol |
| `roles` | any single role, overriding its tier | **planner and restart manager: fable, effort medium** | — (sol is already the top tier there) | — |
| `strategic_fallback` | any strategic role, on usage limit / overload | fable ↔ opus, automatically each other's failover | none | none |

`--driver opencode` is a compatible volume-worker route: the builder defaults to
`opencode/x-preview-f-free` while planner, evaluator, and restart manager stay on Codex.  It changes no ordinary
OpenCode settings, loads no external plugins (`--pure`), installs no hooks, and the existing `--driver codex`
path remains available unchanged. The non-interactive builder uses `--auto` with harness-scoped deny rules,
but, like the full-host Codex builder, remains a trusted same-user worker rather than a hostile-code sandbox.

The native Claude and Codex routes are tiered the same way and for the same reason — the volume role on the mid tier, judgement on
the flagship. Codex prices at the time of writing: sol $5/$30 per MTok, terra $2/$12, luna $0.20/$1.20. Luna is
latency-optimised for routine drafting and classification, so it is not a candidate for agentic implementation
or for judging; terra is the direct analogue of sonnet and sol of opus.

### Optional prompt-driven OpenCode swarm

A project can opt an OpenCode builder into a bounded background-task swarm without adding a scheduler or global
plugin. Put `builder_swarm` under `.longrun/config.json`'s `adapter_config`:

```json
{
  "builder_swarm": {
    "enabled": true,
    "researchers": 12,
    "workers": 5,
    "task_retries": 3,
    "manager_retries": 3
  }
}
```

The parent OpenCode session remains manager and sole integrator: it chooses the research questions and then the
non-overlapping implementation shards. Longrun only injects scoped read-only researcher and non-nesting worker
profiles and audits actual task events and child session IDs. Longrun enables OpenCode's experimental background-subagent
flag only inside a run-scoped persistent OpenCode server; ordinary user configuration is untouched. The manager fills each
wave with `background=true` task calls, and a launch receipt is never counted as completion. If the
first research wave stays underfilled for six manager turns, or Ox ends cleanly before all configured shard ids
were dispatched or before the exact completion/blocker marker, the harness resumes the same manager when possible
on the same server, names the recorded task IDs, and collects or resumes them before respawning only failed or
unreachable shards. Retries are bounded. Ordinary OpenCode sessions
and projects without this opt-in are unchanged.

The split is by *frequency*, not by whether the call feels strategic. Measured over 57 runs, $1,319: builder 63%, evaluator 22%, planner 14%, restart manager 1%. So the builder is where model choice is worth real money (sonnet is $2/$10 per MTok against opus $5/$25 and fable $10/$50, and its cache reads — 683M tokens, the bulk of its bill — are $0.20 against $0.50 and $1.00). The planner and the restart manager run at most once per outcome and decide direction rather than doing work, which is what makes them worth fable despite fable being the dearest model per token; the evaluator runs once per round, so it stays on opus.

Effort is not the lever it looks like: output tokens are where effort shows up, and they are a few percent of a strategic call — the evaluator's 626k output tokens sit against 92M tokens of context, and 60% of the planner's bill is cache *creation*. Lowering effort buys a worse judge and saves almost nothing, which is why nothing here runs below medium either.

A role entry inherits whatever it does not name from its tier, so `{"planner": {"model": "fable"}}` still runs at the tier's effort rather than at none.

`strategic_fallback` (Claude: **fable, effort medium**) is used only when the primary model reports a usage limit or overload. The two strategic models are each other's failover automatically: a role already running the fallback model fails over to the tier's own model instead of retrying the one that is limited. `longrun models` prints `fallback=none` when a strategic role has nowhere to go.

### What differs when the driver is Codex

The harness is one code path; only `drivers/codex.py` changes. `longrun init --project . --driver codex` (or
`--driver codex` on `go`) is the whole switch. Evidence/state validation is unchanged, while the host-access
threat model differs in two important ways:

* **No hooks anywhere.** Claude children get session-scoped hooks via `--settings`; Codex has none installed,
  by design. Strategic roles run `--sandbox read-only`. The builder deliberately runs
  `--sandbox danger-full-access` because Unity needs per-user licensing state and local sockets outside the
  workspace. It is therefore a trusted owner-authorized worker, not an OS security boundary: prompt denies,
  token checks and signed state prevent accidents but cannot contain a hostile same-user builder. The contract
  summary the SessionStart hook injects is already in the Codex prompt itself.
* **The one real gap is the Stop nudge.** On Claude, a builder session that would end without a single piece
  of evidence or a recorded blocker is blocked (at most twice) and told to file what it has. Codex offers no
  equivalent, so an empty session simply ends; the round-level machinery still catches it —
  `evaluation.skipped_no_evidence`, then the strategic loop guard — but a round later and after the money.
* **Cost reporting.** `codex exec` reports no per-session cost, so `longrun cost` prints `n/a` for Codex runs
  rather than `$0.00`. Token accounting from `turn.completed` is recorded in the event stream.

`strategic_driver: "claude"` in `models.json` routes the strategic roles of *Codex* runs to Claude (cross-vendor judging) — useful precisely because it puts a different vendor's model in the judge's seat. Overrides: `--eval-model` > `LONGRUN_STRATEGIC_MODEL_<CLAUDE|CODEX>` > `LONGRUN_STRATEGIC_MODEL` > the `roles` map > the tier. Note `--eval-model` covers the planner too, so passing it overrides the per-role default. Every `session.launch` event records driver, model, effort and where the choice came from.

## Commands

```
longrun --version                     # single version source: longrun/__init__.py
longrun doctor                       # health + "no global hooks" audit
longrun init --project . --adapter software|api_backend|ui_visual|gameplay|vr_visual|data_research|docs_content|custom --driver claude|codex|opencode
longrun plan --project . --contract contract.json [--isolation worktree|none]
longrun run --run <id> [--model m] [--eval-model m] [--permission-mode acceptEdits|auto|dontAsk|bypassPermissions --allow-bypass]   # strategic roles default to opus (planner: fable); builder = session model
longrun go --project . --goal "..." [--chain N]   # auto-plan -> freeze -> bounded run; chain policy above
longrun cost [--project .] [--run <id-prefix>] [--all] [--no-per-run]   # what runs cost, by role — reporting only, never gates
longrun status --run <id> [--json]      longrun runs      longrun watch [-q]  # live, no id needed: run it in the project dir and it follows the whole chain, outcome after outcome
longrun progress --run <id-prefix> [-n N]   # what a run did, readably, after the fact — rendered from files already on disk, no extra tokens
longrun checkpoint|evaluate --run <id>  # controller-launched fresh evaluator now
longrun stop --run <id>                 # graceful: current child killed, run STOPPED
longrun reset --run <id>                # new UUID child run, clean counters, capsule routed
longrun contract show|rebase --run <id> --changes changes.json --reason "..."
longrun migrate --project <old-autopilot-repo> | --global-bundle
longrun uninstall [--purge]
# inside controller-launched builder sessions only (token required):
longrun evidence submit --criterion C1 --kind check --summary "..." --cmd "pytest -q" --exit 0 [--artifact path]
longrun observe --blocker "..." | --note "HYPOTHESIS: ..."
```

## Runbook

* **Normal question / session** — do nothing. `claude` and `codex` behave exactly as before; `longrun doctor` shows "this shell is inside a run session: no".
* **Initialize a project** — `cd repo && longrun init --project . --adapter software` (writes `.longrun/config.json` + a contract template; activates nothing).
* **Plan one reviewable production batch** — write schema-v2 `contract.json` with a mandatory structural `batch` (`boundary`, one `reality_test`, `estimated_seconds` 300–7200, `max_foreground_seconds` no larger than the child timeout, and `deferred_required_outcomes`) plus 1–4 criteria for that same stage. Sequential stages belong to later chain outcomes; both generated and manually supplied contracts pass the same schema validation and independent intent review. The command prints the frozen-to-be contract; nothing executes.
* **Run** — `longrun run --run <id>`; freezes baseline in a worktree at HEAD, then rounds of builder → deterministic checks → fresh evaluator. Ctrl-C stops the child and marks `INTERRUPTED`.
* **Inspect** — `longrun status --run <id>`; events in `~/.local/share/longrun/runs/<id>/events.jsonl`; evidence in `evidence/`; evaluations in `evaluations/`; sessions' streams in `sessions/`.
* **Evaluate** — `longrun evaluate --run <id>` (owner shell only).
* **Stop / resume / restart** — `longrun stop`; a run in `RUNNING` can be re-entered with `longrun run --run <id>` (children reconciled); after a terminal state use `longrun reset` for a fresh child run.
* **Migrate an old autopilot project** — `longrun migrate --project <repo>` archives `.claude/autopilot`, converts open increments to a **draft** contract for `vr_visual`, carries no counters/baseline/history. Review `.longrun/contract.draft.json`, trim to one outcome, then `plan`.
* **Uninstall and restore** — `longrun uninstall` restores the settings snapshot recorded at install (hash-checked) and removes the tool; `--purge` deletes runs/keys. Backups/archives remain under `~/.local/share/longrun/`.

## Layout
`~/.local/share/longrun/{src,runs/<uuid>,keys,backups,archive}`. Per project: `.longrun/{config.json,project.json,contract.*.json}` (pointer/config only).

Tests: `bash ~/.local/share/longrun/src/run_tests.sh` (non-interference, security/correctness, loop guard, drivers).

## License

MIT — see [LICENSE](LICENSE).
