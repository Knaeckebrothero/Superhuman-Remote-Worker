"""Tests for the lite-backend factory (keyed on workspace.backend).

The factory is what the worker/session bootstrap seams call once they detect a
LITE_BACKENDS value. It uses a SimpleNamespace stand-in for the workspace
config so it stays decoupled from the loader dataclass — the factory only needs
``.backend`` and ``.mounts``. The 'memory' object-store type lets virtual mode
be exercised end-to-end here without rclone or a network.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.factory import (  # noqa: E402
    LITE_BACKENDS,
    create_lite_backend,
    is_lite_backend,
)
from src.core.backends.scratch import ScratchBackend  # noqa: E402
from src.core.backends.virtual import VirtualWorkspaceBackend  # noqa: E402


class TestLiteBackendSet:
    def test_lite_backends_membership(self):
        assert is_lite_backend("virtual") is True
        assert is_lite_backend("none") is True

    def test_sandbox_and_vm_are_not_lite(self):
        assert is_lite_backend("sandbox") is False
        assert is_lite_backend("vm") is False
        assert "sandbox" not in LITE_BACKENDS
        assert "vm" not in LITE_BACKENDS


class TestNoneMode:
    def test_creates_scratch_backend(self):
        cfg = SimpleNamespace(backend="none", mounts=None)
        b = create_lite_backend(cfg, job_id="job-1")
        try:
            assert isinstance(b, ScratchBackend)
        finally:
            b.disconnect()


class TestVirtualMode:
    def test_creates_virtual_backend_with_prefix(self):
        cfg = SimpleNamespace(
            backend="virtual",
            mounts=[{"rclone_spec": {"type": "memory"}, "prefix": "jobs/x/"}],
        )
        b = create_lite_backend(cfg, job_id="job-1")
        assert isinstance(b, VirtualWorkspaceBackend)
        assert b.root == "jobs/x/"

    def test_virtual_backend_is_wired_to_store(self):
        cfg = SimpleNamespace(
            backend="virtual",
            mounts=[{"rclone_spec": {"type": "memory"}, "prefix": "jobs/x/"}],
        )
        b = create_lite_backend(cfg, job_id="job-1")
        b.write_file("hello.txt", "world")
        assert b.read_file("hello.txt") == "world"

    def test_default_prefix_from_job_id_when_absent(self):
        cfg = SimpleNamespace(
            backend="virtual",
            mounts=[{"rclone_spec": {"type": "memory"}}],
        )
        b = create_lite_backend(cfg, job_id="abc-123")
        assert b.root == "jobs/abc-123/"

    def test_virtual_without_mounts_raises(self):
        cfg = SimpleNamespace(backend="virtual", mounts=None)
        with pytest.raises(ValueError, match="requires workspace.mounts"):
            create_lite_backend(cfg, job_id="job-1")


class TestNonLiteRejected:
    def test_sandbox_rejected(self):
        cfg = SimpleNamespace(backend="sandbox", mounts=None)
        with pytest.raises(ValueError, match="non-lite backend"):
            create_lite_backend(cfg, job_id="job-1")
