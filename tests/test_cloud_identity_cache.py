"""Cached cloud-identity resolution + project-GET background repair.

Covers the two halves of knowledge-base/knowledge/issues/project_page_open_blocks_on_cloud_heal.md:

* ``services.cloud.identity`` — cache-first semantics: positive resolutions
  persist (once per user per backend), negatives never do, the ``peek``
  helper is pure DB, and the home-URL cache short-circuits the backend.
* ``main._fire_background_repair`` / ``main.get_project`` — the lazy-heal and
  home-URL resolution run as throttled fire-and-forget tasks, never on the
  request path.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.cloud.identity import (
    get_home_browser_url_cached,
    peek_home_browser_url,
    resolve_user_identity_cached,
)

BACKEND_ID = "opencloud"


class _FakeIdentityDB:
    """users.cloud_identity as a dict; records merge calls."""

    def __init__(self, store: dict | None = None):
        self.store = store or {}
        self.merge_calls: list[tuple[str, str, dict]] = []

    async def get_user_cloud_identity(self, user_id):
        return self.store.get(str(user_id), {})

    async def merge_user_cloud_identity(self, user_id, backend_id, entry):
        self.merge_calls.append((str(user_id), backend_id, dict(entry)))
        self.store.setdefault(str(user_id), {}).setdefault(backend_id, {}).update(entry)
        return True


def _fake_backend(resolved=None, home_url=None, initialized=True):
    backend = MagicMock()
    backend.backend_id = BACKEND_ID
    backend.is_initialized = initialized
    backend.resolve_user_identity = AsyncMock(return_value=resolved)
    backend.get_user_home = AsyncMock(
        return_value=SimpleNamespace(browser_url=home_url) if home_url else None
    )
    return backend


def _user(uid="11111111-1111-1111-1111-111111111111"):
    return {"id": uid, "email": "a@example.test", "display_name": "User A"}


class TestResolveUserIdentityCached:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_backend(self):
        user = _user()
        db = _FakeIdentityDB({user["id"]: {BACKEND_ID: {"user_id": "cloud-a"}}})
        backend = _fake_backend()

        resolved = await resolve_user_identity_cached(db, user, backend)

        assert resolved == "cloud-a"
        backend.resolve_user_identity.assert_not_awaited()
        assert db.merge_calls == []

    @pytest.mark.asyncio
    async def test_cache_miss_resolves_and_persists(self):
        user = _user()
        db = _FakeIdentityDB()
        backend = _fake_backend(resolved="cloud-a")

        resolved = await resolve_user_identity_cached(db, user, backend)

        assert resolved == "cloud-a"
        backend.resolve_user_identity.assert_awaited_once_with(user["email"], "user a")
        assert db.store[user["id"]][BACKEND_ID]["user_id"] == "cloud-a"
        assert db.store[user["id"]][BACKEND_ID]["resolved_at"]

    @pytest.mark.asyncio
    async def test_negative_result_not_persisted(self):
        """'User hasn't logged into the cloud yet' must stay retryable."""
        user = _user()
        db = _FakeIdentityDB()
        backend = _fake_backend(resolved=None)

        resolved = await resolve_user_identity_cached(db, user, backend)

        assert resolved is None
        assert db.merge_calls == []

    @pytest.mark.asyncio
    async def test_uninitialized_backend_short_circuits(self):
        user = _user()
        db = _FakeIdentityDB({user["id"]: {BACKEND_ID: {"user_id": "cloud-a"}}})
        backend = _fake_backend(initialized=False)

        assert await resolve_user_identity_cached(db, user, backend) is None
        backend.resolve_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_user_id_returns_none(self):
        db = _FakeIdentityDB()
        backend = _fake_backend(resolved="cloud-a")

        assert await resolve_user_identity_cached(db, {}, backend) is None
        backend.resolve_user_identity.assert_not_awaited()


class TestHomeBrowserUrlCached:
    @pytest.mark.asyncio
    async def test_peek_is_pure_db(self):
        user = _user()
        db = _FakeIdentityDB(
            {user["id"]: {BACKEND_ID: {"home_browser_url": "https://cloud/home"}}}
        )
        assert (
            await peek_home_browser_url(db, user["id"], BACKEND_ID)
            == "https://cloud/home"
        )
        assert await peek_home_browser_url(db, "unknown", BACKEND_ID) is None

    @pytest.mark.asyncio
    async def test_cached_url_skips_backend(self):
        user = _user()
        db = _FakeIdentityDB(
            {user["id"]: {BACKEND_ID: {"home_browser_url": "https://cloud/home"}}}
        )
        backend = _fake_backend()

        url = await get_home_browser_url_cached(db, user, backend)

        assert url == "https://cloud/home"
        backend.resolve_user_identity.assert_not_awaited()
        backend.get_user_home.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_miss_resolves_and_persists_both(self):
        user = _user()
        db = _FakeIdentityDB()
        backend = _fake_backend(resolved="cloud-a", home_url="https://cloud/home")

        url = await get_home_browser_url_cached(db, user, backend)

        assert url == "https://cloud/home"
        entry = db.store[user["id"]][BACKEND_ID]
        assert entry["user_id"] == "cloud-a"
        assert entry["home_browser_url"] == "https://cloud/home"

    @pytest.mark.asyncio
    async def test_no_home_returns_none_without_url_persist(self):
        user = _user()
        db = _FakeIdentityDB()
        backend = _fake_backend(resolved="cloud-a", home_url=None)

        assert await get_home_browser_url_cached(db, user, backend) is None
        # Identity itself still cached; the URL key must not be.
        entry = db.store[user["id"]][BACKEND_ID]
        assert entry["user_id"] == "cloud-a"
        assert "home_browser_url" not in entry


# =============================================================================
# main.py — fire-and-forget repair plumbing and the de-blocked get_project
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_repair_state():
    import orchestrator.main

    orchestrator.main._bg_repair_last.clear()
    yield
    orchestrator.main._bg_repair_last.clear()


async def _drain_repair_tasks():
    import orchestrator.main

    if orchestrator.main._bg_repair_tasks:
        await asyncio.gather(
            *list(orchestrator.main._bg_repair_tasks), return_exceptions=True
        )


class TestFireBackgroundRepair:
    @pytest.mark.asyncio
    async def test_first_fire_schedules_and_runs(self):
        from orchestrator.main import _fire_background_repair

        ran = asyncio.Event()

        async def work():
            ran.set()

        assert _fire_background_repair("k1", work()) is True
        await _drain_repair_tasks()
        assert ran.is_set()

    @pytest.mark.asyncio
    async def test_second_fire_within_cooldown_throttled(self):
        from orchestrator.main import _fire_background_repair

        runs = []

        async def work(tag):
            runs.append(tag)

        assert _fire_background_repair("k1", work("first")) is True
        assert _fire_background_repair("k1", work("second")) is False
        await _drain_repair_tasks()
        assert runs == ["first"]

    @pytest.mark.asyncio
    async def test_distinct_keys_fire_independently(self):
        from orchestrator.main import _fire_background_repair

        runs = []

        async def work(tag):
            runs.append(tag)

        assert _fire_background_repair("k1", work("a")) is True
        assert _fire_background_repair("k2", work("b")) is True
        await _drain_repair_tasks()
        assert sorted(runs) == ["a", "b"]


def _patch_caller_and_db(user: dict, db):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("orchestrator.main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch(
            "orchestrator.security.access.require_approved_user",
            AsyncMock(return_value=user),
        )
    )
    stack.enter_context(patch("orchestrator.main.postgres_db", db))
    return stack


class TestGetProjectOffCriticalPath:
    @pytest.mark.asyncio
    async def test_get_project_returns_while_heal_still_running(
        self, user_a, project_a, fake_db, fake_request
    ):
        """The request must not block on the heal — the old inline await cost
        2.3-5s per page open."""
        from orchestrator.main import get_project

        heal_started = asyncio.Event()
        heal_release = asyncio.Event()

        async def slow_heal(project):
            heal_started.set()
            await heal_release.wait()
            return project

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("orchestrator.main._ensure_project_cloud_resources", slow_heal),
            patch("orchestrator.main.main_cloud_router") as router,
        ):
            router.for_project_optional.return_value.is_initialized = False
            result = await get_project(fake_request, str(project_a["id"]))
            # Returned while the heal is parked on the event.
            assert result["id"] == project_a["id"]
            assert not heal_release.is_set()
            heal_release.set()
            await _drain_repair_tasks()
        assert heal_started.is_set()

    @pytest.mark.asyncio
    async def test_heal_throttled_across_repeat_opens(
        self, user_a, project_a, fake_db, fake_request
    ):
        from orchestrator.main import get_project

        heal = AsyncMock(side_effect=lambda p: p)
        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("orchestrator.main._ensure_project_cloud_resources", heal),
            patch("orchestrator.main.main_cloud_router") as router,
        ):
            router.for_project_optional.return_value.is_initialized = False
            await get_project(fake_request, str(project_a["id"]))
            await get_project(fake_request, str(project_a["id"]))
            await _drain_repair_tasks()
        assert heal.await_count == 1

    @pytest.mark.asyncio
    async def test_default_project_warm_path_is_db_only(
        self, user_a, project_a, fake_db, fake_request
    ):
        """Cached home URL → no backend identity calls on the request."""
        from orchestrator.main import get_project

        project_a["is_default"] = True
        owner = {
            "user_id": user_a["id"],
            "role": "owner",
            "email": user_a["email"],
            "display_name": user_a["display_name"],
        }
        fake_db.get_project_members = AsyncMock(return_value=[owner])
        fake_db.get_user_cloud_identity = AsyncMock(
            return_value={BACKEND_ID: {"home_browser_url": "https://cloud/home"}}
        )
        backend = _fake_backend()

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("orchestrator.main.main_cloud_router") as router,
        ):
            router.for_project_optional.return_value = backend
            result = await get_project(fake_request, str(project_a["id"]))
            await _drain_repair_tasks()

        assert result["cloud_storage_url"] == "https://cloud/home"
        backend.resolve_user_identity.assert_not_awaited()
        backend.get_user_home.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_project_cache_miss_falls_back_and_repairs(
        self, user_a, project_a, fake_db, fake_request
    ):
        """Cold cache: serve the generic home URL now, resolve in background."""
        from orchestrator.main import get_project

        project_a["is_default"] = True
        owner = {
            "user_id": user_a["id"],
            "role": "owner",
            "email": user_a["email"],
            "display_name": user_a["display_name"],
        }
        fake_db.get_project_members = AsyncMock(return_value=[owner])
        fake_db.get_user_cloud_identity = AsyncMock(return_value={})
        backend = _fake_backend(resolved="cloud-a", home_url="https://cloud/home")
        backend.get_default_home_browser_url = MagicMock(
            return_value="https://cloud/default"
        )

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("orchestrator.main.main_cloud_router") as router,
        ):
            router.for_project_optional.return_value = backend
            result = await get_project(fake_request, str(project_a["id"]))
            # This open serves the fallback; the resolve runs off-path.
            assert result["cloud_storage_url"] == "https://cloud/default"
            await _drain_repair_tasks()

        # Background repair did the real resolution and persisted it.
        backend.resolve_user_identity.assert_awaited_once()
        fake_db.merge_user_cloud_identity.assert_awaited()
