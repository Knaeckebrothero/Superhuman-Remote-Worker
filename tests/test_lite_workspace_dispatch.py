"""S2 — orchestrator-side dispatch/validation for the lite workspace tiers.

Covers ``docs/features/no_workspace_agent_mode.md`` §4/§11 (orchestrator side):
the pure config helpers the job-dispatch and session-attach seams share, the
skip-provisioning decision, the repository-datasource rejection, and — the
load-bearing one — that the §4 ``mounts`` payload the orchestrator emits is
exactly what the agent-side factory (``create_lite_backend``) consumes. That
roundtrip is what proves S1 and S2 agree on the contract without a cluster.

Import pattern mirrors test_dispatch_phase_credentials.py: ``orchestrator/`` on
``sys.path`` so ``import main`` resolves; conftest handles the license gate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).parent.parent
_ORCH = _ROOT / "orchestrator"
for _p in (str(_ROOT), str(_ORCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402
from src.core.backends.factory import create_lite_backend  # noqa: E402


# ---------------------------------------------------------------------------
# _backend_from_override
# ---------------------------------------------------------------------------
class TestBackendFromOverride:
    def test_dict(self):
        assert (
            main._backend_from_override({"workspace": {"backend": "virtual"}})
            == "virtual"
        )

    def test_json_string(self):
        assert (
            main._backend_from_override('{"workspace": {"backend": "none"}}') == "none"
        )

    def test_none(self):
        assert main._backend_from_override(None) is None

    def test_no_workspace_key(self):
        assert main._backend_from_override({"llm": {"model": "x"}}) is None

    def test_bad_json_string(self):
        assert main._backend_from_override("not json at all") is None

    def test_non_dict_workspace(self):
        assert main._backend_from_override({"workspace": "oops"}) is None


# ---------------------------------------------------------------------------
# _virtual_workspace_rclone_spec
# ---------------------------------------------------------------------------
class TestVirtualWorkspaceRcloneSpec:
    def test_unset_type_returns_none(self, monkeypatch):
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", raising=False)
        assert main._virtual_workspace_rclone_spec() is None

    def test_set_builds_spec(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", "s3")
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_ROOT", "bucket-x")
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_CONFIG", '{"access_key_id": "K"}')
        assert main._virtual_workspace_rclone_spec() == {
            "type": "s3",
            "config": {"access_key_id": "K"},
            "root": "bucket-x",
        }

    def test_bad_json_config_falls_back_to_empty(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", "s3")
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_CONFIG", "not json")
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_ROOT", raising=False)
        assert main._virtual_workspace_rclone_spec() == {
            "type": "s3",
            "config": {},
            "root": "",
        }

    def test_non_object_json_config_ignored(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", "s3")
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_CONFIG", '["a", "b"]')
        assert main._virtual_workspace_rclone_spec()["config"] == {}

    def test_memory_type_needs_no_config(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", "memory")
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_CONFIG", raising=False)
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_ROOT", raising=False)
        assert main._virtual_workspace_rclone_spec() == {
            "type": "memory",
            "config": {},
            "root": "",
        }


# ---------------------------------------------------------------------------
# _inject_lite_workspace_config
# ---------------------------------------------------------------------------
class TestInjectLiteWorkspaceConfig:
    def test_non_lite_returns_unchanged(self):
        co = {"workspace": {"backend": "sandbox"}}
        assert main._inject_lite_workspace_config(co, prefix="jobs/j/") is co

    def test_none_override_stays_none(self):
        # backend is None -> not lite -> no enrichment, no crash.
        assert main._inject_lite_workspace_config(None, prefix="jobs/j/") is None

    def test_none_backend_git_off_no_mounts(self):
        co = main._inject_lite_workspace_config(
            {"workspace": {"backend": "none"}}, prefix="jobs/j/"
        )
        ws = co["workspace"]
        assert ws["backend"] == "none"
        assert ws["git_versioning"] is False
        assert "mounts" not in ws

    def test_none_backend_strips_stray_mounts(self):
        co = main._inject_lite_workspace_config(
            {"workspace": {"backend": "none", "mounts": [{"x": 1}]}},
            prefix="jobs/j/",
        )
        assert "mounts" not in co["workspace"]

    def test_virtual_builds_mount(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", "memory")
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_CONFIG", raising=False)
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_ROOT", raising=False)
        co = main._inject_lite_workspace_config(
            {"workspace": {"backend": "virtual"}}, prefix="jobs/j7/"
        )
        ws = co["workspace"]
        assert ws["backend"] == "virtual"
        assert ws["git_versioning"] is False
        assert len(ws["mounts"]) == 1
        mount = ws["mounts"][0]
        assert mount["name"] == "workspace"
        assert mount["prefix"] == "jobs/j7/"
        assert mount["access"] == "read_write"
        assert mount["rclone_spec"]["type"] == "memory"

    def test_virtual_without_objectstore_raises(self, monkeypatch):
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", raising=False)
        with pytest.raises(main.LiteWorkspaceConfigError):
            main._inject_lite_workspace_config(
                {"workspace": {"backend": "virtual"}}, prefix="jobs/j/"
            )


# ---------------------------------------------------------------------------
# _repository_datasource_names
# ---------------------------------------------------------------------------
class TestRepositoryDatasourceNames:
    def test_filters_repository(self):
        ds = [
            {"type": "postgresql", "name": "pg"},
            {"type": "repository", "name": "repo1"},
        ]
        assert main._repository_datasource_names(ds) == ["repo1"]

    def test_case_insensitive(self):
        assert main._repository_datasource_names(
            [{"type": "Repository", "name": "r"}]
        ) == ["r"]

    def test_id_fallback_when_no_name(self):
        assert main._repository_datasource_names(
            [{"type": "repository", "id": "abc"}]
        ) == ["abc"]

    def test_empty_and_none(self):
        assert main._repository_datasource_names(None) == []
        assert main._repository_datasource_names([]) == []

    def test_skips_non_dict_entries(self):
        assert main._repository_datasource_names(
            ["junk", {"type": "repository", "name": "r"}]
        ) == ["r"]


# ---------------------------------------------------------------------------
# _job_needs_sandbox / _job_needs_vm: lite tiers never provision
# ---------------------------------------------------------------------------
class TestLiteNeverProvisions:
    def _force_provisioners_available(self, monkeypatch):
        # is_available is a read-only property on the real singletons, so swap
        # the module references for stand-ins that report available. This is
        # the worst case for the lite short-circuit: a provisioner *is* ready.
        monkeypatch.setattr(
            main, "container_provisioner", SimpleNamespace(is_available=True)
        )
        monkeypatch.setattr(
            main, "docker_provisioner", SimpleNamespace(is_available=True)
        )

    def test_virtual_does_not_need_sandbox(self, monkeypatch):
        self._force_provisioners_available(monkeypatch)
        job = {"config_override": {"workspace": {"backend": "virtual"}}}
        assert main._job_needs_sandbox(job) is False

    def test_none_does_not_need_sandbox(self, monkeypatch):
        self._force_provisioners_available(monkeypatch)
        job = {"config_override": {"workspace": {"backend": "none"}}}
        assert main._job_needs_sandbox(job) is False

    def test_unset_backend_still_needs_sandbox(self, monkeypatch):
        # Sanity: the stand-ins really do report available, so the lite=False
        # results above are the short-circuit, not an inert provisioner.
        self._force_provisioners_available(monkeypatch)
        assert main._job_needs_sandbox({"config_override": {}}) is True

    def test_virtual_does_not_need_vm(self):
        job = {"config_override": {"workspace": {"backend": "virtual"}}}
        assert main._job_needs_vm(job) is False

    def test_none_does_not_need_vm(self):
        job = {"config_override": {"workspace": {"backend": "none"}}}
        assert main._job_needs_vm(job) is False


# ---------------------------------------------------------------------------
# §4 payload contract roundtrip — orchestrator output -> agent factory
# ---------------------------------------------------------------------------
class TestPayloadContractRoundtrip:
    """The single most important S2 test: whatever the orchestrator emits in
    ``config_override.workspace`` must be directly consumable by the agent-side
    ``create_lite_backend`` (no translation in between). Uses the dev ``memory``
    object store so the whole path runs in-process."""

    def test_virtual_payload_builds_a_working_backend(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_WORKSPACE_RCLONE_TYPE", "memory")
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_CONFIG", raising=False)
        monkeypatch.delenv("VIRTUAL_WORKSPACE_RCLONE_ROOT", raising=False)

        co = main._inject_lite_workspace_config(
            {"workspace": {"backend": "virtual"}}, prefix="jobs/roundtrip/"
        )
        ws = co["workspace"]

        # Exactly the shape the agent loader hands the factory (backend + mounts).
        cfg = SimpleNamespace(backend=ws["backend"], mounts=ws["mounts"])
        backend = create_lite_backend(cfg, job_id="roundtrip")
        backend.connect()

        # The orchestrator's prefix flowed through to the backend's virtual root.
        assert backend.root == "jobs/roundtrip/"

        # Full write -> read -> exists -> list roundtrip over the emitted payload.
        backend.write_file("notes/plan.md", "hello virtual")
        assert backend.read_file("notes/plan.md") == "hello virtual"
        assert backend.exists("notes/plan.md") is True
        # Directories list with a trailing slash in the virtual backend.
        assert "notes/" in backend.list_dir("")

    def test_none_payload_builds_scratch_backend(self):
        co = main._inject_lite_workspace_config(
            {"workspace": {"backend": "none"}}, prefix="jobs/n/"
        )
        cfg = SimpleNamespace(backend=co["workspace"]["backend"], mounts=None)
        backend = create_lite_backend(cfg, job_id="n1")
        try:
            backend.connect()
            # ScratchBackend is a real disposable FS backend (no file tools are
            # registered over it in `none` mode, but the object itself works).
            backend.write_file("scratch.txt", "x")
            assert backend.read_file("scratch.txt") == "x"
        finally:
            backend.disconnect()
