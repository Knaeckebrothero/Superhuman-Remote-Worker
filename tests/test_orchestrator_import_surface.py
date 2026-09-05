"""The orchestrator must not import the agent runtime's API package.

``src/api/__init__.py`` eagerly imports ``.app``, which imports
``src.agent`` and therefore agent-only dependencies (``aiosqlite``,
LangGraph, ...). Those are absent from ``docker/Dockerfile.orchestrator``, so
a single ``from src.api...`` line anywhere under ``orchestrator/`` crash-loops
the production orchestrator at startup while the dev image (which installs the
agent requirements) stays green.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = REPO_ROOT / "src" / "orchestrator"
FORBIDDEN_ROOT = "agent.api"


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _orchestrator_sources() -> list[Path]:
    return [
        path
        for path in sorted(ORCHESTRATOR.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def test_orchestrator_never_imports_the_agent_api_package():
    offenders: list[str] = []
    for path in _orchestrator_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for module in _imported_modules(tree):
            if module == FORBIDDEN_ROOT or module.startswith(f"{FORBIDDEN_ROOT}."):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {module}")
    assert offenders == [], (
        "The orchestrator image has no agent-only dependencies. Move the shared "
        "type into src/shared/ instead of importing src.api:\n" + "\n".join(offenders)
    )


def test_pinned_job_recipient_is_importable_without_the_agent_runtime():
    """The shared module must stay light enough for the orchestrator image."""

    source = (REPO_ROOT / "src" / "shared" / "pinned_session_identity.py").read_text()
    tree = ast.parse(source)
    heavy = {
        module
        for module in _imported_modules(tree)
        if module.split(".")[0]
        in {"aiosqlite", "langchain", "langgraph", "playwright", "paramiko"}
    }
    assert heavy == set()

    from shared.pinned_session_identity import (
        PinnedJobRecipient,
        pinned_job_recipient_matches,
    )

    recipient = PinnedJobRecipient(
        expected_agent_id="a",
        expected_pod_uid=None,
        expected_process_generation="g",
        expected_job_id="j",
    )
    assert pinned_job_recipient_matches(
        recipient, agent_id="a", pod_uid=None, process_generation="g", job_id="j"
    )
    assert not pinned_job_recipient_matches(
        recipient, agent_id="a", pod_uid=None, process_generation="g2", job_id="j"
    )


@pytest.mark.parametrize("name", ("PinnedJobRecipient", "pinned_job_recipient_matches"))
def test_agent_api_models_still_re_export_the_recipient(name):
    import agent.api.models as models

    assert hasattr(models, name)
