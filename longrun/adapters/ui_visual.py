"""Web/desktop UI: screenshots at named viewports as evidence."""
from __future__ import annotations
from . import Adapter


class UiVisualAdapter(Adapter):
    name = "ui_visual"
    description = "Web/desktop UI: screenshots at named viewports as evidence."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = ['npm run *', 'npx playwright *', 'pytest *', 'python3 *']
    builder_guidance = """Visual criteria need `screenshot` evidence captured at the current revision with the view name in the file name; the evaluator opens the image."""
    evaluator_guidance = """Open every cited screenshot. Judge what is visible, not what the summary claims. If baseline and current are indistinguishable for a change criterion, verdict FAIL."""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = UiVisualAdapter
