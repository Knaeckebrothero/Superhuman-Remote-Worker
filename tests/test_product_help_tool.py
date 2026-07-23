"""Managed, workspace-independent SRW product-guide reader."""

from types import SimpleNamespace

from src.core.skill_resolution import (
    APP_GUIDE_LOADER_TOOL,
    APP_GUIDE_SKILL,
    skill_bundle_digest,
)
from src.tools.context import ToolContext
from src.tools.product_help import create_product_help_tools
from src.tools.registry import filter_tools_by_backend, load_tools


def _context(files: dict[str, str] | None = None) -> ToolContext:
    files = files or {
        "SKILL.md": (
            "---\nname: app-guide\ndescription: product help\n---\n"
            "ROUTE USING THE INDEX."
        ),
        "references/overview.md": "# Overview\nCURRENT OVERVIEW",
        "references/sessions.md": "# Sessions\nCURRENT SESSION HELP",
    }
    digest = skill_bundle_digest(files)
    return ToolContext(
        config={
            "_resolved_skills": {
                "menu": [
                    {
                        "name": APP_GUIDE_SKILL,
                        "system_managed": True,
                        "loader_tool": APP_GUIDE_LOADER_TOOL,
                        "bundle_digest": digest,
                    }
                ],
                "files": {APP_GUIDE_SKILL: files},
            }
        }
    )


def _reader(context: ToolContext):
    return {tool.name: tool for tool in create_product_help_tools(context)}[
        APP_GUIDE_LOADER_TOOL
    ]


def test_reader_works_without_a_workspace_and_lists_current_topics():
    context = _context()
    assert context.workspace_manager is None

    out = _reader(context).invoke({"topic_id": "index"})

    assert "ROUTE USING THE INDEX" in out
    assert "[available topic IDs]" in out
    assert "overview" in out and "sessions" in out
    assert "sha256:" in out


def test_reader_returns_procedure_and_one_focused_reference():
    out = _reader(_context()).invoke({"topic_id": "sessions"})

    assert "[guide procedure]" in out
    assert "ROUTE USING THE INDEX" in out
    assert "[product guide topic: sessions]" in out
    assert "CURRENT SESSION HELP" in out
    assert "CURRENT OVERVIEW" not in out


def test_reader_rejects_paths_and_unknown_topics_without_reading_them():
    reader = _reader(_context())

    traversal = reader.invoke({"topic_id": "../sessions"})
    unknown = reader.invoke({"topic_id": "not-a-topic"})

    assert "file paths are not accepted" in traversal
    assert "Unknown product-guide topic" in unknown
    assert "CURRENT SESSION HELP" not in traversal
    assert "CURRENT SESSION HELP" not in unknown


def test_reader_fails_closed_when_bundle_digest_changes():
    context = _context()
    context.config["_resolved_skills"]["files"][APP_GUIDE_SKILL][
        "references/sessions.md"
    ] = "MUTATED AFTER RESOLUTION"

    out = _reader(context).invoke({"topic_id": "sessions"})

    assert "unavailable" in out.lower()
    assert "MUTATED AFTER RESOLUTION" not in out


def test_product_help_tool_survives_none_backend_filter_and_loads():
    none_backend = SimpleNamespace(
        supports_shell=False,
        supports_file_tools=False,
        supports_canvas_presentation=False,
    )

    assert filter_tools_by_backend([APP_GUIDE_LOADER_TOOL], none_backend) == [
        APP_GUIDE_LOADER_TOOL
    ]
    loaded = load_tools([APP_GUIDE_LOADER_TOOL], _context())
    assert [tool.name for tool in loaded] == [APP_GUIDE_LOADER_TOOL]
