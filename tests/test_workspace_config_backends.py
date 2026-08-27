"""Tests for WorkspaceConfig backend validation + the virtual-tier mounts field.

Pins the loader-level contract for the two new no-workspace tiers
(no_workspace_agent_mode.md §4): ``virtual`` and ``none`` are accepted
alongside ``sandbox``/``vm``, legacy aliases still map, bogus values still
raise, and the object-store ``mounts`` payload survives parsing.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.loader import WorkspaceConfig, load_agent_config_from_dict  # noqa: E402

_BASE = {"agent_id": "a", "display_name": "A"}


class TestBackendValidation:
    def test_default_is_sandbox(self):
        assert WorkspaceConfig().backend == "sandbox"

    @pytest.mark.parametrize("backend", ["sandbox", "vm", "virtual", "none"])
    def test_valid_backends_accepted(self, backend):
        assert WorkspaceConfig(backend=backend).backend == backend

    @pytest.mark.parametrize(
        "legacy,resolved", [("remote", "sandbox"), ("container", "sandbox")]
    )
    def test_legacy_aliases_map(self, legacy, resolved):
        assert WorkspaceConfig(backend=legacy).backend == resolved

    def test_invalid_backend_rejected(self):
        with pytest.raises(ValueError, match="Invalid workspace.backend"):
            WorkspaceConfig(backend="bogus")

    def test_invalid_backend_message_lists_options(self):
        with pytest.raises(ValueError, match="virtual"):
            WorkspaceConfig(backend="bogus")


class TestMountsField:
    def test_mounts_defaults_none(self):
        assert WorkspaceConfig().mounts is None

    def test_mounts_accepts_list(self):
        mounts = [{"rclone_spec": {"type": "s3"}, "prefix": "jobs/x/"}]
        assert WorkspaceConfig(backend="virtual", mounts=mounts).mounts == mounts


class TestDictParsing:
    def test_virtual_with_mounts_parsed(self):
        cfg = load_agent_config_from_dict(
            {
                **_BASE,
                "workspace": {
                    "backend": "virtual",
                    "mounts": [{"rclone_spec": {"type": "s3"}, "prefix": "jobs/x/"}],
                },
            }
        )
        assert cfg.workspace.backend == "virtual"
        assert cfg.workspace.mounts == [
            {"rclone_spec": {"type": "s3"}, "prefix": "jobs/x/"}
        ]

    def test_none_backend_parsed(self):
        cfg = load_agent_config_from_dict({**_BASE, "workspace": {"backend": "none"}})
        assert cfg.workspace.backend == "none"
        assert cfg.workspace.mounts is None

    def test_legacy_remote_parsed_to_sandbox(self):
        cfg = load_agent_config_from_dict({**_BASE, "workspace": {"backend": "remote"}})
        assert cfg.workspace.backend == "sandbox"
