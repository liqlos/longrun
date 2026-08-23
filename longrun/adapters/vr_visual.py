"""vr_visual adapter — the useful visual-judging logic from the old autopilot bundle, made optional.

Carried over (adapted): fixed named views (establishing/spawn/vista, look-around, look-down, work pose),
new-pixels check via hashes, blind comparison against previous and baseline sets, magnitude tiers
(thumbnail/flicker/inspection/none) as evaluator vocabulary, and the owner comparison packet.
Dropped from the core: spawn/vista words, hard-coded widths, mandatory light/atmosphere/grade quotas,
projects/<product>/docs assumptions and game-specific agent prompts. A global render pass is never forced by
a counter; it only appears when a criterion or demonstrated visual blocker asks for it.
"""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path

from . import Adapter

CAPTURE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_VIEWS = ["establishing", "lookaround", "lookdown", "work"]
MAGNITUDES = ("thumbnail", "flicker", "inspection", "none", "unsure")


class VrVisualAdapter(Adapter):
    name = "vr_visual"
    description = "VR/3D visual product: named-view captures, blind before/after comparison, headset evidence."
    baseline_commands = [{"cmd": "git rev-parse --verify HEAD", "kind": "check", "timeout_seconds": 30}]
    # Run after every builder round, before the evaluator is paid. Kept to seconds: the point is to catch a
    # workspace that cannot build (a compile error, a scene whose script references lost their guids) before
    # spending ~140 s on a capture or ~$5 on a judgement. A project supplies the real build check through
    # adapter_config["round_gate"]; the default only catches the scene-corruption case, which costs nothing.
    min_child_timeout_seconds = 5400
    round_gate_commands = [
        {"cmd": "! git ls-files -co --exclude-standard -- '*.unity' | xargs grep -lE '^  m_Script: \\{fileID: [0-9]+\\}$' 2>/dev/null | head -1 | grep .",
         "kind": "check", "timeout_seconds": 60},
    ]
    # Unity rewrites nearly the whole scene file on every build, so its diff is noise that crowds the code
    # being judged out of the evaluator's window. The --stat still names it.
    diff_exclude_globs = ["*.unity"]
    allowed_commands = ["python3 *", "make *", "Unity *", "unity *", "adb *", "xcrun simctl *", "ls *"]
    builder_guidance = (
        "Visual criteria need `screenshot` evidence for the named views (see adapter_config.views) captured at the "
        "current revision with the view name in the file name, plus one `capture_manifest` evidence record produced by "
        "`longrun evidence submit --kind capture_manifest --criterion <id> --artifact <manifest.json>` where the manifest "
        "is written by `longrun adapter vr-visual manifest --dir <capture dir>` (it hashes the captures and refuses "
        "byte-identical re-saves). Do not describe what changed to the evaluator; it judges blind. Headset/simulator "
        "measurements (comfort, frame time) are `metric` evidence from the device, never a claim. Verify the chain "
        "reference -> mesh file -> project import -> live scene/prefab binding -> fresh named-view capture; never "
        "report a later stage from evidence of an earlier one. A failed visual generation is not a stopping point: "
        "If the owner requires a named generator or model, verify the provider request and successful result records, "
        "task or asset id, downloaded raw artifact hash, processed import hash, and live binding. A prompt, provider name "
        "in prose, deterministic lookalike, authored proxy, or retexture-only result cannot stand in for required generated "
        "geometry. Keep exact authored cages for scale, collision, pivots, and cleanup without counting them as the final "
        "generated visual asset. "
        "For a visual-upgrade outcome, trace every appearance-bearing provider output that creates the accepted look: "
        "geometry, UVs, material assignments and textures. Never replace those with an old deterministic atlas or generic "
        "material merely because the generated mesh remains in the scene. Capture one neutral representative asset view "
        "to prove the look survived import and a real player view where the change occupies enough pixels to matter. "
        "Treat provider-to-engine calibration as an ordering gate, not end-of-round evidence: before batch binding or any "
        "Simulator run, import one representative generated asset and compare an ordinary-color engine render against the "
        "provider preview from equivalent front and oblique directions. Verify upright/up-forward axes, ground pivot, "
        "metric bounds, UV continuity, provider material and texture assignment, plausible brightness, and recognizable "
        "silhouette/detail preservation. If that calibration fails, repair the shared import/normalization/material route "
        "or reject/regenerate the asset; do not multiply the defect across the scene and do not spend a Simulator cycle. "
        "When the active increment defines a named first wave, cohort, or batch whose combined presence creates the visible delta, neutral-review one representative to calibrate the shared route, then finish and neutral-review the entire named cohort before any Simulator run. Never use a one-item Simulator capture to stand in for a documented batch-level whole-frame result. "
        "A route labelled raw/provider must render the downloaded provider bytes without transcoding, decimation, texture "
        "repacking, or non-uniform deformation. Show processed output separately. When optimization removes more than 80% "
        "of provider faces, compare raw and processed neutral renders and reject melted silhouette or repeated detail before "
        "integration; receipts and triangle counts cannot establish visual preservation. "
        "after two failures with the same material failure signature, change only the failing source-of-truth step "
        "while continuing toward the visible criterion. For a multi-view 3D source, prove that "
        "every view is the same object — invariant bay/floor counts, footprint, silhouette, roofline, service elements "
        "and facade details. Near-duplicate angles do not supply missing side or back coverage. If views conflict or omit "
        "the generator's required/recommended coverage, never feed them to reconstruction; switch to one strong source image "
        "plus metric cleanup, or an exact authored base plus a generated material/retexture pass. Before spending a "
        "Simulator cycle, prove from the real camera transform/FOV and object bounds that every required named view "
        "can physically contain the subject. Use cheap neutral asset renders for angles outside the live camera's reach. "
        "For one coherent edit batch, build once and capture once; a detached wrapper may be recovered with one "
        "no-build capture, but repeated placement-by-screenshot is a contract/visibility defect, not an authoring loop.")
    evaluator_guidance = (
        "You judge frames, not claims. Open every cited capture; compare the establishing view against the previous set "
        "and the frozen baseline. Report the magnitude tier for each visual criterion: thumbnail (a stranger sees it at "
        "~200 px, no A/B), flicker (only when A/B-toggled at full size), inspection (only when told where to look), none. "
        "A criterion phrased as a visible change PASSes only at the tier its statement demands (default: thumbnail for "
        "establishing-view criteria, flicker for detail criteria). Never call a capture comfortable or performant; those are "
        "device metrics. Provider-default looks are a failure worth naming. If a cited capture is byte-identical to an "
        "earlier one, verdict INSUFFICIENT_EVIDENCE. For generated multi-view assets, inspect every cited angle rather "
        "than the curated hero frame. Mutated floors, bays, footprint, roofline or service construction are a FAIL, as "
        "are facade-card extrusion, toy/maquette scale cues, or a nominal storey count contradicted by visible rhythm. "
        "A standalone reference, turntable, asset file, rejection document, or contact sheet cannot PASS visible "
        "integration; inspect a fresh named-view capture and the current scene or prefab binding. When a named generator "
        "or model is mandatory, do not PASS from filenames or provenance prose alone: inspect provider-authenticated "
        "request/result records and trace their task or asset id through the downloaded raw artifact, processed import, "
        "and current binding. Authored proxy geometry does not satisfy required generated geometry. A batch visual result "
        "cannot PASS when the representative provider-to-engine calibration is absent or when equivalent provider and "
        "engine views disagree on upright orientation, silhouette, facade detail, UV continuity, material identity, or "
        "plausible brightness; require repair at the shared source stage before judging player-view integration. "
        "Reject a batch-level player-view claim when only a representative subset is integrated or when the active increment's named first cohort is incomplete; the representative is calibration evidence, not the visible batch result. "
        "Reject a supposed raw/provider comparison if the candidate was transcoded, decimated, texture-repacked, or non-uniformly "
        "deformed before rendering, and reject multi-view reconstruction evidence built from conflicting or near-duplicate "
        "angles that omit required side/back coverage.")
    evaluator_guidance += (
        " A segmentation mask, object-id overlay, binding report, or a few distant pixels proves presence, not visual "
        "improvement. For a generated visual upgrade, compare a representative imported-asset view with the accepted "
        "source and inspect the player frame; FAIL when old or generic materials erase the generated look, or when the "
        "change is too distant, occluded, or sparse to improve the experienced frame.")

    def views(self) -> list[str]:
        return list(self.config.get("views") or DEFAULT_VIEWS)

    def capture_dir(self, workspace: Path) -> Path:
        return workspace / (self.config.get("capture_dir") or "captures")

    # ---------------------------------------------------------------- capture manifest
    def build_manifest(self, capture_dir: Path, since_epoch: float | None, known_hashes: set[str]) -> dict:
        """Hash captures newer than `since_epoch`; mark named views; refuse byte-identical re-saves."""
        items = []
        views = self.views()
        for f in sorted(capture_dir.iterdir()) if capture_dir.is_dir() else []:
            if not f.is_file() or f.suffix.lower() not in CAPTURE_EXT:
                continue
            if since_epoch is not None and f.stat().st_mtime < since_epoch:
                continue
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            view = next((v for v in views if v in f.name.lower()), None)
            items.append({"path": str(f), "sha256": h, "view": view, "size": f.stat().st_size,
                          "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f.stat().st_mtime)),
                          "duplicate_of_known": h in known_hashes})
        by_view = {v: [i for i in items if i["view"] == v] for v in views}
        problems = []
        if not items:
            problems.append("no captures found")
        if not by_view.get(views[0]):
            problems.append(f"no capture for the establishing view '{views[0]}'")
        dups = [i["path"] for i in items if i["duplicate_of_known"]]
        if dups:
            problems.append(f"byte-identical to earlier captures: {dups}")
        return {"adapter": "vr_visual", "views": views, "captures": items, "problems": problems,
                "ok": not problems, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def post_round(self, workspace: Path, since_epoch: float, contract: dict, known_hashes: set[str]) -> list[dict]:
        """Pick up captures written during the round that nobody filed. Only genuinely new frames count:
        `build_manifest` already marks byte-identical re-saves, and those are dropped here rather than
        offered to the evaluator as if something had changed."""
        man = self.build_manifest(self.capture_dir(workspace), since_epoch, known_hashes)
        fresh = [c for c in man["captures"] if not c["duplicate_of_known"]]
        if not fresh:
            return []
        # Bind to the criteria that actually want frames; a capture proves nothing about a docs criterion.
        wants = {"screenshot", "capture_manifest"}
        cids = [x["id"] for x in contract["criteria"] if wants & set(x.get("evidence_requirements") or [])]
        if not cids:
            return []
        named = sorted({c["view"] for c in fresh if c["view"]})
        return [{
            "kind": "capture_manifest", "criterion_ids": cids,
            "summary": (f"harvested by the controller at the end of the round: {len(fresh)} new capture(s)"
                        + (f", named views {named}" if named else ", no named views")
                        + (f"; manifest problems: {man['problems']}" if man["problems"] else "")),
            "artifacts": [c["path"] for c in fresh],
            "data": {"harvested": True, "manifest": dict(man, captures=fresh)},
        }]

    def owner_packet(self, baseline_manifest: dict | None, current_manifest: dict) -> str:
        """Human comparison packet: baseline vs current per view (paths only; the owner looks)."""
        L = ["Owner visual comparison packet"]
        base = {i["view"]: i["path"] for i in (baseline_manifest or {}).get("captures", []) if i.get("view")}
        cur = {i["view"]: i["path"] for i in current_manifest.get("captures", []) if i.get("view")}
        for v in self.views():
            L.append(f"  {v:14s} baseline: {base.get(v, '-')}\n  {'':14s} current : {cur.get(v, '-')}")
        return "\n".join(L)


ADAPTER_CLASS = VrVisualAdapter
