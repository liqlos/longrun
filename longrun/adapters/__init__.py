"""Domain adapters. The core only knows runs/outcomes/criteria/evidence/budgets/verdicts.
An adapter contributes: default evidence requirements per criterion kind, baseline commands, allowed commands,
compact builder/evaluator guidance, and optional capture/manifest helpers. Adapters never touch run state."""
from __future__ import annotations
import importlib
import json
from pathlib import Path

ADAPTER_NAMES = ["software", "api_backend", "ui_visual", "gameplay", "vr_visual", "data_research", "docs_content", "custom"]


class Adapter:
    name = "custom"
    description = "Custom/empty adapter"
    default_evidence = {"functional": ["check", "test"], "user_facing": ["screenshot", "http", "artifact"],
                        "visual": ["screenshot"], "player_facing": ["screenshot", "video"], "ui": ["screenshot"],
                        "docs": ["doc", "check"], "data": ["metric", "artifact"]}
    baseline_commands: list[dict] = []          # {cmd, timeout_seconds, kind}
    # Cheap checks run after every builder round, before the LLM evaluator is paid. A round that left the
    # workspace unable to build cannot have produced evidence worth judging, and the next round should be
    # told the compiler output rather than rediscovering it: one measured night lost 37 minutes and $5.95
    # to a compile error that surfaced two seconds before the round ended.
    round_gate_commands: list[dict] = []         # {cmd, timeout_seconds, kind}
    # The project's standing regression suite: "does the thing still work at all". Run beside the round gate
    # and blocking in the same way, but deliberately NOT criteria. Nine consecutive contracts each spent a
    # criterion re-inventing "the shift still plays" — 35% of one night's criteria went to things that were
    # not the outcome, each one a planner guess, an evaluator judgement and a chance to fail on bookkeeping.
    standing_checks: list[dict] = []             # {cmd, timeout_seconds, kind}
    # Paths to drop from the diff shown to the evaluator. Generated artifacts crowd out the code being
    # judged: a regenerated Unity scene occupied 22–91% (median ~45%) of the evaluator's 60k diff window,
    # and a one-material colour change moved 131,340 lines of it. The --stat is computed before exclusion,
    # so the evaluator still sees that the file changed — it just does not read the YAML.
    diff_exclude_globs: list[str] = []
    # Floor for a builder session when one verification step alone takes minutes. A measured round was
    # killed by the 2700 s timeout in the middle of a clean rebuild plus simulator capture ($11.80, 45 min,
    # no evidence). The waste this could invite is bounded separately by max_cost_without_delta_usd.
    min_child_timeout_seconds: int = 0
    allowed_commands: list[str] = []            # Bash allow patterns for the builder (e.g. "pytest *")
    builder_guidance = ""
    evaluator_guidance = ""
    phase_docs: dict[str, str] = {}             # phase -> knowledge doc name (routed, not injected wholesale)

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # A project knows its own build command; adapter_config["round_gate"] replaces the adapter's default
        # gate with it (a list of {cmd, timeout_seconds}), so the cheap check is the real compile.
        if isinstance(self.config.get("round_gate"), list):
            self.round_gate_commands = [dict(c, kind=c.get("kind", "check")) for c in self.config["round_gate"]]
        if isinstance(self.config.get("standing_checks"), list):
            self.standing_checks = [dict(c, kind=c.get("kind", "check")) for c in self.config["standing_checks"]]
        if isinstance(self.config.get("diff_exclude_globs"), list):
            self.diff_exclude_globs = [str(g) for g in self.config["diff_exclude_globs"]]

    def evidence_for_kind(self, kind: str) -> list[str]:
        return list(self.default_evidence.get(kind, ["check"]))

    def baseline(self, workspace: Path) -> list[dict]:
        return list(self.baseline_commands)

    def post_round(self, workspace: Path, since_epoch: float, contract: dict, known_hashes: set[str]) -> list[dict]:
        """Artifacts the controller can see and hash itself at the end of a round, returned as
        record_evidence(**kwargs) calls. Four builder sessions one night died — timed out or loop-killed —
        *after* running the full verification: the captures were on disk and the ledger was empty, so the
        round was scored as worthless and bought a repair. Evidence the controller can verify unaided should
        not depend on the builder surviving long enough to file it. Builder submissions stay authoritative;
        this only picks up what nobody claimed."""
        return []

    def builder_prompt_fragment(self) -> str:
        return self.builder_guidance.strip()

    def evaluator_prompt_fragment(self) -> str:
        return self.evaluator_guidance.strip()

    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description, "config": self.config,
                "allowed_commands": self.allowed_commands, "baseline_commands": self.baseline_commands,
                "round_gate_commands": self.round_gate_commands, "standing_checks": self.standing_checks,
                "diff_exclude_globs": self.diff_exclude_globs,
                "min_child_timeout_seconds": self.min_child_timeout_seconds}


def load_adapter(name: str, config: dict | None = None) -> Adapter:
    if name not in ADAPTER_NAMES:
        raise ValueError(f"unknown adapter {name!r}; choose from {ADAPTER_NAMES}")
    mod = importlib.import_module(f"longrun.adapters.{name}")
    return mod.ADAPTER_CLASS(config)
