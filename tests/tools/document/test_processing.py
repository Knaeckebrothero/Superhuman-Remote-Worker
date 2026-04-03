"""Tests for document processing remote workspace support."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.tools.document.processing import _local_copy


class TestLocalCopy:
    """Tests for _local_copy context manager — remote file fetching."""

    def test_no_workspace_yields_path_as_is(self):
        """Without a workspace, yield the path unchanged."""
        with _local_copy(None, "/some/path.pdf") as p:
            assert p == "/some/path.pdf"

    def test_local_backend_yields_resolved_path(self, tmp_path):
        """Local backend resolves through workspace.get_path()."""
        workspace = MagicMock()
        workspace.backend.host = None
        workspace.get_path.return_value = tmp_path / "documents" / "file.pdf"

        with _local_copy(workspace, "documents/file.pdf") as p:
            assert p == str(tmp_path / "documents" / "file.pdf")

        workspace.get_path.assert_called_once_with("documents/file.pdf")

    def test_remote_backend_fetches_to_tempfile(self):
        """Remote backend fetches file content to a temp file."""
        workspace = MagicMock()
        workspace.backend.host = "10.42.0.50"
        workspace.backend.read_file.return_value = b"%PDF-1.4 fake content"

        with _local_copy(workspace, "documents/paper.pdf") as p:
            local_path = Path(p)
            assert local_path.exists()
            assert local_path.suffix == ".pdf"
            assert local_path.read_bytes() == b"%PDF-1.4 fake content"

        workspace.backend.read_file.assert_called_once_with(
            "documents/paper.pdf", binary=True
        )
        # Temp file cleaned up after context exit
        assert not Path(p).exists()

    def test_remote_backend_preserves_extension(self):
        """Temp file has the same extension as the source file."""
        workspace = MagicMock()
        workspace.backend.host = "10.42.0.50"
        workspace.backend.read_file.return_value = b"<html>test</html>"

        with _local_copy(workspace, "reference/page.html") as p:
            assert Path(p).suffix == ".html"

    def test_remote_backend_cleanup_on_error(self):
        """Temp file is cleaned up even if an exception occurs."""
        workspace = MagicMock()
        workspace.backend.host = "10.42.0.50"
        workspace.backend.read_file.return_value = b"content"

        temp_path = None
        with pytest.raises(ValueError):
            with _local_copy(workspace, "file.txt") as p:
                temp_path = p
                assert Path(p).exists()
                raise ValueError("Simulated error")

        assert temp_path is not None
        assert not Path(temp_path).exists()
