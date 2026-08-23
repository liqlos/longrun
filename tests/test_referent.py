"""The referent check: does a literal a criterion quotes name anything in the product?

Modelled on the incident that motivated it — a criterion PASSed on a log line reading `cue=steel-knock`
while the project contained no steel impact sound at all.
"""
import subprocess
from pathlib import Path

from longrun.referent import referent_report


def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "proj"
    (ws / "Assets").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    return ws


def _commit(ws: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=ws, check=True)


def test_literal_that_names_nothing_is_reported_unresolved(tmp_path):
    ws = _repo(tmp_path)
    (ws / "Assets" / "Audio.cs").write_text("void Play(){ Log(\"cue=thunk\"); }\n")
    _commit(ws)
    contract = {"criteria": [{"id": "C1", "statement": "The log shows `steel-knock` when a rivet is seated.",
                              "deterministic_checks": []}]}
    rows = referent_report(ws, contract)
    lits = {l["literal"]: l for r in rows for l in r["literals"]}
    assert lits["steel-knock"]["resolves"] is False
    assert lits["steel-knock"]["found_in"] == []


def test_literal_backed_by_a_real_asset_resolves(tmp_path):
    ws = _repo(tmp_path)
    (ws / "Assets" / "steel-knock.wav.meta").write_text("guid: 1\n")
    (ws / "Assets" / "Audio.cs").write_text("var clip = Load(\"steel-knock\");\n")
    _commit(ws)
    contract = {"criteria": [{"id": "C1", "statement": "Seating a rivet plays `steel-knock`.",
                              "deterministic_checks": []}]}
    rows = referent_report(ws, contract)
    lit = rows[0]["literals"][0]
    assert lit["literal"] == "steel-knock"
    assert lit["resolves"] is True
    assert any("Audio.cs" in f for f in lit["found_in"])


def test_a_name_present_only_in_a_log_is_visible_as_such(tmp_path):
    """The steel-knock shape exactly: the token exists, but only where the product talks about itself."""
    ws = _repo(tmp_path)
    (ws / "Logs").mkdir()
    (ws / "Logs" / "rivet-log.txt").write_text("RECORD cue=steel-knock t=12.5\n")
    _commit(ws)
    contract = {"criteria": [{"id": "C1", "statement": "The log shows `steel-knock`.", "deterministic_checks": []}]}
    lit = referent_report(ws, contract)[0]["literals"][0]
    assert lit["resolves"] is True
    assert lit["found_in"] == ["Logs/rivet-log.txt"]  # the evaluator can see it lives nowhere else


def test_untracked_build_output_is_not_a_referent(tmp_path):
    ws = _repo(tmp_path)
    (ws / "Assets" / "keep.cs").write_text("// nothing\n")
    _commit(ws)
    (ws / "Library").mkdir()
    (ws / "Library" / "artifact.dat").write_text("steel-knock\n")  # never committed
    contract = {"criteria": [{"id": "C1", "statement": "plays `steel-knock`", "deterministic_checks": []}]}
    assert referent_report(ws, contract)[0]["literals"][0]["resolves"] is False


def test_prose_criteria_produce_no_rows(tmp_path):
    ws = _repo(tmp_path)
    (ws / "Assets" / "a.cs").write_text("x\n")
    _commit(ws)
    contract = {"criteria": [{"id": "C1", "statement": "The gang feels alive when the rivet lands.",
                              "deterministic_checks": []}]}
    assert referent_report(ws, contract) == []


def test_stopwords_and_heavy_regexes_are_not_mined(tmp_path):
    ws = _repo(tmp_path)
    (ws / "Assets" / "a.cs").write_text("x\n")
    _commit(ws)
    contract = {"criteria": [{"id": "C1", "statement": "The `test` must `pass`.",
                              "deterministic_checks": [{"expect_stdout_regex": r"^\s*(\d+)\s+(ok|OK)\s*$"}]}]}
    assert referent_report(ws, contract) == []


def test_literal_regex_expectation_is_mined(tmp_path):
    ws = _repo(tmp_path)
    (ws / "Assets" / "a.cs").write_text("x\n")
    _commit(ws)
    contract = {"criteria": [{"id": "C1", "statement": "clash audit is clean",
                              "deterministic_checks": [{"expect_stdout_regex": "CLASH_AUDIT_CLEAN"}]}]}
    lit = referent_report(ws, contract)[0]["literals"][0]
    assert lit["literal"] == "CLASH_AUDIT_CLEAN"
    assert lit["resolves"] is False


def test_non_git_workspace_degrades_to_empty(tmp_path):
    ws = tmp_path / "bare"
    ws.mkdir()
    contract = {"criteria": [{"id": "C1", "statement": "plays `steel-knock`", "deterministic_checks": []}]}
    rows = referent_report(ws, contract)
    assert rows[0]["literals"][0]["resolves"] is False  # no crash, no gate
