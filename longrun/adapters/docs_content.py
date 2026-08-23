"""Documentation/content: rendered output and link/lint checks."""
from __future__ import annotations
from . import Adapter


class DocsContentAdapter(Adapter):
    name = "docs_content"
    description = "Documentation/content: rendered output and link/lint checks."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = ['make *', 'npm run *', 'python3 *', 'mkdocs *', 'markdownlint *']
    builder_guidance = """Docs criteria need `doc` evidence pointing at the concrete file/section plus any lint/link check command."""
    evaluator_guidance = """Read the cited section; check it states what the criterion requires. Style-only edits do not satisfy content criteria."""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = DocsContentAdapter
