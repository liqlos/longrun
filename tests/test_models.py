from __future__ import annotations
import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["LONGRUN_HOME"] = self.tmp
        for k in ("LONGRUN_STRATEGIC_MODEL", "LONGRUN_STRATEGIC_MODEL_CODEX", "LONGRUN_STRATEGIC_MODEL_CLAUDE"):
            os.environ.pop(k, None)

    def test_defaults_route_by_frequency_on_both_drivers(self):
        """Frequency decides, not how strategic a call feels. The per-round evaluator runs on the middle
        judgement model; the once-per-outcome directional calls (planner, restart manager) get the dearest
        one; the builder — 63% of the bill measured over 57 runs — gets the volume tier. Codex is tiered the
        same way for the same reason: terra where sonnet is, sol where opus is."""
        from longrun.models import resolve
        r = resolve("evaluator", "claude")
        self.assertEqual((r["model"], r["effort"]), ("opus", "medium"))
        for role in ("planner", "restart_manager"):
            p = resolve(role, "claude")
            self.assertEqual((p["model"], p["effort"]), ("fable", "medium"), role)
        self.assertEqual(resolve("builder", "claude")["model"], "sonnet")
        c = resolve("evaluator", "codex")
        self.assertEqual((c["model"], c["effort"], c["driver"]), ("gpt-5.6-sol", "medium", "codex"))
        self.assertIsNone(c["fallback"])
        self.assertEqual(resolve("builder", "codex")["model"], "gpt-5.6-terra")
        o = resolve("builder", "opencode")
        self.assertEqual((o["driver"], o["model"]), ("opencode", "opencode/x-preview-f-free"))
        self.assertEqual(resolve("planner", "opencode")["driver"], "codex")

    def test_legacy_codex_fallback_cannot_demote_explicit_sol(self):
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps(
            {"codex": {"strategic": {"model": "gpt-5.6-sol", "effort": "medium"},
                       "strategic_fallback": {"model": "gpt-5.6-terra", "effort": "medium"}}}))
        self.assertIsNone(resolve("planner", "codex")["fallback"])

    def test_no_role_anywhere_runs_above_medium_on_fable(self):
        """Owner rule, stated twice: fable never above medium — including the failover entry, which is
        where an unchosen `high` sat unnoticed for weeks and cost $8 a call when it fired."""
        from longrun.models import load_table
        t = load_table()
        for tier in ("builder", "strategic", "strategic_fallback"):
            e = t["claude"].get(tier) or {}
            if e.get("model") == "fable":
                self.assertEqual(e.get("effort"), "medium", tier)
        for role, e in (t["claude"].get("roles") or {}).items():
            if e.get("model") == "fable":
                self.assertEqual(e.get("effort"), "medium", role)

    def test_builder_runs_at_medium_effort(self):
        from longrun.models import resolve
        self.assertEqual(resolve("builder", "claude")["effort"], "medium")

    def test_a_named_role_overrides_the_tier_and_says_so(self):
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps(
            {"claude": {"strategic": {"model": "opus", "effort": "medium"},
                        "roles": {"evaluator": {"model": "sonnet", "effort": "low"}}}}))
        r = resolve("evaluator", "claude")
        self.assertEqual((r["model"], r["effort"]), ("sonnet", "low"))
        self.assertIn("roles.evaluator", r["source"])
        # one role entry must not leak into another: the restart manager keeps its own default entry
        self.assertEqual(resolve("restart_manager", "claude")["model"], "fable")

    def test_a_named_role_inherits_what_it_does_not_override(self):
        """A role entry naming only a model must not launch with no effort at all: the child would then run
        at the CLI's ambient default, which is exactly the unchosen-effort trap this work set out to close."""
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps(
            {"claude": {"strategic": {"model": "opus", "effort": "medium"},
                        "roles": {"planner": {"model": "fable"}}}}))
        r = resolve("planner", "claude")
        self.assertEqual((r["model"], r["effort"]), ("fable", "medium"))

    def test_a_role_may_override_the_effort_alone(self):
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps(
            {"claude": {"strategic": {"model": "opus", "effort": "medium"},
                        "roles": {"evaluator": {"effort": "low"}}}}))
        r = resolve("evaluator", "claude")
        self.assertEqual((r["model"], r["effort"]), ("opus", "low"))

    def test_a_failover_always_carries_an_effort(self):
        """The rescue path runs under a usage limit with nobody watching; launching it at an unset effort is
        the same bug as above, in the place it is least likely to be noticed. A hand-written entry that names
        only a model must still come out with one — from the defaults it merges over, or from its own tier."""
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps(
            {"codex": {"strategic": {"model": "gpt-a", "effort": "high"},
                       "strategic_fallback": {"model": "gpt-b"}}}))
        fb = resolve("evaluator", "codex")["fallback"]
        self.assertEqual(fb["model"], "gpt-b")
        self.assertIsNotNone(fb.get("effort"))

    def test_a_hand_edited_file_with_the_wrong_shape_does_not_crash_every_command(self):
        """`longrun models --init` invites hand-editing; {"claude": "opus"} used to raise AttributeError out
        of every command that resolves a model, including `status`."""
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps({"claude": "opus"}))
        self.assertEqual(resolve("evaluator", "claude")["model"], "opus")   # falls through to the defaults

    def test_overrides_and_cross_driver(self):
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps({"codex": {"strategic": {"model": "gpt-x-judge"}}, "strategic_driver": "claude"}))
        r = resolve("evaluator", "codex")
        self.assertEqual((r["driver"], r["model"]), ("claude", "opus"))           # cross-driver judging wins
        (Path(self.tmp) / "models.json").write_text(json.dumps({"codex": {"strategic": {"model": "gpt-x-judge"}}}))
        self.assertEqual(resolve("evaluator", "codex")["model"], "gpt-x-judge")
        os.environ["LONGRUN_STRATEGIC_MODEL_CODEX"] = "gpt-env"
        self.assertEqual(resolve("evaluator", "codex")["model"], "gpt-env")
        self.assertEqual(resolve("evaluator", "codex", cli_model="gpt-cli")["model"], "gpt-cli")
        self.assertEqual(resolve("builder", "codex")["model"], "gpt-5.6-terra")   # env never touches the builder tier

    def test_driver_flags(self):
        from longrun.drivers.claude import ClaudeDriver
        from longrun.drivers.codex import CodexDriver
        c = ClaudeDriver().build_command(role="evaluator", prompt="p", session_id="s", cwd=Path("/tmp"), max_turns=5, allowed_commands=[], deny_paths=[], model="fable", json_schema={"type": "object"}, max_budget_usd=None, permission_mode="acceptEdits", effort="high")
        self.assertIn("--effort", c); self.assertIn("fable", c)
        x = CodexDriver().build_command(role="evaluator", prompt="p", session_id="s", cwd=Path("/tmp"), max_turns=5, allowed_commands=[], deny_paths=[], model=None, json_schema=None, max_budget_usd=None, permission_mode="acceptEdits", effort="high")
        self.assertIn('model_reasoning_effort="high"', x)


if __name__ == "__main__":
    unittest.main()


class FallbackTest(unittest.TestCase):
    def setUp(self):
        # Without this the class reads the machine's real models.json and asserts against the owner's
        # live configuration, so an edit there breaks the suite for reasons that have nothing to do with it.
        self.tmp = tempfile.mkdtemp(); os.environ["LONGRUN_HOME"] = self.tmp
        for k in ("LONGRUN_STRATEGIC_MODEL", "LONGRUN_STRATEGIC_MODEL_CODEX", "LONGRUN_STRATEGIC_MODEL_CLAUDE"):
            os.environ.pop(k, None)

    def test_strategic_roles_fail_over_to_fable_at_medium(self):
        from longrun.models import resolve
        r = resolve("evaluator", "claude")
        self.assertEqual(r["model"], "opus")
        self.assertEqual(r["fallback"], {"model": "fable", "effort": "medium"})
        self.assertIsNone(resolve("builder", "claude")["fallback"])

    def test_cli_named_strategic_model_keeps_its_failover(self):
        """A night launched with --eval-model must not silently lose the failover: a usage limit hits an
        explicitly named model exactly as it hits the default one. One night ran six outcomes this way."""
        from longrun.models import resolve
        self.assertEqual(resolve("evaluator", "claude", cli_model="sonnet")["fallback"],
                         {"model": "fable", "effort": "medium"})

    def test_a_run_launched_on_the_fallback_model_fails_over_to_the_other_one(self):
        """`--eval-model fable` names the model that is also the tier's fallback; it must still have
        somewhere to go when that model reports a limit, not sit and retry the limited one."""
        from longrun.models import resolve
        self.assertEqual(resolve("evaluator", "claude", cli_model="fable")["fallback"],
                         {"model": "opus", "effort": "medium"})

    def test_the_two_strategic_models_are_each_others_failover(self):
        """The planner runs the model that is also the tier's fallback. Without this it would 'fail over'
        to the model that just reported a usage limit — a no-op that spends the infra-retry budget waiting
        out a limit that will not lift for hours. It hung a full test run before it was caught."""
        from longrun.models import resolve
        self.assertEqual(resolve("planner", "claude")["fallback"], {"model": "opus", "effort": "medium"})
        self.assertEqual(resolve("evaluator", "claude")["fallback"], {"model": "fable", "effort": "medium"})

    def test_a_role_on_the_tier_model_keeps_no_self_failover(self):
        from longrun.models import resolve
        (Path(self.tmp) / "models.json").write_text(json.dumps(
            {"claude": {"strategic": {"model": "opus", "effort": "medium"},
                        "strategic_fallback": {"model": "opus", "effort": "medium"}}}))
        self.assertIsNone(resolve("evaluator", "claude")["fallback"])
