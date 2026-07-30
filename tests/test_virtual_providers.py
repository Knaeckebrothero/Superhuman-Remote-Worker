from src.core.virtual_dirs import SingleFileProvider, ToolsProvider
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
