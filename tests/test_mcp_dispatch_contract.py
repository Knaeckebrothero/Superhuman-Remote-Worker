"""Regression checks for the MCP job-creation/dispatch contract."""

import ast
from pathlib import Path

from src.shared.orch_surface.formatters import format_created_job
from src.shared.orch_surface.jobs import get_descriptor


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


def test_created_job_response_reports_workspace_assignment() -> None:
    rendered = format_created_job(
        {
            "id": "job-1",
            "status": "created",
            "workspace_contract": {
                "requested_backend": "vm",
                "assigned_backend": "vm",
                "effective_backend": None,
                "state": "waiting",
            },
        },
        "developer",
    )

    assert "Workspace: requested=vm, assigned=vm, effective=pending" in rendered


def test_mcp_creation_signatures_expose_deliverable_contract():
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))

    assert (
        "required_deliverables"
        in get_descriptor("create_job").public_signature.parameters
    )
    function = _async_function(tree, "create_project_job")
    argument_names = {argument.arg for argument in function.args.args}
    assert "required_deliverables" in argument_names


def test_both_creation_tools_take_the_one_expert_selector():
    """`expert` accepts a bundled slug or a DB UUID on either tool, so a
    caller can pass any id `list_experts` printed without knowing which store
    it came from. The single-store aliases stay for existing callers.
    See knowledge-base/knowledge/issues/experts_one_catalogue_two_selection_paths.md."""
    signature = get_descriptor("create_job").public_signature.parameters
    assert {"expert", "config_name", "expert_id"} <= set(signature)

    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    function = _async_function(tree, "create_project_job")
    argument_names = {argument.arg for argument in function.args.args}
    assert {"expert", "config_name", "expert_id"} <= argument_names
    assert "list_experts" in (ast.get_docstring(function) or "")


def test_workspace_reader_promises_committed_gitea_state():
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    function = _async_function(tree, "get_workspace_file")
    doc = ast.get_docstring(function) or ""

    assert "Gitea-backed" in doc
    assert "committed state" in doc
