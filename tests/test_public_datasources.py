"""Public datasources — grant-gated is_global publishing.

Spec: docs/features/public_datasources.md. Covers the PostgresDB helper here;
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
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": True}
        ) is True
        db.list_grants_for_scopes.assert_not_awaited()

    async def test_no_rows_denies_by_default(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": False}
        ) is False

    async def test_user_scope_grant_allows(self):
        db = _db_with_grant_rows(
            {
                "user": [{"key": "public_datasources", "value_json": True}],
                "project": [],
                "global": [],
            }
        )
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": False}
        ) is True

    async def test_grant_read_failure_fails_closed(self):
        db = PostgresDB.__new__(PostgresDB)
        db.list_grants_for_scopes = AsyncMock(side_effect=RuntimeError("db down"))
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": False}
        ) is False
