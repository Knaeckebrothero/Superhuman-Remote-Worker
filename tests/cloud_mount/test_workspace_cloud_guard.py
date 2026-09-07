from __future__ import annotations

from pathlib import Path

from agent.tools.context import ToolContext
from agent.tools.workspace.files import create_file_tools
from agent.tools.workspace.filesystem import create_filesystem_tools


class FakeWorkspace:
    def __init__(self, exists_result: bool = True) -> None:
        self.is_initialized = True
        self.search_calls: list[tuple[str, str, bool, list[str] | None]] = []
        self.exists_calls: list[str] = []
        self.write_calls: list[tuple[str, str]] = []
        self.exists_result = exists_result

    def search_files(
        self,
        query: str,
        path: str = "",
        case_sensitive: bool = False,
        exclude_dirs: list[str] | None = None,
    ) -> list[dict]:
        self.search_calls.append((query, path, case_sensitive, exclude_dirs))
        return [{"path": "notes/todo.md", "line_number": 1, "line": "invoice"}]

    def exists(self, path: str) -> bool:
        self.exists_calls.append(path)
        return self.exists_result

    def get_path(self, path: str) -> Path:
        return Path("/workspace") / path

    def read_file(self, path: str) -> str:
        return "hello\n"

    def write_file(self, path: str, content: str) -> None:
        self.write_calls.append((path, content))


class FakeCloudMountManager:
    def __init__(self, cache_message: str | None = None) -> None:
        self.cache_message = cache_message

    def cache_limit_message(self) -> str | None:
        return self.cache_message


class FakeOverlayManager:
    def __init__(self, quota_message: str | None = None) -> None:
        self.quota_message = quota_message

    def quota_guard_message(self) -> str | None:
        return self.quota_message


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
    assert workspace.search_calls == [("invoice", "notes", False, None)]


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


def test_write_file_blocks_cloud_path_when_upperdir_over_quota():
    workspace = FakeWorkspace()
    tools = create_file_tools(
        _context(
            workspace,
            {
                "active": True,
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    write_file = next(tool for tool in tools if tool.name == "write_file")

    result = write_file.invoke({"path": "cloud/notes/todo.md", "content": "hi"})

    assert "Cloud staging guard: full" in result
    assert workspace.exists_calls == []
    assert workspace.write_calls == []


def test_edit_file_blocks_cloud_path_when_upperdir_over_quota():
    workspace = FakeWorkspace()
    tools = create_file_tools(
        _context(
            workspace,
            {
                "active": True,
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    edit_file = next(tool for tool in tools if tool.name == "edit_file")

    result = edit_file.invoke(
        {"path": "cloud/notes/todo.md", "old_string": "hello", "new_string": "hi"}
    )

    assert "Cloud staging guard: full" in result
    assert workspace.exists_calls == []


def test_write_file_not_blocked_when_overlay_manager_absent():
    workspace = FakeWorkspace(exists_result=False)
    tools = create_file_tools(_context(workspace, {"active": True}))
    write_file = next(tool for tool in tools if tool.name == "write_file")

    result = write_file.invoke({"path": "cloud/notes/todo.md", "content": "hi"})

    assert "Cloud staging guard" not in result
    # Absolute, not the caller's relative string — see
    # knowledge-base/knowledge/issues/deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md
    assert result == "Written: /workspace/cloud/notes/todo.md (2 chars)"
    assert workspace.write_calls == [("cloud/notes/todo.md", "hi")]


def test_read_file_not_blocked_by_upperdir_guard():
    workspace = FakeWorkspace()
    tools = create_file_tools(
        _context(
            workspace,
            {
                "active": True,
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    read_file = next(tool for tool in tools if tool.name == "read_file")

    result = read_file.invoke({"path": "cloud/notes/todo.md"})

    assert "Cloud staging guard" not in result
    assert workspace.exists_calls == ["cloud/notes/todo.md"]
