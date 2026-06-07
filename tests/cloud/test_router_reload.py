"""Tests for ``MainCloudRouter.replace_active`` and ``reload_from_db``.

Phase 4 added a hot-reload path: the cockpit admin UI writes a new
config to ``system_settings.main_cloud`` and the PUT handler calls
``main_cloud_router.reload_from_db(overlay)`` to swap the active
backend atomically. These tests exercise:

* ``replace_active`` closing the old backend when the id is unchanged,
  versus demoting it into ``_legacy`` when the id changes.
* ``reload_from_db`` happy path (swap) and unhappy paths (build failure,
  init failure).
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.services.cloud import (
    CloudBackendError,
    CloudBackendErrorKind,
    MainCloudRouter,
)
from tests.cloud.fake import FakeMainCloudBackend


class _AltFakeBackend(FakeMainCloudBackend):
    """A second fake with a different ``backend_id`` for swap tests."""

    backend_id = "fake-alt"


class TestReplaceActive:
    @pytest.mark.asyncio
    async def test_same_id_closes_old_backend(self):
        old = FakeMainCloudBackend(start_initialized=True)
        new = FakeMainCloudBackend(start_initialized=True)
        router = MainCloudRouter(old)

        await router.replace_active(new)

        assert router.active is new
        assert old.is_initialized is False  # close() called
        assert router._legacy == {}

    @pytest.mark.asyncio
    async def test_different_id_demotes_old_to_legacy(self):
        old = FakeMainCloudBackend(start_initialized=True)
        new = _AltFakeBackend(start_initialized=True)
        router = MainCloudRouter(old)

        await router.replace_active(new)

        assert router.active is new
        # Old backend is kept alive for projects that were created on it.
        assert router._legacy == {"fake": old}
        assert old.is_initialized is True  # NOT closed


class TestReloadFromDb:
    @pytest.mark.asyncio
    async def test_happy_path_swaps_active(self, monkeypatch: pytest.MonkeyPatch):
        router = MainCloudRouter(FakeMainCloudBackend(start_initialized=True))

        # Monkey-patch build_backend to return a pre-baked fake so we
        # don't hit load_main_cloud_config at all.
        new_backend = FakeMainCloudBackend(start_initialized=False)
        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(
            cloud_pkg,
            "build_backend",
            lambda *args, **kwargs: new_backend,
        )

        old_active = router.active
        ok = await router.reload_from_db(
            {"value": {"backend_id": "fake"}, "credentials_ref": None}
        )
        assert ok is True
        assert router.active is new_backend
        assert router.active.is_initialized is True
        # Old was closed (same id, so not demoted to legacy).
        assert old_active.is_initialized is False

    @pytest.mark.asyncio
    async def test_init_failure_keeps_old_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        router = MainCloudRouter(FakeMainCloudBackend(start_initialized=True))
        old_active = router.active

        class _FailingInit(FakeMainCloudBackend):
            async def ensure_initialized(self) -> bool:
                raise CloudBackendError(
                    CloudBackendErrorKind.UNAVAILABLE,
                    "boom",
                    backend=self.backend_id,
                )

        failing = _FailingInit(start_initialized=False)
        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(cloud_pkg, "build_backend", lambda *args, **kwargs: failing)

        ok = await router.reload_from_db({"value": {}, "credentials_ref": None})
        assert ok is False
        assert router.active is old_active  # unchanged
        assert old_active.is_initialized is True  # not closed

    @pytest.mark.asyncio
    async def test_init_returns_false_keeps_old_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        router = MainCloudRouter(FakeMainCloudBackend(start_initialized=True))
        old_active = router.active

        class _SoftFail(FakeMainCloudBackend):
            async def ensure_initialized(self) -> bool:
                return False

        soft = _SoftFail(start_initialized=False)
        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(cloud_pkg, "build_backend", lambda *args, **kwargs: soft)

        ok = await router.reload_from_db({"value": {}, "credentials_ref": None})
        assert ok is False
        assert router.active is old_active

    @pytest.mark.asyncio
    async def test_build_failure_keeps_old_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        router = MainCloudRouter(FakeMainCloudBackend(start_initialized=True))
        old_active = router.active

        def _raise(*args, **kwargs):
            raise ValueError("bad config")

        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(cloud_pkg, "build_backend", _raise)

        ok = await router.reload_from_db({"value": {}, "credentials_ref": None})
        assert ok is False
        assert router.active is old_active


class TestReloadDispatchesLegacy:
    """After a successful swap to a new backend id, old projects must
    still route to the demoted backend."""

    @pytest.mark.asyncio
    async def test_for_project_dispatches_to_demoted(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        old = FakeMainCloudBackend(start_initialized=True)
        new = _AltFakeBackend(start_initialized=False)

        router = MainCloudRouter(old)

        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(cloud_pkg, "build_backend", lambda *args, **kwargs: new)

        ok = await router.reload_from_db(
            {"value": {"backend_id": "fake-alt"}, "credentials_ref": None}
        )
        assert ok is True
        assert router.active is new

        legacy_row = {"main_cloud_backend": "fake"}
        new_row = {"main_cloud_backend": "fake-alt"}
        unknown_row: dict[str, Any] = {}

        assert router.for_project(legacy_row) is old
        assert router.for_project(new_row) is new
        assert router.for_project(unknown_row) is new  # None → active

    @pytest.mark.asyncio
    async def test_for_thread_dispatches_to_demoted(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Issue 16 resume hazard: a thread created on the old backend must
        still resolve to it after the active backend is swapped, so resume
        re-provisioning (``main.py`` ``_late_cloud_setup``) does not land on
        the wrong cloud."""
        old = FakeMainCloudBackend(start_initialized=True)
        new = _AltFakeBackend(start_initialized=False)
        router = MainCloudRouter(old)

        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(cloud_pkg, "build_backend", lambda *args, **kwargs: new)
        ok = await router.reload_from_db(
            {"value": {"backend_id": "fake-alt"}, "credentials_ref": None}
        )
        assert ok is True

        pinned_to_old = {"main_cloud_backend": "fake"}
        pinned_to_new = {"main_cloud_backend": "fake-alt"}
        unpinned: dict[str, Any] = {}

        assert router.for_thread(pinned_to_old) is old
        assert router.for_thread(pinned_to_new) is new
        assert router.for_thread(unpinned) is new  # None → active


class TestForOwner:
    """``for_owner`` is the resolution seam for *fresh* creates that aren't
    tied to a project/thread row yet (Issue 16). It returns the active
    backend today; the ``owner`` argument is reserved for per-org resolution
    under multi-tenancy."""

    def test_returns_active_ignoring_owner(self):
        active = FakeMainCloudBackend(start_initialized=True)
        router = MainCloudRouter(active)
        assert router.for_owner({"id": "u1", "email": "a@b.c"}) is active
        assert router.for_owner(None) is active
        assert router.for_owner() is active

    @pytest.mark.asyncio
    async def test_fresh_create_follows_active_after_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A fresh create always lands on the *new* active backend after a
        swap — never on a demoted legacy backend."""
        old = FakeMainCloudBackend(start_initialized=True)
        new = _AltFakeBackend(start_initialized=False)
        router = MainCloudRouter(old)

        import orchestrator.services.cloud as cloud_pkg

        monkeypatch.setattr(cloud_pkg, "build_backend", lambda *args, **kwargs: new)
        await router.reload_from_db(
            {"value": {"backend_id": "fake-alt"}, "credentials_ref": None}
        )
        assert router.for_owner({"id": "u1"}) is new
