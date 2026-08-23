"""Empty adapter: everything comes from the contract."""
from __future__ import annotations
from . import Adapter


class CustomAdapter(Adapter):
    name = "custom"
    description = "Empty adapter: everything comes from the contract."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = []
    builder_guidance = """"""
    evaluator_guidance = """"""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = CustomAdapter
