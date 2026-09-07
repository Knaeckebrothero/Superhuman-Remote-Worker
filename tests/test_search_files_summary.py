"""Render tests for the search_files tool's result summary line.

Regression coverage for the "server-side capped" branch introduced alongside
SEARCH_RESULT_HARD_CAP (src/core/workspace_backend.py): when a backend's
search_files() returns a result set that hit the hard cap, the display must
say so honestly instead of reporting a plain count that understates how many
matches actually exist.
"""

from shared.runtime.core.workspace_backend import SEARCH_RESULT_HARD_CAP
from agent.tools.context import ToolContext
from agent.tools.workspace.filesystem import create_filesystem_tools


class _StubWorkspace:
    """A WorkspaceManager stand-in whose search_files() returns a fixed
    number of matches, regardless of query."""

    is_initialized = True

    def __init__(self, result_count: int) -> None:
        self._result_count = result_count
        self.search_calls: list[tuple[str, str, bool, list[str] | None]] = []

    def search_files(
        self,
        query: str,
        path: str = "",
        case_sensitive: bool = False,
        exclude_dirs: list[str] | None = None,
    ):
        self.search_calls.append((query, path, case_sensitive, exclude_dirs))
        return [
            {"path": "file.txt", "line_number": i + 1, "line": f"match {i}"}
            for i in range(self._result_count)
        ]


def _context(workspace: _StubWorkspace) -> ToolContext:
    return ToolContext(
        workspace_manager=workspace,
        config={"max_search_results": 50},
    )


def _search_files_tool(workspace: _StubWorkspace):
    tools = create_filesystem_tools(_context(workspace))
    return next(t for t in tools if t.name == "search_files")


def test_summary_shows_server_side_capped_at_hard_cap():
    """Exactly SEARCH_RESULT_HARD_CAP results (the backend's grep pipeline
    was capped) must render the honest "N+ (capped)" summary, not a bare
    count equal to the cap."""
    workspace = _StubWorkspace(SEARCH_RESULT_HARD_CAP)
    result = _search_files_tool(workspace).invoke({"query": "role"})

    assert (
        f"[Showing 50 of {SEARCH_RESULT_HARD_CAP}+ matches (server-side capped)]"
        in result
    )


def test_summary_shows_plain_count_below_hard_cap():
    """A result set above the display cap but below the hard cap keeps the
    plain "Showing X of Y matches" summary (no false "capped" claim)."""
    workspace = _StubWorkspace(SEARCH_RESULT_HARD_CAP - 1)
    result = _search_files_tool(workspace).invoke({"query": "role"})

    assert f"[Showing 50 of {SEARCH_RESULT_HARD_CAP - 1} matches]" in result
    assert "capped" not in result


def test_summary_shows_plain_count_above_hard_cap():
    """A result set above the hard cap (from an uncapping backend) must show
    the plain accurate count, not a false "capped" message. The cap applies
    only to RemoteBackend; Scratch/Virtual/Subdir backends may return more."""
    above_cap = SEARCH_RESULT_HARD_CAP + 500
    workspace = _StubWorkspace(above_cap)
    result = _search_files_tool(workspace).invoke({"query": "role"})

    assert f"[Showing 50 of {above_cap} matches]" in result
    assert "capped" not in result


def test_search_files_forwards_exclude_dirs_to_workspace():
    """The search_files tool passes exclude_dirs through WorkspaceManager."""
    workspace = _StubWorkspace(1)
    _search_files_tool(workspace).invoke(
        {"query": "role", "path": "src", "exclude_dirs": ["node_modules", ".git"]}
    )

    assert workspace.search_calls == [("role", "src", False, ["node_modules", ".git"])]
