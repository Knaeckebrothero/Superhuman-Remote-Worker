"""Regression checks for the MCP job-creation/dispatch contract."""

import ast
from pathlib import Path

from src.shared.orch_surface.formatters import format_created_job


SERVER_PATH = Path(__file__).parent.parent / "orchestrator" / "mcp" / "server.py"


def _async_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"MCP function {name!r} not found")


def test_created_job_response_describes_automatic_dispatch():
    rendered = format_created_job(
        {"id": "job-1", "status": "created", "description": "research"},
        "scholar",
    )

    assert "Queued for automatic workspace provisioning" in rendered
    assert "Next step: Use assign_job" not in rendered


def test_mcp_creation_signatures_expose_deliverable_contract():
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))

    for function_name in ("create_job", "create_project_job"):
        function = _async_function(tree, function_name)
        argument_names = {argument.arg for argument in function.args.args}
        assert "required_deliverables" in argument_names


def test_workspace_reader_promises_committed_gitea_state():
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    function = _async_function(tree, "get_workspace_file")
    doc = ast.get_docstring(function) or ""

    assert "Gitea-backed" in doc
    assert "committed state" in doc
