from src.core.virtual_dirs import (
    SingleFileProvider,
    ToolsProvider,
    build_instruction_providers,
)
from src.tools.description_manager import generate_tool_index


class FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.description = description


def test_tools_provider_lists_index_and_each_tool():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "Reads a file.")])
    assert set(provider.entries()) == {"README.md", "read_file.md"}


def test_readme_matches_canonical_renderer():
    tools = [FakeTool("read_file", "Reads a file.")]
    provider = ToolsProvider(lambda: tools)
    assert provider.read("README.md") == generate_tool_index(["read_file"])


def test_tool_doc_contains_full_docstring():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "Reads a file fully.")])
    assert "Reads a file fully." in provider.read("read_file.md")


def test_unknown_tool_returns_none():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "d")])
    assert provider.read("gone.md") is None


def test_tool_list_changes_are_reflected_without_reregistration():
    """The workspace-upgrade re-derive changes the tool list mid-lifecycle."""
    tools = [FakeTool("read_file", "d")]
    provider = ToolsProvider(lambda: tools)
    assert "run_command.md" not in provider.entries()
    tools.append(FakeTool("run_command", "Runs a command."))
    assert "run_command.md" in provider.entries()
    assert "Runs a command." in provider.read("run_command.md")


def test_provider_flags():
    provider = ToolsProvider(lambda: [])
    assert provider.prefix == "tools" and provider.is_dir and not provider.writable


def test_single_file_provider_serves_one_entry():
    provider = SingleFileProvider("instructions.md", lambda: "# Instructions\n")
    assert set(provider.entries()) == {"instructions.md"}
    assert provider.read("instructions.md") == "# Instructions\n"
    assert provider.read("other.md") is None
    assert not provider.is_dir and not provider.writable


def test_single_file_provider_renders_lazily():
    calls = []

    def render():
        calls.append(1)
        return "body"

    provider = SingleFileProvider("task_brief.md", render)
    provider.read("task_brief.md")
    provider.read("task_brief.md")
    assert len(calls) == 2  # always live, never cached


def _providers(uploaded=None, template="TEMPLATE", brief="# Task Brief\n"):
    return {
        p.prefix: p
        for p in build_instruction_providers(
            uploaded=lambda: uploaded,
            template=lambda: template,
            brief=lambda: brief,
        )
    }


def test_builds_both_instruction_files():
    assert set(_providers()) == {"instructions.md", "task_brief.md"}


def test_uploaded_instructions_beat_the_template():
    provider = _providers(uploaded="UPLOADED")["instructions.md"]
    assert provider.read("instructions.md") == "UPLOADED"


def test_template_is_used_when_no_upload():
    provider = _providers(uploaded=None)["instructions.md"]
    assert provider.read("instructions.md") == "TEMPLATE"


def test_blank_upload_falls_back_to_template():
    provider = _providers(uploaded="   ")["instructions.md"]
    assert provider.read("instructions.md") == "TEMPLATE"


def test_source_precedence_is_inline_then_upload_then_template():
    """Mirrors the agent's `_uploaded_instructions` closure contract."""

    def resolver(inline, upload):
        def _resolve():
            if inline and inline.strip():
                return inline
            return upload

        return _resolve

    def _read(inline, upload):
        providers = {
            p.prefix: p
            for p in build_instruction_providers(
                uploaded=resolver(inline, upload),
                template=lambda: "TEMPLATE",
                brief=lambda: "",
            )
        }
        return providers["instructions.md"].read("instructions.md")

    assert _read("INLINE", "UPLOAD") == "INLINE"
    assert _read(None, "UPLOAD") == "UPLOAD"
    assert _read(None, None) == "TEMPLATE"


def test_task_brief_is_served_from_the_callable():
    provider = _providers(brief="# Task Brief\n\nDo the thing.")["task_brief.md"]
    assert "Do the thing." in provider.read("task_brief.md")


def test_instruction_providers_are_read_only():
    for provider in _providers().values():
        assert not provider.writable and not provider.is_dir
