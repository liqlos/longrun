"""Role → (driver, model, effort) resolution. Two tiers: builder (mechanical) and strategic
(evaluator, restart manager, planner). Strategic roles run on a judgement model per driver;
they may also run on a different driver than the builder (cross-vendor judging).

A `roles` map overrides the tier for one named role. It exists because "strategic" turned out to mix
two very different frequencies: the planner runs once per outcome and decides what the run is even
about, while the evaluator runs once per round and judges evidence against an already-written
contract. Measured over 35 runs on one project, the strategic tier was 45% of the bill and most of
that was the evaluator — so the owner's rule became "the rare directional call gets the expensive
model, the frequent judgement call does not". The map is how that is expressed without inventing a
third tier.

Resolution order for a strategic role: CLI --eval-model > env LONGRUN_STRATEGIC_MODEL_<DRIVER> >
env LONGRUN_STRATEGIC_MODEL > roles map > ~/.local/share/longrun/models.json > built-in defaults.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

from .paths import data_root

STRATEGIC_ROLES = {"evaluator", "restart_manager", "planner", "contract_repair", "intent_reviewer"}

DEFAULTS = {
    # Claude: builder = session default model at medium effort; strategic = opus/medium; the planner alone
    # keeps fable (owner rule: fable for the rare directional call, never above medium effort anywhere).
    "claude": {"builder": {"model": "sonnet", "effort": "medium"}, "strategic": {"model": "opus", "effort": "medium"},
               # used for a strategic role only when the primary strategic model is unavailable (usage limit / overload)
               "strategic_fallback": {"model": "fable", "effort": "medium"},
               "roles": {"planner": {"model": "fable", "effort": "medium"},
                         "restart_manager": {"model": "fable", "effort": "medium"}}},
    # Codex, same shape as Claude and chosen the same way — the volume role on the mid tier, judgement on the
    # flagship: builder = gpt-5.6-terra ($2/$12 per MTok), strategic = gpt-5.6-sol ($5/$30). SOL is an
    # explicit quality choice and therefore has no cross-model fallback. gpt-5.6-luna ($0.20/$1.20) is
    # latency-optimised for routine drafting/classification and is not a
    # candidate for either agentic implementation or judging.
    "codex": {"builder": {"model": "gpt-5.6-terra", "effort": "medium"},
              "strategic": {"model": "gpt-5.6-sol", "effort": "medium"},
              "strategic_fallback": None, "roles": {}},
    # OpenCode is a volume-worker route. Ox Alpha is temporarily free, while
    # planning and evaluation remain on Codex through strategic_driver_by_run.
    "opencode": {"builder": {"model": "opencode/x-preview-f-free", "effort": None},
                 "strategic": {"model": "opencode/x-preview-f-free", "effort": None},
                 "strategic_fallback": None, "roles": {}},
    # If set, strategic roles run on this driver regardless of the run's builder driver (e.g. "claude").
    "strategic_driver": None,
    # Per-builder override keeps existing Claude/Codex runs unchanged.
    "strategic_driver_by_run": {"opencode": "codex"},
}


def models_path() -> Path:
    return data_root() / "models.json"


def load_table() -> dict:
    t = json.loads(json.dumps(DEFAULTS))
    p = models_path()
    if p.is_file():
        try:
            user = json.loads(p.read_text())
        except Exception:
            user = {}
        for drv in ("claude", "codex", "opencode"):
            if not isinstance(user.get(drv), dict):
                continue      # a hand-edited file with e.g. {"claude": "opus"} must not crash every command
            for tier in ("builder", "strategic", "strategic_fallback"):
                if isinstance(user.get(drv, {}).get(tier), dict):
                    if t[drv].get(tier) is None:
                        t[drv][tier] = {}
                    t[drv][tier].update({k: v for k, v in user[drv][tier].items() if k in ("model", "effort")})
            roles = user.get(drv, {}).get("roles")
            if isinstance(roles, dict):
                for role, entry in roles.items():
                    if isinstance(entry, dict):
                        t[drv].setdefault("roles", {})[role] = {k: v for k, v in entry.items() if k in ("model", "effort")}
        if "strategic_driver" in user:
            t["strategic_driver"] = user["strategic_driver"]
        if isinstance(user.get("strategic_driver_by_run"), dict):
            t["strategic_driver_by_run"].update(user["strategic_driver_by_run"])
    return t


def tier_of(role: str) -> str:
    return "strategic" if role in STRATEGIC_ROLES else "builder"


def resolve(role: str, run_driver: str, *, cli_model: str | None = None) -> dict:
    """Return {"driver", "model", "effort", "tier", "source"}."""
    t = load_table()
    tier = tier_of(role)
    driver = run_driver
    per_run = (t.get("strategic_driver_by_run") or {}).get(run_driver)
    strategic_driver = per_run or t.get("strategic_driver")
    if tier == "strategic" and strategic_driver in ("claude", "codex", "opencode"):
        driver = strategic_driver
    if driver not in ("claude", "codex", "opencode"):
        driver = "claude"
    entry = dict(t[driver][tier])
    src = "models.json/defaults"
    role_entry = (t[driver].get("roles") or {}).get(role)
    if isinstance(role_entry, dict) and (role_entry.get("model") or role_entry.get("effort")):
        # Anything the role omits is inherited from its tier rather than left empty. An override that names
        # only a model would otherwise launch with no --effort at all and pick up whatever the CLI's ambient
        # default happens to be — an effort nobody chose, which is the exact thing the owner's
        # "nothing above medium anywhere" rule exists to prevent.
        entry, src = {**entry, **{k: v for k, v in role_entry.items() if v is not None}}, f"models.json/roles.{role}"
    if tier == "strategic":
        env_specific = os.environ.get(f"LONGRUN_STRATEGIC_MODEL_{driver.upper()}")
        env_any = os.environ.get("LONGRUN_STRATEGIC_MODEL")
        if cli_model:
            entry["model"], src = cli_model, "cli"
        elif env_specific:
            entry["model"], src = env_specific, f"env LONGRUN_STRATEGIC_MODEL_{driver.upper()}"
        elif env_any:
            entry["model"], src = env_any, "env LONGRUN_STRATEGIC_MODEL"
    else:
        if cli_model:
            entry["model"], src = cli_model, "cli"
    # The fallback is about the model being unavailable (usage limit / overload), which happens to an
    # explicitly named model just as much as to the default one — a night launched with --eval-model
    # must not silently lose its failover.
    fb = t[driver].get("strategic_fallback") if tier == "strategic" else None
    if driver == "codex" and entry.get("model") == "gpt-5.6-sol":
        # An explicitly selected SOL session may retry SOL, but must never silently become Terra.
        # Keep this invariant even if an older user models.json still contains the former fallback.
        fb = None
    if fb and fb.get("model") and fb["model"] == entry.get("model"):
        # This role already runs the tier's fallback model — the planner does, by design, now that the
        # frequent roles moved to the cheaper model and the expensive one became the failover. Failing over
        # to itself is a no-op that silently spends the infra-retry budget waiting out a limit that will not
        # lift for hours, so the tier's own model becomes this role's failover instead. Keeping the two
        # strategic models as each other's failover needs no extra configuration and cannot drift.
        other = t[driver][tier]
        fb = dict(other) if other.get("model") and other["model"] != entry.get("model") else None
    if fb and not fb.get("effort"):
        # A failover that carries no effort launches the retry with none, so the rescue silently runs at the
        # CLI's ambient default — the same unchosen-effort trap as above, on the path taken under a usage
        # limit, where nobody is watching.
        fb["effort"] = t[driver][tier].get("effort")
    return {"driver": driver, "model": entry.get("model"), "effort": entry.get("effort"), "tier": tier, "source": src,
            "fallback": dict(fb) if fb and fb.get("model") else None}


def write_default_table() -> Path:
    p = models_path()
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    return p


def describe(run_driver: str = "claude") -> str:
    L = [f"models table: {models_path()} ({'present' if models_path().is_file() else 'built-in defaults'})"]
    for role in ("builder", "evaluator", "restart_manager", "planner"):
        r = resolve(role, run_driver)
        # A role with no failover is printed as such rather than left blank: "no fallback" is a state that
        # costs hours of infra retries under a usage limit, and it must not be inferred from a missing column.
        fb = (f"  fallback={r['fallback']['model']}/{r['fallback'].get('effort')}" if r.get("fallback")
              else ("  fallback=none" if r["tier"] == "strategic" else ""))
        L.append(f"  {role:16s} -> driver={r['driver']:6s} model={r['model'] or '(driver default)':14s} effort={r['effort'] or '-':6s} [{r['source']}]{fb}")
    return "\n".join(L)
