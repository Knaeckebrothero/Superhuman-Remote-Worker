"""File tools must name the anchor they resolved against.

Regression suite for the gap that lost job bbce4bed's deliverable: the file
tools resolve relative paths against the workspace root while ``shell_execute``
resolves against the tab's cwd, so the same string denotes two different files.
``write_file`` used to return the caller's own string (``Written: output/x.md``),
which told the model nothing about which file it got.

See knowledge-base/knowledge/issues/deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.context import ToolContext
from agent.tools.workspace.files import create_file_tools


class FakeWorkspace:
    """Minimal WorkspaceManager double; ``get_path`` is the anchor under test."""

    def __init__(self, root: str = "/home/agent-host/workspace") -> None:
        self.is_initialized = True
        self.root = root
        self.write_calls: list[tuple[str, str]] = []

    def get_path(self, path: str = "") -> Path:
        return Path(self.root) / path

    def exists(self, path: str) -> bool:
        return False

    def read_file(self, path: str) -> str:
        return ""

    def write_file(self, path: str, content: str) -> None:
        self.write_calls.append((path, content))


class ExplodingPathWorkspace(FakeWorkspace):
    """A backend whose path resolution fails — the aid must not break writes."""

    def get_path(self, path: str = "") -> Path:
        raise RuntimeError("backend unavailable")


def _write_tool(workspace: FakeWorkspace):
    context = ToolContext(
        workspace_manager=workspace,
        config={"max_read_size": 1024, "max_write_words": 10_000},
    )
    return next(t for t in create_file_tools(context) if t.name == "write_file")


class TestWriteFileReportsAbsolutePath:
    def test_returns_absolute_not_the_callers_string(self):
        """The core fix: the result names the resolved file, not the input."""
        ws = FakeWorkspace()
        result = _write_tool(ws).invoke(
            {"path": "output/ui_recovery_report.md", "content": "x"}
        )
        assert "/home/agent-host/workspace/output/ui_recovery_report.md" in result
        assert result != "Written: output/ui_recovery_report.md"

    def test_distinguishes_two_roots_for_the_same_relative_string(self):
        """`output/x.md` under two roots must produce two different results.

        This is the bbce4bed failure in miniature: the file tools wrote to the
        workspace root while the shell, cd'd into `repo/`, looked under it.
        """
        rel = "output/report.md"
        at_root = _write_tool(FakeWorkspace()).invoke({"path": rel, "content": "x"})
        at_repo = _write_tool(
            FakeWorkspace(root="/home/agent-host/workspace/repo")
        ).invoke({"path": rel, "content": "x"})
        assert at_root != at_repo
        assert at_root.endswith("/workspace/output/report.md (1 chars)")
        assert at_repo.endswith("/workspace/repo/output/report.md (1 chars)")

    def test_does_not_mangle_dotfiles(self):
        """Prefix-stripping must not eat a leading dot from `.hidden/file`."""
        ws = FakeWorkspace()
        result = _write_tool(ws).invoke({"path": ".hidden/file.md", "content": "x"})
        assert "/workspace/.hidden/file.md" in result

    def test_reports_content_size(self):
        ws = FakeWorkspace()
        result = _write_tool(ws).invoke({"path": "a.md", "content": "hello"})
        assert "(5 chars)" in result

    def test_still_writes_when_path_resolution_fails(self):
        """A diagnostic aid must never be the reason a write fails."""
        ws = ExplodingPathWorkspace()
        result = _write_tool(ws).invoke({"path": "output/x.md", "content": "x"})
        assert ws.write_calls == [("output/x.md", "x")]
        assert "output/x.md" in result


class TestWriteFileDocstringNamesTheAnchor:
    def test_docstring_states_the_workspace_root_anchor(self):
        """The tool contract must say what a relative path is relative to.

        The old docstring said only "Relative path for the file", leaving the
        anchor unstated — the model had no way to learn it.
        """
        doc = _write_tool(FakeWorkspace()).description
        assert "workspace root" in doc
        assert "cd" in doc


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
