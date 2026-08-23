"""HTTP/API backend: endpoints verified by real requests, not only unit tests."""
from __future__ import annotations
from . import Adapter


class ApiBackendAdapter(Adapter):
    name = "api_backend"
    description = "HTTP/API backend: endpoints verified by real requests, not only unit tests."
    baseline_commands = [{'cmd': 'git status --porcelain', 'kind': 'check', 'timeout_seconds': 60}]
    allowed_commands = ['pytest *', 'curl *', 'http *', 'uv run *', 'python3 *', 'npm run *', 'docker compose *', 'make *']
    builder_guidance = """User-facing API criteria need `http` evidence: an actual request/response transcript (curl -i) at the current revision, not only tests."""
    evaluator_guidance = """A user_facing API criterion cannot PASS on unit tests alone; require an http evidence record whose stdout shows status and body."""
    phase_docs = {"verifier_design": "verifier_design.md", "loop_diagnosis": "loop_diagnosis.md"}


ADAPTER_CLASS = ApiBackendAdapter
