"""Game logic/feel: recorded play sessions, logs, and captures."""
from __future__ import annotations
from . import Adapter


class GameplayAdapter(Adapter):
    name = "gameplay"
    description = "Game logic/feel: recorded play sessions, logs, and captures."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = ['python3 *', 'make *', 'npm run *']
    builder_guidance = """Player-facing criteria need `screenshot`/`video`/`log` evidence from an actual run of the game at the current revision."""
    evaluator_guidance = """Player-facing criteria cannot PASS on unit tests. Cite the capture or log that shows the behaviour."""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = GameplayAdapter
