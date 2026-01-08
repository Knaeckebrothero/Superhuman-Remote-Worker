"""Unit tests for WorkspaceManager.

Tests the filesystem-based workspace management functionality.
"""

import pytest
import tempfile
import os
import sys
import importlib.util
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def _import_module_directly(module_path: Path, module_name: str):
    """Import a module directly without triggering __init__.py side effects."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# First import config (dependency of workspace_manager)
config_path = project_root / "src" / "core" / "config.py"
config_module = _import_module_directly(config_path, "src.core.config")

# Now import workspace_manager
workspace_manager_path = project_root / "src" / "agents" / "shared" / "workspace_manager.py"
workspace_manager_module = _import_module_directly(workspace_manager_path, "src.agents.shared.workspace_manager")

WorkspaceManager = workspace_manager_module.WorkspaceManager
WorkspaceConfig = workspace_manager_module.WorkspaceConfig
get_workspace_base_path = workspace_manager_module.get_workspace_base_path


@pytest.fixture
def temp_workspace():
    """Create a temporary directory for workspace testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_manager(temp_workspace):
    """Create a WorkspaceManager with a temporary base path."""
    ws = WorkspaceManager(
        job_id="test-job-123",
        base_path=temp_workspace,
    )
    ws.initialize()
    return ws


class TestWorkspaceConfig:
    """Tests for WorkspaceConfig."""

    def test_default_structure(self):
        """Test that default structure contains expected directories."""
        config = WorkspaceConfig()
        assert "plans" in config.structure
        assert "archive" in config.structure
        assert "documents" in config.structure
        assert "output" in config.structure

    def test_from_dict(self):
        """Test creating config from dictionary."""
        data = {
            "base_path": "/custom/path",
            "structure": ["custom1", "custom2"],
            "instructions_template": "test.md",
        }
        config = WorkspaceConfig.from_dict(data)
        assert config.base_path == "/custom/path"
        assert config.structure == ["custom1", "custom2"]
        assert config.instructions_template == "test.md"

    def test_from_dict_defaults(self):
        """Test that from_dict uses defaults for missing keys."""
        config = WorkspaceConfig.from_dict({})
        assert config.base_path is None
        assert len(config.structure) > 0  # Default structure


class TestWorkspaceBasePath:
    """Tests for get_workspace_base_path function."""

    def test_env_var_override(self, monkeypatch, temp_workspace):
        """Test that WORKSPACE_PATH env var takes precedence."""
        monkeypatch.setenv("WORKSPACE_PATH", str(temp_workspace))
        path = get_workspace_base_path()
        assert path == temp_workspace

    def test_no_env_var(self, monkeypatch):
        """Test behavior when no env var is set."""
        monkeypatch.delenv("WORKSPACE_PATH", raising=False)
        path = get_workspace_base_path()
        # Should return either /workspace (container) or ./workspace (dev)
        assert path is not None


class TestWorkspaceManagerInitialization:
    """Tests for WorkspaceManager initialization."""

    def test_initialization(self, temp_workspace):
        """Test basic workspace initialization."""
        ws = WorkspaceManager(
            job_id="test-123",
            base_path=temp_workspace,
        )
        ws.initialize()

        assert ws.is_initialized
        assert ws.path.exists()
        assert ws.path.name == "job_test-123"

    def test_creates_subdirectories(self, temp_workspace):
        """Test that initialization creates all configured subdirectories."""
        config = WorkspaceConfig(structure=["foo", "bar", "baz/nested"])
        ws = WorkspaceManager(
            job_id="test-456",
            config=config,
            base_path=temp_workspace,
        )
        ws.initialize()

        assert (ws.path / "foo").exists()
        assert (ws.path / "bar").exists()
        assert (ws.path / "baz" / "nested").exists()

    def test_idempotent_initialization(self, workspace_manager):
        """Test that initialize can be called multiple times safely."""
        ws = workspace_manager
        ws.write_file("test.txt", "content")

        # Re-initialize should not delete existing files
        ws.initialize()

        assert ws.read_file("test.txt") == "content"


class TestWorkspaceManagerPaths:
    """Tests for path handling."""

    def test_get_path_root(self, workspace_manager):
        """Test getting workspace root path."""
        path = workspace_manager.get_path()
        assert path == workspace_manager.path

    def test_get_path_subdir(self, workspace_manager):
        """Test getting path to subdirectory."""
        path = workspace_manager.get_path("plans")
        assert path == workspace_manager.path / "plans"
        assert path.exists()

    def test_get_path_nested(self, workspace_manager):
        """Test getting nested path."""
        path = workspace_manager.get_path("documents/sources")
        assert path == workspace_manager.path / "documents" / "sources"

    def test_get_path_traversal_blocked(self, workspace_manager):
        """Test that path traversal attempts are blocked."""
        with pytest.raises(ValueError, match="escapes workspace"):
            workspace_manager.get_path("../outside")

        with pytest.raises(ValueError, match="escapes workspace"):
            workspace_manager.get_path("plans/../../outside")

    def test_exists(self, workspace_manager):
        """Test exists method."""
        assert workspace_manager.exists("plans")
        assert not workspace_manager.exists("nonexistent")


class TestWorkspaceManagerFileOperations:
    """Tests for file read/write operations."""

    def test_write_and_read_file(self, workspace_manager):
        """Test basic file write and read."""
        content = "Hello, World!"
        workspace_manager.write_file("test.txt", content)

        result = workspace_manager.read_file("test.txt")
        assert result == content

    def test_write_creates_parent_dirs(self, workspace_manager):
        """Test that write_file creates parent directories."""
        content = "Nested content"
        workspace_manager.write_file("deep/nested/file.txt", content)

        assert workspace_manager.read_file("deep/nested/file.txt") == content

    def test_read_nonexistent_file(self, workspace_manager):
        """Test reading a file that doesn't exist."""
        with pytest.raises(FileNotFoundError):
            workspace_manager.read_file("nonexistent.txt")

    def test_read_directory(self, workspace_manager):
        """Test that reading a directory raises an error."""
        with pytest.raises(ValueError, match="Not a file"):
            workspace_manager.read_file("plans")

    def test_append_file(self, workspace_manager):
        """Test appending to a file."""
        workspace_manager.write_file("append.txt", "Line 1\n")
        workspace_manager.append_file("append.txt", "Line 2\n")

        content = workspace_manager.read_file("append.txt")
        assert content == "Line 1\nLine 2\n"

    def test_append_creates_file(self, workspace_manager):
        """Test that append creates file if it doesn't exist."""
        workspace_manager.append_file("new.txt", "First line")

        content = workspace_manager.read_file("new.txt")
        assert content == "First line"

    def test_delete_file(self, workspace_manager):
        """Test deleting a file."""
        workspace_manager.write_file("delete_me.txt", "content")
        assert workspace_manager.exists("delete_me.txt")

        result = workspace_manager.delete_file("delete_me.txt")
        assert result is True
        assert not workspace_manager.exists("delete_me.txt")

    def test_delete_nonexistent(self, workspace_manager):
        """Test deleting a file that doesn't exist."""
        result = workspace_manager.delete_file("nonexistent.txt")
        assert result is False

    def test_delete_empty_directory(self, workspace_manager):
        """Test deleting an empty directory."""
        workspace_manager.write_file("empty_dir/placeholder.txt", "x")
        workspace_manager.delete_file("empty_dir/placeholder.txt")

        result = workspace_manager.delete_file("empty_dir")
        assert result is True

    def test_delete_nonempty_directory(self, workspace_manager):
        """Test that deleting non-empty directory raises error."""
        workspace_manager.write_file("nonempty/file.txt", "content")

        with pytest.raises(ValueError, match="non-empty"):
            workspace_manager.delete_file("nonempty")


class TestWorkspaceManagerListing:
    """Tests for file listing operations."""

    def test_list_files(self, workspace_manager):
        """Test listing files in a directory."""
        workspace_manager.write_file("plans/plan1.md", "content")
        workspace_manager.write_file("plans/plan2.md", "content")

        files = workspace_manager.list_files("plans")
        assert "plans/plan1.md" in files
        assert "plans/plan2.md" in files

    def test_list_files_with_pattern(self, workspace_manager):
        """Test listing files with glob pattern."""
        workspace_manager.write_file("notes/note.md", "md")
        workspace_manager.write_file("notes/note.txt", "txt")

        md_files = workspace_manager.list_files("notes", pattern="*.md")
        assert len(md_files) == 1
        assert "notes/note.md" in md_files

    def test_list_empty_directory(self, workspace_manager):
        """Test listing an empty directory."""
        files = workspace_manager.list_files("archive")
        assert files == []

    def test_list_nonexistent_directory(self, workspace_manager):
        """Test listing a directory that doesn't exist."""
        files = workspace_manager.list_files("nonexistent")
        assert files == []

    def test_list_shows_directories_with_slash(self, workspace_manager):
        """Test that directories are listed with trailing slash."""
        workspace_manager.write_file("parent/child/file.txt", "content")

        files = workspace_manager.list_files("parent")
        assert "parent/child/" in files


class TestWorkspaceManagerSearch:
    """Tests for file search operations."""

    def test_search_files(self, workspace_manager):
        """Test searching for text in files."""
        workspace_manager.write_file("notes/note1.md", "This mentions GoBD compliance")
        workspace_manager.write_file("notes/note2.md", "This is about GDPR")
        workspace_manager.write_file("notes/note3.md", "Another GoBD reference")

        results = workspace_manager.search_files("GoBD")
        assert len(results) == 2

        paths = [r["path"] for r in results]
        assert "notes/note1.md" in paths
        assert "notes/note3.md" in paths

    def test_search_case_insensitive(self, workspace_manager):
        """Test case-insensitive search."""
        workspace_manager.write_file("test.txt", "HELLO world")

        results = workspace_manager.search_files("hello", case_sensitive=False)
        assert len(results) == 1

    def test_search_case_sensitive(self, workspace_manager):
        """Test case-sensitive search."""
        workspace_manager.write_file("test.txt", "HELLO world")

        results = workspace_manager.search_files("hello", case_sensitive=True)
        assert len(results) == 0

        results = workspace_manager.search_files("HELLO", case_sensitive=True)
        assert len(results) == 1

    def test_search_returns_line_info(self, workspace_manager):
        """Test that search results include line information."""
        workspace_manager.write_file("multi.txt", "Line 1\nLine 2 with match\nLine 3")

        results = workspace_manager.search_files("match")
        assert len(results) == 1
        assert results[0]["line_number"] == 2
        assert "Line 2 with match" in results[0]["line"]


class TestWorkspaceManagerCleanup:
    """Tests for workspace cleanup."""

    def test_cleanup(self, temp_workspace):
        """Test workspace cleanup removes everything."""
        ws = WorkspaceManager(job_id="cleanup-test", base_path=temp_workspace)
        ws.initialize()
        ws.write_file("test.txt", "content")

        assert ws.path.exists()

        result = ws.cleanup()
        assert result is True
        assert not ws.path.exists()

    def test_cleanup_nonexistent(self, temp_workspace):
        """Test cleanup when workspace doesn't exist."""
        ws = WorkspaceManager(job_id="never-created", base_path=temp_workspace)

        result = ws.cleanup()
        assert result is False


class TestWorkspaceManagerUtilities:
    """Tests for utility methods."""

    def test_get_size_file(self, workspace_manager):
        """Test getting size of a file."""
        content = "Hello, World!"  # 13 bytes
        workspace_manager.write_file("sized.txt", content)

        size = workspace_manager.get_size("sized.txt")
        assert size == len(content.encode("utf-8"))

    def test_get_size_directory(self, workspace_manager):
        """Test getting total size of a directory."""
        workspace_manager.write_file("dir/file1.txt", "123")  # 3 bytes
        workspace_manager.write_file("dir/file2.txt", "4567")  # 4 bytes

        size = workspace_manager.get_size("dir")
        assert size == 7

    def test_get_summary(self, workspace_manager):
        """Test getting workspace summary."""
        workspace_manager.write_file("plans/plan.md", "content")
        workspace_manager.write_file("notes/note.md", "content")

        summary = workspace_manager.get_summary()

        assert summary["job_id"] == "test-job-123"
        assert summary["exists"] is True
        assert summary["total_files"] >= 2
        assert "plans" in summary["directories"]

    def test_repr(self, workspace_manager):
        """Test string representation."""
        repr_str = repr(workspace_manager)
        assert "test-job-123" in repr_str
        assert "WorkspaceManager" in repr_str


class TestWorkspaceManagerUnicode:
    """Tests for unicode handling."""

    def test_unicode_content(self, workspace_manager):
        """Test writing and reading unicode content."""
        content = "日本語テキスト 🎉 émojis"
        workspace_manager.write_file("unicode.txt", content)

        result = workspace_manager.read_file("unicode.txt")
        assert result == content

    def test_search_unicode(self, workspace_manager):
        """Test searching for unicode text."""
        workspace_manager.write_file("unicode.md", "Contains 日本語 text")

        results = workspace_manager.search_files("日本語")
        assert len(results) == 1
