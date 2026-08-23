"""General software project: tests, builds, CLIs, libraries."""
from __future__ import annotations
from . import Adapter


class SoftwareAdapter(Adapter):
    name = "software"
    description = "General software project: tests, builds, CLIs, libraries."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = ['pytest *', 'python3 -m pytest *', 'python3 -m unittest *', 'npm test*', 'npm run *', 'pnpm *', 'yarn *', 'cargo test*', 'cargo build*', 'go test *', 'go build *', 'make *', 'uv run *', 'python3 *', 'node *']
    builder_guidance = """Evidence for functional criteria is a command you ran with its exit code (kind check/test/build). Submit the exact command."""
    evaluator_guidance = """Re-run the cited deterministic checks yourself when cheap; a claim without a command and exit code is INSUFFICIENT_EVIDENCE."""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = SoftwareAdapter
