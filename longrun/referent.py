"""Where does a criterion's own vocabulary actually live?

A criterion names things: an asset, a cue, a symbol, a state. The evaluator then reads a ledger that
also contains those words, and judges whether the outcome was reached. Nothing in that loop ever asks
the flat question of whether the word names anything at all.

One night a criterion PASSed on a log line reading `cue=steel-knock`, while the code played a different
sound and the project contained no steel impact sound whatsoever. The disconfirming code was inside the
diff the evaluator already held. The label was fine; it referred to nothing, and no step in the pipeline
was looking at the join between a label and its referent.

This module answers that one question, mechanically: for every literal a criterion quotes, which tracked
files contain it. The answer is a fact, not a judgement, and it is handed to the evaluator as a fact.
It blocks nothing and fails no run — a token constructed at runtime legitimately resolves nowhere, and a
gate here would cost repair rounds for honest work. What it removes is the ability to pass a criterion
about a thing that does not exist without that being visible on the page.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Literals a criterion quotes about itself: 'x', "x" or `x`. Unquoted prose is not a referent claim.
_QUOTED = re.compile(r"[`'\"]([A-Za-z][A-Za-z0-9_.\-/]{2,63})[`'\"]")
# Regex checks carry their expectation literally; take the runs of plain text out of them.
_REGEX_LITERAL = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,63}")
_REGEX_META = set(".^$*+?()[]{}|\\")

# Words that are quoted constantly and name nothing in particular.
_STOP = {
    "the", "and", "not", "for", "with", "that", "this", "from", "into", "must", "should", "when", "then",
    "true", "false", "null", "none", "pass", "fail", "passed", "failed", "error", "warning", "log", "logs",
    "test", "tests", "check", "checks", "scene", "scenes", "asset", "assets", "file", "files",
}


def _literals(criterion: dict) -> list[str]:
    out: list[str] = []
    for m in _QUOTED.finditer(criterion.get("statement") or ""):
        out.append(m.group(1))
    for ch in criterion.get("deterministic_checks") or []:
        pat = ch.get("expect_stdout_regex")
        if not pat:
            continue
        # Only mine a regex that is mostly literal; a heavily quantified pattern is not a name.
        if sum(c in _REGEX_META for c in pat) > len(pat) / 4:
            continue
        out.extend(_REGEX_LITERAL.findall(pat))
    seen, uniq = set(), []
    for t in out:
        low = t.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        uniq.append(t)
    return uniq[:8]


def _tracked_hits(workspace: Path, token: str, limit: int = 6) -> list[str]:
    """Files under version control that contain the token. Untracked build output is not a referent."""
    try:
        p = subprocess.run(["git", "grep", "-l", "--fixed-strings", "-I", "--", token],
                           cwd=str(workspace), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode not in (0, 1):
        return []
    files = [l for l in (p.stdout or "").splitlines() if l.strip()]
    return files[:limit]


def referent_report(workspace: Path, contract: dict) -> list[dict]:
    """Per criterion, the literals it names and the tracked files each one is found in.

    Criteria that quote nothing are omitted entirely, so a contract written in plain prose adds no noise.
    """
    rows: list[dict] = []
    for c in contract.get("criteria") or []:
        toks = _literals(c)
        if not toks:
            continue
        found = []
        for t in toks:
            hits = _tracked_hits(workspace, t)
            found.append({"literal": t, "found_in": hits, "resolves": bool(hits)})
        if found:
            rows.append({"criterion": c["id"], "literals": found})
    return rows
