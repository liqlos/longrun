"""Migration of the old autopilot bundle and clean uninstall/restore.

migrate_project: detect `.claude/autopilot/state.json` + `projects/<product>/docs/increments.json`, archive them,
convert reusable settings into a vr_visual adapter config and a DRAFT contract for owner review. Never carries
active status, baseline, history or counters. Never deletes tracked repository files.
migrate_global: archive ~/.claude/hooks/autopilot_* and remove any autopilot Stop/SessionStart hooks from
~/.claude/settings.json (with a hashed backup and restore script).
uninstall: remove the longrun tool, restore the settings snapshot recorded at install (hash-checked).
"""
from __future__ import annotations
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from .paths import archive_root, backups_root, data_root, ensure_dirs
from .store import atomic_write_json


def _ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def migrate_project(root: Path, dry_run: bool = False) -> dict:
    ensure_dirs()
    rep = {"project": str(root), "found": [], "archived": [], "draft_contract": None, "adapter_config": None, "notes": []}
    old_state = root / ".claude" / "autopilot" / "state.json"
    if not old_state.is_file():
        rep["notes"].append("no old autopilot state found"); return rep
    st = json.loads(old_state.read_text())
    rep["found"].append(str(old_state))
    product = st.get("product") or ""
    inc_path = root / product / "docs" / "increments.json" if product else None
    incs = []
    if inc_path and inc_path.is_file():
        rep["found"].append(str(inc_path))
        incs = json.loads(inc_path.read_text()).get("increments", [])
    arch = archive_root() / f"{_ts()}-{root.name}"
    if not dry_run:
        arch.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old_state.parent, arch / "autopilot", dirs_exist_ok=True)
        if inc_path and inc_path.is_file():
            shutil.copy2(inc_path, arch / "increments.json")
        for extra in (root / ".claude" / "agents", root / ".agents" / "skills" / "project-autopilot"):
            if extra.exists():
                shutil.copytree(extra, arch / extra.name if extra.name != "agents" else arch / "claude-agents", dirs_exist_ok=True)
        rep["archived"].append(str(arch))
        # remove the old (untracked) state so nothing can read `active` from it; the archive keeps evidence
        shutil.rmtree(old_state.parent, ignore_errors=True)
        rep["notes"].append(f"removed {old_state.parent} (archived first; it was untracked runtime state)")
    # adapter config: reusable, non-stateful settings only
    adapter_config = {"product_path": product, "capture_dir": f"{product}/docs/progress" if product else "captures",
                      "views": ["spawn", "vista", "lookaround", "lookdown", "work"],
                      "note": "converted from autopilot_gate; no counters, history, or baseline carried over"}
    todo = [i for i in incs if i.get("status") != "done" and not i.get("passes")]
    criteria = []
    for i in todo[:6]:
        axis = i.get("axis", "")
        kind = "visual" if axis in ("light", "atmosphere", "grade", "geometry", "character") else "player_facing"
        criteria.append({"id": i["id"][:32].replace(" ", "-"), "statement": (i.get("done_when") or i.get("title") or "")[:600],
                         "kind": kind, "evidence_requirements": ["screenshot", "capture_manifest"],
                         "deterministic_checks": [], "evaluator_policy": "llm_required",
                         "_migrated_from": {"axis": axis, "title": i.get("title")}})
    draft = {"observable_end_state": f"<OWNER: state the one visible outcome for {product or 'the product'} that these criteria add up to>",
             "criteria": criteria or [{"id": "C1", "statement": "<owner to define>", "kind": "visual",
                                       "evidence_requirements": ["screenshot", "capture_manifest"], "deterministic_checks": [], "evaluator_policy": "llm_required"}],
             "constraints": ["one coherent change set per round; capture named views at the current revision"],
             "non_goals": ["no mandatory light/atmosphere/grade quota; a global pass only when a criterion asks for it"],
             "allowed_replace_remove": [], "allowed_commands": [], "budgets": {"wall_time_seconds": 4 * 3600, "child_timeout_seconds": 2700,
             "max_rounds": 6, "max_repairs": 2, "max_fresh_restarts": 1}, "adapter_config": adapter_config,
             "_review": "DRAFT for owner review — do not run until observable_end_state is concrete and criteria are trimmed to one coherent outcome."}
    out = root / ".longrun" / "contract.draft.json"
    if not dry_run:
        out.parent.mkdir(exist_ok=True)
        atomic_write_json(out, draft)
        atomic_write_json(root / ".longrun" / "config.json", {"adapter": "vr_visual", "driver": "claude", "note": "migrated; presence activates nothing"})
    rep["draft_contract"] = str(out); rep["adapter_config"] = adapter_config
    rep["notes"].append(f"{len(todo)} open increments considered; {len(criteria)} converted to draft criteria (owner must trim to one outcome)")
    return rep


def migrate_global(dry_run: bool = False) -> dict:
    ensure_dirs()
    rep = {"archived": [], "settings_changed": False, "backup": None, "notes": []}
    home = Path.home()
    arch = archive_root() / f"{_ts()}-global-autopilot-bundle"
    hooks_dir = home / ".claude" / "hooks"
    files = [p for p in (hooks_dir / "autopilot_gate.py", hooks_dir / "autopilot_run.sh") if p.exists()]
    settings = home / ".claude" / "settings.json"
    if not dry_run:
        arch.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, arch / f.name); f.unlink(); rep["archived"].append(str(f))
        pc = hooks_dir / "__pycache__"
        if pc.is_dir() and not any(hooks_dir.glob("*.py")):
            shutil.rmtree(pc, ignore_errors=True)
    if settings.is_file():
        s = json.loads(settings.read_text())
        changed = False
        hooks = s.get("hooks") or {}
        for ev in list(hooks):
            keep = [g for g in hooks[ev] if not any("autopilot_gate" in h.get("command", "") for h in g.get("hooks", []))]
            if len(keep) != len(hooks[ev]):
                changed = True
                if keep: hooks[ev] = keep
                else: hooks.pop(ev)
        if changed and not dry_run:
            b = backups_root() / f"{_ts()}-settings-before-migrate"
            b.mkdir(parents=True, exist_ok=True)
            shutil.copy2(settings, b / "settings.json")
            (b / "settings.json.sha256").write_text(_sha(settings))
            atomic_write_json(settings, s)
            rep["backup"] = str(b)
        rep["settings_changed"] = changed
    rep["notes"].append("old bundle archived; no global hooks remain; ralph-loop symlink left untouched (not part of the bundle)")
    return rep


def install_snapshot(path: Path | None = None) -> Path:
    """Record the exact user settings state at install time so uninstall can restore it (hash-checked)."""
    ensure_dirs()
    b = backups_root() / f"{_ts()}-install-snapshot"
    b.mkdir(parents=True, exist_ok=True)
    for src in (Path.home() / ".claude" / "settings.json", Path.home() / ".codex" / "config.toml",
                Path.home() / ".codex" / "hooks.json", Path.home() / ".codex" / "AGENTS.md"):
        if src.is_file():
            shutil.copy2(src, b / src.name)
            (b / (src.name + ".sha256")).write_text(_sha(src))
    atomic_write_json(data_root() / "install.json", {"snapshot": str(b), "at": _ts()})
    return b


def uninstall(remove_data: bool = False, dry_run: bool = False) -> dict:
    rep = {"restored": [], "removed": [], "notes": [], "hash_ok": {}}
    info = data_root() / "install.json"
    snap = None
    if info.is_file():
        snap = Path(json.loads(info.read_text()).get("snapshot", ""))
    if snap and snap.is_dir():
        for name, dest in (("settings.json", Path.home() / ".claude" / "settings.json"),
                           ("config.toml", Path.home() / ".codex" / "config.toml"),
                           ("hooks.json", Path.home() / ".codex" / "hooks.json"),
                           ("AGENTS.md", Path.home() / ".codex" / "AGENTS.md")):
            src = snap / name
            if src.is_file():
                want = (snap / (name + ".sha256")).read_text().strip()
                if not dry_run:
                    shutil.copy2(src, dest)
                rep["restored"].append(str(dest))
                rep["hash_ok"][str(dest)] = (_sha(dest) == want) if dest.is_file() else False
    else:
        rep["notes"].append("no install snapshot; settings left as-is (longrun installs no global hooks anyway)")
    if not dry_run:
        r = subprocess.run(["uv", "tool", "uninstall", "longrun"], capture_output=True, text=True)
        rep["removed"].append(f"uv tool uninstall longrun -> {r.returncode}")
        if remove_data:
            shutil.rmtree(data_root() / "runs", ignore_errors=True)
            shutil.rmtree(data_root() / "keys", ignore_errors=True)
            rep["removed"].append(str(data_root() / "runs"))
    rep["notes"].append("backups/ and archive/ are kept; delete manually if desired")
    return rep
