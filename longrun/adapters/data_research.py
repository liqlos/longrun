"""Data/research: metrics, notebooks, artifacts, reproducible commands."""
from __future__ import annotations
from . import Adapter


class DataResearchAdapter(Adapter):
    name = "data_research"
    description = "Data/research: metrics, notebooks, artifacts, reproducible commands."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = ['python3 *', 'uv run *', 'jupyter nbconvert *', 'dvc *', 'make *']
    builder_guidance = """Metric criteria need `metric` evidence: the command that computed the number, its stdout, and the artifact path. Cite sources for factual claims."""
    evaluator_guidance = """Recompute cheap metrics; reject numbers without a reproducing command. Uncited factual claims are INSUFFICIENT_EVIDENCE."""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = DataResearchAdapter
