"""Exercise the checked-in architecture contracts with allowed and poisoned imports."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def boundary_tree(tmp_path):
    (tmp_path / "pyproject.toml").write_text((REPO / "pyproject.toml").read_text())
    for package in (
        "agent",
        "orchestrator",
        "orchestrator/routers",
        "mcp_server",
        "vm_controller",
        "shared",
        "shared/runtime",
        "shared/contracts",
    ):
        directory = tmp_path / "src" / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    for module, body in {
        "shared/value.py": "VALUE = 1\n",
        "shared/contracts/item.py": "from shared.value import VALUE\n",
        "shared/runtime/provider.py": "from shared.contracts.item import VALUE\n",
        "agent/app.py": "from shared.runtime.provider import VALUE\n",
        "orchestrator/app.py": "from shared.runtime.provider import VALUE\n",
        "orchestrator/main.py": "from orchestrator.app import VALUE\n",
        "orchestrator/routers/contacts.py": "from shared.value import VALUE\n",
        "orchestrator/routers/tables.py": "from shared.value import VALUE\n",
        "mcp_server/app.py": "from shared.contracts.item import VALUE\n",
        "vm_controller/app.py": "from shared.value import VALUE\n",
    }.items():
        (tmp_path / "src" / module).write_text(body)
    return tmp_path


def lint_boundaries(root):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--no-cache",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONSAFEPATH": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_allowed_runtime_and_lightweight_dependencies_pass(boundary_tree):
    result = lint_boundaries(boundary_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 8 kept, 0 broken" in result.stdout


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("agent/app.py", "orchestrator.app"),
        ("orchestrator/app.py", "agent.app"),
        ("mcp_server/app.py", "vm_controller.app"),
        ("vm_controller/app.py", "mcp_server.app"),
        ("shared/value.py", "agent.app"),
        ("shared/runtime/provider.py", "orchestrator.app"),
        ("shared/value.py", "shared.runtime.provider"),
        ("shared/contracts/item.py", "shared.runtime.provider"),
        ("shared/__init__.py", "shared.runtime.provider"),
        ("mcp_server/app.py", "shared.runtime.provider"),
        ("vm_controller/app.py", "shared.runtime.provider"),
        ("agent/app.py", "src.core.loader"),
        ("orchestrator/app.py", "services.canvas"),
        ("orchestrator/app.py", "database.postgres"),
        ("orchestrator/routers/contacts.py", "orchestrator.main"),
        ("orchestrator/routers/tables.py", "orchestrator.main"),
        ("vm_controller/app.py", "headscale_client"),
    ],
)
def test_forbidden_dependency_fails_the_gate(boundary_tree, source, target):
    (boundary_tree / "src" / source).write_text(f"import {target}\n")
    result = lint_boundaries(boundary_tree)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "BROKEN" in result.stdout
