"""Shared test helpers: isolated LONGRUN_HOME, disposable git repos, stub claude on PATH."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
STUB = Path(__file__).resolve().parent / "stubbin"
PY = sys.executable
LONGRUN_BIN = shutil.which("longrun") or str(Path.home() / ".local/bin/longrun")


def _ensure_stub_exec():
    p = STUB / "claude"
    p.chmod(0o755)
    # make the stub run under the same interpreter that has longrun importable
    txt = p.read_text()
    if not txt.startswith(f"#!{PY}"):
        lines = txt.splitlines()
        lines[0] = f"#!{PY}"
        p.write_text("\n".join(lines) + "\n")


class LongrunTestCase(unittest.TestCase):
    def setUp(self):
        _ensure_stub_exec()
        self.tmp = Path(tempfile.mkdtemp(prefix="longrun-test-"))
        self.home = self.tmp / "lrhome"
        os.environ["LONGRUN_HOME"] = str(self.home)
        os.environ.pop("LONGRUN_TOKEN", None); os.environ.pop("LONGRUN_RUN_ID", None); os.environ.pop("LONGRUN_ROLE", None)
        os.environ["PATH"] = f"{STUB}:{os.environ['PATH']}"
        os.environ["LONGRUN_BIN"] = LONGRUN_BIN
        self.mode_file = self.tmp / "mode.json"
        os.environ["LONGRUN_STUB_MODE"] = str(self.mode_file)
        self.set_mode({})
        # ensure src is importable in-process
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        from longrun.paths import ensure_dirs
        ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_mode(self, d: dict):
        self.mode_file.write_text(json.dumps(d))

    def repo(self, name="proj") -> Path:
        r = self.tmp / name
        i = 0
        while r.exists():
            i += 1; r = self.tmp / f"{name}{i}"
        r.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=r); subprocess.run(["git", "config", "user.name", "t"], cwd=r)
        (r / "README.md").write_text("# test\n")
        subprocess.run(["git", "add", "-A"], cwd=r, check=True); subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
        return r

    def contract_spec(self, n=1, kind="functional", checks=True) -> dict:
        crits = []
        for i in range(1, n + 1):
            c = {"id": f"C{i}", "statement": f"criterion {i} holds and is externally checkable", "kind": kind,
                 "evidence_requirements": ["check"] if kind == "functional" else ["screenshot"],
                 "deterministic_checks": [{"cmd": "test -f feature.txt"}] if checks and kind == "functional" else [],
                 "evaluator_policy": "llm_required"}
            crits.append(c)
        return {"observable_end_state": "feature.txt exists in the workspace and the checks pass",
                "batch": {"boundary": "feature file implemented", "reality_test": "feature check passes",
                          "estimated_seconds": 600, "max_foreground_seconds": 60,
                          "deferred_required_outcomes": []}, "criteria": crits,
                "constraints": [], "non_goals": [], "budgets": {"wall_time_seconds": 600, "child_timeout_seconds": 60,
                "evaluator_timeout_seconds": 60, "max_rounds": 5, "max_repairs": 2, "max_fresh_restarts": 1}}

    def make_run(self, spec=None, isolation="worktree", adapter="software", repo=None):
        from longrun.controller import create_run, set_contract
        r = repo or self.repo()
        st = create_run(r, adapter, "claude", budgets=(spec or self.contract_spec()).get("budgets"), isolation=isolation, allow_dirty=True)
        set_contract(st, spec or self.contract_spec())
        return st, r

    def cli(self, *args, env=None, stdin=None):
        e = dict(os.environ); e.update(env or {})
        return subprocess.run([LONGRUN_BIN, *args], capture_output=True, text=True, env=e, input=stdin)
