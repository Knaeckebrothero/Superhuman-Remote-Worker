from __future__ import annotations

from pathlib import Path

from src.tools.context import ToolContext
from src.tools.workspace.files import create_file_tools
from src.tools.workspace.filesystem import create_filesystem_tools


class FakeWorkspace:
    def __init__(self) -> None:
        self.is_initialized = True
        self.search_calls: list[tuple[str, str, bool]] = []
        self.exists_calls: list[str] = []

    def search_files(
        self, query: str, path: str = "", case_sensitive: bool = False
    ) -> list[dict]:
        self.search_calls.append((query, path, case_sensitive))
        return [{"path": "notes/todo.md", "line_number": 1, "line": "invoice"}]

    def exists(self, path: str) -> bool:
        self.exists_calls.append(path)
        return True

    def get_path(self, path: str) -> Path:
        return Path("/workspace") / path

    def read_file(self, path: str) -> str:
        return "hello\n"


class FakeCloudMountManager:
    def __init__(self, cache_message: str | None = None) -> None:
        self.cache_message = cache_message

    def cache_limit_message(self) -> str | None:
        return self.cache_message


def _context(workspace: FakeWorkspace, cloud_mount: dict | None = None) -> ToolContext:
    config = {
        "max_read_size": 1024,
        "max_search_results": 50,
    }
    if cloud_mount is not None:
        config["cloud_mount"] = cloud_mount
    return ToolContext(workspace_manager=workspace, config=config)


def test_search_files_blocks_workspace_root_when_cloud_mount_active():
    workspace = FakeWorkspace()
    tools = create_filesystem_tools(_context(workspace, {"active": True}))
    search_files = next(tool for tool in tools if tool.name == "search_files")

    result = search_files.invoke({"query": "invoice", "path": ""})

    assert "Cloud scan guard" in result
    assert workspace.search_calls == []


def test_search_files_allows_non_cloud_workspace_path():
    workspace = FakeWorkspace()
    tools = create_filesystem_tools(_context(workspace, {"active": True}))
    search_files = next(tool for tool in tools if tool.name == "search_files")

    result = search_files.invoke({"query": "invoice", "path": "notes"})

    assert "Search results for 'invoice'" in result
    assert workspace.search_calls == [("invoice", "notes", False)]


def test_read_file_blocks_cloud_path_when_cache_limit_reached():
    workspace = FakeWorkspace()
    tools = create_file_tools(
        _context(
            workspace,
            {
                "active": True,
                "_manager": FakeCloudMountManager("Cloud cache guard: full"),
            },
        )
    )
    read_file = next(tool for tool in tools if tool.name == "read_file")

    result = read_file.invoke({"path": "cloud/notes/todo.md"})

    assert "Cloud cache guard: full" in result
    assert workspace.exists_calls == []
