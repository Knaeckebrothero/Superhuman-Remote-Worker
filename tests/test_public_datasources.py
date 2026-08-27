"""Public datasources — grant-gated is_global publishing.

Spec: knowledge-base/knowledge/features/public_datasources.md. Covers the PostgresDB helper here;
endpoint gates are covered in the classes added by later tasks.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from database.postgres import PostgresDB

# Every test in this module is async (endpoint + helper coverage).
pytestmark = pytest.mark.asyncio


def _patch_caller_and_db(user: dict, db):
    """Patch the caller (require_approved_user) and DB on the main module.

    Mirrors tests/test_datasource_access.py — kept local so this file stays
    self-contained.
    """
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch(
            "security.access.require_approved_user",
            AsyncMock(return_value=user),
        )
    )
    stack.enter_context(patch("main.postgres_db", db))
    return stack


EMPTY_SCOPES = {"user": [], "project": [], "global": []}


def _db_with_grant_rows(scoped):
    """PostgresDB with no pool — only list_grants_for_scopes is exercised."""
    db = PostgresDB.__new__(PostgresDB)
    db.list_grants_for_scopes = AsyncMock(return_value=scoped)
    return db


class TestUserCanPublishDatasource:
    async def test_admin_short_circuits_without_grant_read(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert (
            await db.user_can_publish_datasource({"id": "u1", "is_admin": True}) is True
        )
        db.list_grants_for_scopes.assert_not_awaited()

    async def test_no_rows_denies_by_default(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert (
            await db.user_can_publish_datasource({"id": "u1", "is_admin": False})
            is False
        )

    async def test_user_scope_grant_allows(self):
        db = _db_with_grant_rows(
            {
                "user": [{"key": "public_datasources", "value_json": True}],
                "project": [],
                "global": [],
            }
        )
        assert (
            await db.user_can_publish_datasource({"id": "u1", "is_admin": False})
            is True
        )

    async def test_grant_read_failure_fails_closed(self):
        db = PostgresDB.__new__(PostgresDB)
        db.list_grants_for_scopes = AsyncMock(side_effect=RuntimeError("db down"))
        assert (
            await db.user_can_publish_datasource({"id": "u1", "is_admin": False})
            is False
        )


def _created_row(**overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Org Wiki",
        "description": None,
        "type": "repository",
        "connection_url": "https://github.com/org/wiki",
        "credentials": {"auth_method": "token", "token": "secret"},
        "config": {},
        "job_id": None,
        "cli_hint": None,
        "default_branch": None,
        "created_by": "user-a",
        "is_global": True,
        "read_only": True,
        "created_at": "2026-07-11T00:00:00Z",
        "updated_at": "2026-07-11T00:00:00Z",
    }
    row.update(overrides)
    return row


class TestCreatePublishGate:
    async def test_publish_without_grant_403(self, user_a, fake_db, fake_request):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        fake_db.create_datasource = AsyncMock()
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await create_datasource(
                    DatasourceCreate(
                        name="Org Wiki",
                        type="repository",
                        connection_url="https://github.com/org/wiki",
                        is_global=True,
                    ),
                    fake_request,
                )
        assert exc.value.status_code == 403
        assert "public_datasources" in exc.value.detail
        fake_db.create_datasource.assert_not_awaited()

    async def test_publish_with_grant_defaults_read_only_true(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        fake_db.create_datasource = AsyncMock(return_value=_created_row())
        with _patch_caller_and_db(user_a, fake_db):
            result = await create_datasource(
                DatasourceCreate(
                    name="Org Wiki",
                    type="repository",
                    connection_url="https://github.com/org/wiki",
                    is_global=True,
                ),
                fake_request,
            )
        kwargs = fake_db.create_datasource.await_args.kwargs
        assert kwargs["is_global"] is True
        assert kwargs["read_only"] is True  # defaulted on publish
        assert "secret" not in str(result.get("credentials"))  # still redacted

    async def test_publish_read_write_with_grant_keeps_false(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        fake_db.create_datasource = AsyncMock(
            return_value=_created_row(read_only=False)
        )
        with _patch_caller_and_db(user_a, fake_db):
            await create_datasource(
                DatasourceCreate(
                    name="Org Wiki",
                    type="repository",
                    connection_url="https://github.com/org/wiki",
                    is_global=True,
                    read_only=False,
                ),
                fake_request,
            )
        assert fake_db.create_datasource.await_args.kwargs["read_only"] is False

    async def test_private_create_never_calls_gate(self, user_a, fake_db, fake_request):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        fake_db.create_datasource = AsyncMock(
            return_value=_created_row(is_global=False, read_only=None)
        )
        with _patch_caller_and_db(user_a, fake_db):
            await create_datasource(
                DatasourceCreate(
                    name="Mine",
                    type="repository",
                    connection_url="https://github.com/me/mine",
                ),
                fake_request,
            )
        fake_db.user_can_publish_datasource.assert_not_awaited()
        assert fake_db.create_datasource.await_args.kwargs["read_only"] is None

    async def test_kb_read_write_flag_400(self, user_a, fake_db, fake_request):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        fake_db.create_datasource = AsyncMock()
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await create_datasource(
                    DatasourceCreate(
                        name="Org KB",
                        type="kb",
                        connection_url="https://github.com/org/kb",
                        is_global=True,
                        read_only=False,
                    ),
                    fake_request,
                )
        assert exc.value.status_code == 400
        assert "read-only" in exc.value.detail
        fake_db.create_datasource.assert_not_awaited()


def _existing_private():
    return _created_row(is_global=False, read_only=None)


def _existing_public(read_only=True):
    return _created_row(is_global=True, read_only=read_only)


def _wire_owner_update(fake_db, user, existing):
    """require_datasource_owner resolves via get_datasource + creator check."""
    existing = {**existing, "created_by": str(user["id"])}
    fake_db.get_datasource = AsyncMock(return_value=existing)
    fake_db.update_datasource = AsyncMock(return_value=True)
    fake_db.list_datasource_projects = AsyncMock(return_value=[])
    return existing


class TestUpdatePublishGate:
    async def test_publish_flip_without_grant_403(self, user_a, fake_db, fake_request):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_private())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await update_datasource(
                    fake_request, existing["id"], DatasourceUpdate(is_global=True)
                )
        assert exc.value.status_code == 403
        fake_db.update_datasource.assert_not_awaited()

    async def test_publish_flip_with_grant_defaults_read_only(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_private())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        with _patch_caller_and_db(user_a, fake_db):
            result = await update_datasource(
                fake_request, existing["id"], DatasourceUpdate(is_global=True)
            )
        assert result["id"] == existing["id"]
        kwargs = fake_db.update_datasource.await_args.kwargs
        assert kwargs["is_global"] is True
        assert kwargs["read_only"] is True

    async def test_unpublish_needs_no_grant(self, user_a, fake_db, fake_request):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_public())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        with _patch_caller_and_db(user_a, fake_db):
            result = await update_datasource(
                fake_request, existing["id"], DatasourceUpdate(is_global=False)
            )
        assert result["id"] == existing["id"]
        fake_db.user_can_publish_datasource.assert_not_awaited()
        assert fake_db.update_datasource.await_args.kwargs["is_global"] is False

    async def test_ro_to_rw_flip_needs_no_grant(self, user_a, fake_db, fake_request):
        # Spec: friction for RO→RW is the client-side typed confirmation;
        # the server gate is only on the publish transition.
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_public())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        with _patch_caller_and_db(user_a, fake_db):
            result = await update_datasource(
                fake_request, existing["id"], DatasourceUpdate(read_only=False)
            )
        assert result["id"] == existing["id"]
        fake_db.user_can_publish_datasource.assert_not_awaited()
        assert fake_db.update_datasource.await_args.kwargs["read_only"] is False

    async def test_kb_read_write_flag_400(self, user_a, fake_db, fake_request):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(
            fake_db, user_a, _created_row(type="kb", is_global=True)
        )
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await update_datasource(
                    fake_request, existing["id"], DatasourceUpdate(read_only=False)
                )
        assert exc.value.status_code == 400
        fake_db.update_datasource.assert_not_awaited()
