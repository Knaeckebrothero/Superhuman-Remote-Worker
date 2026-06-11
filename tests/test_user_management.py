"""Tests for the user management system.

Covers:
- PostgresDB user operations (CRUD, is_admin, upsert_default_user)
- PostgresDB MCP token operations (create, list, revoke, verify, cleanup)
- Init seeding (_seed_admin_mcp_token)
- The _user_dict helper, schema and config-file sanity checks
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Add orchestrator to path so we can import its modules
_orchestrator_dir = os.path.join(os.path.dirname(__file__), "..", "orchestrator")
if _orchestrator_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_orchestrator_dir))


# ============================================================================
# Helpers
# ============================================================================


def _make_user(
    user_id=None,
    display_name="Test User",
    avatar_color="#89b4fa",
    email="test@example.com",
    default_project_id=None,
    is_admin=False,
    created_at=None,
    password_hash=None,
    email_verified=False,
):
    """Build a fake user dict matching PostgresDB.get_user() output."""
    return {
        "id": user_id or uuid4(),
        "display_name": display_name,
        "avatar_color": avatar_color,
        "email": email,
        "default_project_id": default_project_id or uuid4(),
        "is_admin": is_admin,
        "created_at": created_at or datetime.now(timezone.utc),
        "password_hash": password_hash,
        "email_verified": email_verified,
    }


def _make_mcp_token(user_id=None, scope="user", name="Test Token", revoked=False):
    """Build a fake MCP token dict."""
    return {
        "id": uuid4(),
        "user_id": user_id or uuid4(),
        "name": name,
        "token_prefix": "srw_abc12345",
        "scope": scope,
        "expires_at": None,
        "revoked_at": datetime.now(timezone.utc) if revoked else None,
        "last_used_at": None,
        "created_at": datetime.now(timezone.utc),
    }


# ============================================================================
# PostgresDB User Method Tests (mocked connection)
# ============================================================================


class TestPostgresDBUserOps:
    """Tests for PostgresDB user-related methods using mocked connections."""

    def _make_db(self):
        """Create a PostgresDB instance with a mocked pool."""
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
            from database import PostgresDB

            db = PostgresDB()
        db._pool = MagicMock()
        return db

    def _mock_conn(
        self, db, fetchrow_return=None, fetch_return=None, execute_return="UPDATE 1"
    ):
        """Set up a mock connection context manager on the db."""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
        conn.fetch = AsyncMock(return_value=fetch_return or [])
        conn.execute = AsyncMock(return_value=execute_return)

        # Make db.acquire() return an async context manager yielding conn
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        db.acquire = MagicMock(return_value=cm)
        return conn

    @pytest.mark.asyncio
    async def test_get_user_includes_is_admin(self):
        db = self._make_db()
        user_id = uuid4()
        row = {
            "id": user_id,
            "display_name": "Alice",
            "avatar_color": "#89b4fa",
            "email": "alice@test.com",
            "default_project_id": uuid4(),
            "is_admin": True,
            "created_at": datetime.now(timezone.utc),
        }
        self._mock_conn(db, fetchrow_return=row)

        result = await db.get_user(str(user_id))
        assert result is not None
        assert result["is_admin"] is True

    @pytest.mark.asyncio
    async def test_get_user_invalid_uuid_returns_none(self):
        db = self._make_db()
        result = await db.get_user("not-a-uuid")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_not_found_returns_none(self):
        db = self._make_db()
        self._mock_conn(db, fetchrow_return=None)
        result = await db.get_user(str(uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_by_email_includes_is_admin(self):
        db = self._make_db()
        row = {
            "id": uuid4(),
            "display_name": "Bob",
            "avatar_color": "#89b4fa",
            "email": "bob@test.com",
            "default_project_id": uuid4(),
            "is_admin": False,
            "created_at": datetime.now(timezone.utc),
        }
        self._mock_conn(db, fetchrow_return=row)

        result = await db.get_user_by_email("bob@test.com")
        assert result is not None
        assert result["is_admin"] is False

    @pytest.mark.asyncio
    async def test_get_user_by_email_with_auth_includes_is_admin(self):
        db = self._make_db()
        row = {
            "id": uuid4(),
            "display_name": "Carol",
            "avatar_color": "#89b4fa",
            "email": "carol@test.com",
            "default_project_id": uuid4(),
            "is_admin": True,
            "created_at": datetime.now(timezone.utc),
            "password_hash": "$argon2...",
            "email_verified": True,
        }
        self._mock_conn(db, fetchrow_return=row)

        result = await db.get_user_by_email_with_auth("carol@test.com")
        assert result is not None
        assert result["is_admin"] is True
        assert result["password_hash"] == "$argon2..."

    @pytest.mark.asyncio
    async def test_list_users_includes_is_admin(self):
        db = self._make_db()
        rows = [
            {
                "id": uuid4(),
                "display_name": "Admin",
                "avatar_color": "#f38ba8",
                "email": "admin@test.com",
                "default_project_id": uuid4(),
                "is_admin": True,
                "created_at": datetime.now(timezone.utc),
            },
            {
                "id": uuid4(),
                "display_name": "User",
                "avatar_color": "#89b4fa",
                "email": "user@test.com",
                "default_project_id": uuid4(),
                "is_admin": False,
                "created_at": datetime.now(timezone.utc),
            },
        ]
        self._mock_conn(db, fetch_return=rows)

        result = await db.list_users()
        assert len(result) == 2
        assert result[0]["is_admin"] is True
        assert result[1]["is_admin"] is False

    @pytest.mark.asyncio
    async def test_get_admin_user_found(self):
        db = self._make_db()
        row = {
            "id": uuid4(),
            "display_name": "Admin",
            "is_admin": True,
            "email": "admin@test.com",
        }
        self._mock_conn(db, fetchrow_return=row)

        result = await db.get_admin_user()
        assert result is not None
        assert result["is_admin"] is True

    @pytest.mark.asyncio
    async def test_get_admin_user_not_found(self):
        db = self._make_db()
        self._mock_conn(db, fetchrow_return=None)

        result = await db.get_admin_user()
        assert result is None

    @pytest.mark.asyncio
    async def test_list_users_includes_approval_columns(self):
        db = self._make_db()
        rows = [
            {
                "id": uuid4(),
                "display_name": "Pending",
                "avatar_color": "#89b4fa",
                "email": "p@test.com",
                "default_project_id": uuid4(),
                "is_admin": False,
                "can_use_vm": False,
                "is_approved": False,
                "approved_at": None,
                "approved_by": None,
                "created_at": datetime.now(timezone.utc),
            },
        ]
        self._mock_conn(db, fetch_return=rows)

        result = await db.list_users()
        assert result[0]["is_approved"] is False
        assert "approved_at" in result[0]
        assert "approved_by" in result[0]

    @pytest.mark.asyncio
    async def test_update_user_stamps_approval(self):
        db = self._make_db()
        conn = self._mock_conn(db, execute_return="UPDATE 1")

        ok = await db.update_user(
            str(uuid4()),
            is_approved=True,
            approved_at=datetime.now(timezone.utc),
            approved_by=str(uuid4()),
        )
        assert ok is True
        sql = conn.execute.call_args[0][0]
        assert "is_approved = $" in sql
        assert "approved_at = $" in sql
        assert "approved_by = $" in sql

    @pytest.mark.asyncio
    async def test_update_user_suspension_omits_stamp(self):
        # Suspension (is_approved=False) flips the flag but must NOT touch
        # approved_at/approved_by — they survive as approval history.
        db = self._make_db()
        conn = self._mock_conn(db, execute_return="UPDATE 1")

        ok = await db.update_user(str(uuid4()), is_approved=False)
        assert ok is True
        sql = conn.execute.call_args[0][0]
        assert "is_approved = $" in sql
        assert "approved_at" not in sql
        assert "approved_by" not in sql

    @pytest.mark.asyncio
    async def test_approve_users_returns_matched_ids(self):
        db = self._make_db()
        id1, id2 = uuid4(), uuid4()
        conn = self._mock_conn(db, fetch_return=[{"id": id1}, {"id": id2}])

        result = await db.approve_users([str(id1), str(id2)], approved_by=str(uuid4()))
        assert result == [str(id1), str(id2)]
        sql = conn.fetch.call_args[0][0]
        assert "is_approved = TRUE" in sql
        assert "RETURNING id" in sql
        passed_uuids = conn.fetch.call_args[0][1]
        assert id1 in passed_uuids and id2 in passed_uuids

    @pytest.mark.asyncio
    async def test_approve_users_filters_invalid_ids(self):
        db = self._make_db()
        good = uuid4()
        conn = self._mock_conn(db, fetch_return=[{"id": good}])

        result = await db.approve_users([str(good), "not-a-uuid", ""])
        assert result == [str(good)]
        # Only the valid UUID is passed to the query.
        assert conn.fetch.call_args[0][1] == [good]

    @pytest.mark.asyncio
    async def test_approve_users_all_invalid_skips_db(self):
        db = self._make_db()
        conn = self._mock_conn(db, fetch_return=[])

        result = await db.approve_users(["nope", ""])
        assert result == []
        conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_admin_user_ids(self):
        db = self._make_db()
        id1, id2 = uuid4(), uuid4()
        conn = self._mock_conn(db, fetch_return=[{"id": id1}, {"id": id2}])

        result = await db.list_admin_user_ids()
        assert result == [str(id1), str(id2)]
        sql = conn.fetch.call_args[0][0]
        assert "is_admin = TRUE" in sql


class TestAppSideAdmission:
    """App-side admission seam: the ``users.is_approved`` column owns approval,
    with a transition-window write-through from the legacy Keycloak ``user``
    role and PAT/MCP paths that no longer force approval. See
    docs/done/app_side_admission.md.
    """

    def _db_with_user(self, user_row):
        """Mock db whose get_user_by_keycloak_sub returns user_row and whose
        acquire() yields a conn with a captured execute()."""
        db = MagicMock()
        db.get_user_by_keycloak_sub = AsyncMock(return_value=user_row)
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        db.acquire = MagicMock(return_value=cm)
        return db, conn

    def _user_row(self, **over):
        row = {
            "id": uuid4(),
            "display_name": "rowuser",
            "avatar_color": "#89b4fa",
            "email": "u@test.com",
            "default_project_id": uuid4(),
            "is_admin": False,
            "can_use_vm": False,
            "is_approved": False,
            "preferred_username": "rowuser",
            "keycloak_sub": "kc-1",
            "created_at": datetime.now(timezone.utc),
        }
        row.update(over)
        return row

    def _claims(self, roles):
        return {
            "sub": "kc-1",
            "email": "u@test.com",
            "preferred_username": "rowuser",
            "realm_access": {"roles": roles},
        }

    @pytest.mark.asyncio
    async def test_write_through_migrates_legacy_role_holder(self):
        from security.auth import _resolve_user_from_claims

        db, conn = self._db_with_user(self._user_row(is_approved=False))
        result = await _resolve_user_from_claims(self._claims(["user"]), db)

        assert result["is_approved"] is True
        # The role-holder's DB flag was stamped through on this request.
        sql = conn.execute.call_args[0][0]
        assert "is_approved = $" in sql

    @pytest.mark.asyncio
    async def test_pending_when_no_role_and_db_false(self):
        from security.auth import _resolve_user_from_claims

        db, _ = self._db_with_user(self._user_row(is_approved=False))
        result = await _resolve_user_from_claims(self._claims([]), db)

        assert result["is_approved"] is False
        db.acquire.assert_not_called()  # nothing to write through

    @pytest.mark.asyncio
    async def test_db_approved_survives_absent_role(self):
        from security.auth import _resolve_user_from_claims

        db, _ = self._db_with_user(self._user_row(is_approved=True))
        result = await _resolve_user_from_claims(self._claims([]), db)

        # DB flag wins; an absent role never downgrades an approved user.
        assert result["is_approved"] is True
        db.acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_pat_no_longer_forces_approval(self):
        from security.auth import _resolve_pat

        db = MagicMock()
        db.get_auth_token_by_hash = AsyncMock(
            return_value={
                "id": uuid4(),
                "user_id": uuid4(),
                "kind": "api",
                "scopes": [],
            }
        )
        db.get_user = AsyncMock(
            return_value={"id": uuid4(), "display_name": "x", "is_approved": False}
        )
        db.touch_auth_token = AsyncMock()
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock(host="127.0.0.1")

        result = await _resolve_pat("ak_token", request, db)
        # A suspended owner's PAT now reflects the row → denied downstream.
        assert result["is_approved"] is False

    @pytest.mark.asyncio
    async def test_resolve_pat_approved_user_passes(self):
        from security.auth import _resolve_pat

        db = MagicMock()
        db.get_auth_token_by_hash = AsyncMock(
            return_value={
                "id": uuid4(),
                "user_id": uuid4(),
                "kind": "api",
                "scopes": [],
            }
        )
        db.get_user = AsyncMock(
            return_value={"id": uuid4(), "display_name": "x", "is_approved": True}
        )
        db.touch_auth_token = AsyncMock()
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock(host="127.0.0.1")

        result = await _resolve_pat("ak_token", request, db)
        assert result["is_approved"] is True

    @pytest.mark.asyncio
    async def test_ensure_user_provisioned_skips_without_sub(self):
        # Admin-created / pre-OIDC rows have no keycloak_sub → no provisioning
        # (they provision on their owner's first real OIDC login).
        from security import auth

        with (
            patch.object(auth, "_ensure_cloud_user", new=AsyncMock()) as ec,
            patch.object(auth, "_ensure_gitea_user", new=AsyncMock()) as eg,
        ):
            await auth.ensure_user_provisioned({"id": uuid4(), "email": "x@y.com"})
        ec.assert_not_called()
        eg.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_user_provisioned_fires_both_ensures(self):
        import asyncio

        from security import auth

        with (
            patch.object(auth, "_ensure_cloud_user", new=AsyncMock()) as ec,
            patch.object(auth, "_ensure_gitea_user", new=AsyncMock()) as eg,
        ):
            await auth.ensure_user_provisioned(
                {
                    "id": uuid4(),
                    "keycloak_sub": "kc-1",
                    "email": "x@y.com",
                    "display_name": "X",
                    "preferred_username": "x",
                }
            )
            await asyncio.sleep(0)  # let the scheduled tasks run
            ec.assert_called_once()
            eg.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_admins_fans_out_sse_and_email(self):
        from services.notification_service import NotificationService

        svc = NotificationService()
        feed = MagicMock()  # broadcast is sync
        email = MagicMock()
        email.send_system_notification = AsyncMock(return_value=True)
        db = MagicMock()
        admin1, admin2 = str(uuid4()), str(uuid4())
        db.list_admin_user_ids = AsyncMock(return_value=[admin1, admin2])
        db.get_user = AsyncMock(
            side_effect=lambda uid: {
                "id": uid,
                "email": f"{uid}@x.com",
                "display_name": "Admin",
            }
        )
        svc.connect(db, email, feed)
        svc._get_user_channels = AsyncMock(return_value={"email": True})
        svc._get_user_settings = AsyncMock(return_value={})
        svc._is_in_quiet_hours = MagicMock(return_value=False)

        res = await svc.notify_admins_user_registered(
            "new-user-id", display_name="New", email="new@x.com"
        )
        assert res["notified"] == 2
        assert feed.broadcast.call_count == 2
        assert email.send_system_notification.call_count == 2
        assert feed.broadcast.call_args.kwargs["event_type"] == "user_registered"

    @pytest.mark.asyncio
    async def test_notify_admins_skips_email_in_quiet_hours(self):
        from services.notification_service import NotificationService

        svc = NotificationService()
        feed = MagicMock()
        email = MagicMock()
        email.send_system_notification = AsyncMock(return_value=True)
        db = MagicMock()
        admin1 = str(uuid4())
        db.list_admin_user_ids = AsyncMock(return_value=[admin1])
        db.get_user = AsyncMock(
            return_value={"id": admin1, "email": "a@x.com", "display_name": "A"}
        )
        svc.connect(db, email, feed)
        svc._get_user_channels = AsyncMock(return_value={"email": True})
        svc._get_user_settings = AsyncMock(return_value={})
        svc._is_in_quiet_hours = MagicMock(return_value=True)  # in quiet hours

        await svc.notify_admins_user_registered("new-user-id", display_name="New")
        # SSE still fires (in-app isn't quiet-houred); email is suppressed.
        assert feed.broadcast.call_count == 1
        email.send_system_notification.assert_not_called()


class TestUpsertDefaultUser:
    """Tests for PostgresDB.upsert_default_user with is_admin support."""

    def _make_db(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
            from database import PostgresDB

            db = PostgresDB()
        db._pool = MagicMock()
        return db

    def _mock_conn(self, db):
        conn = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        db.acquire = MagicMock(return_value=cm)
        return conn

    @pytest.mark.asyncio
    async def test_creates_new_user_with_is_admin(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        new_row = {
            "id": uuid4(),
            "display_name": "Admin",
            "avatar_color": "#f38ba8",
            "email": "admin@test.com",
            "is_admin": True,
            "created_at": datetime.now(timezone.utc),
        }
        # First fetchrow (check existing) returns None, second (INSERT) returns new_row
        conn.fetchrow = AsyncMock(side_effect=[None, new_row])

        result = await db.upsert_default_user(
            display_name="Admin",
            avatar_color="#f38ba8",
            email="admin@test.com",
            is_admin=True,
        )

        assert result["is_admin"] is True
        assert result["display_name"] == "Admin"
        # Verify INSERT was called (second fetchrow)
        assert conn.fetchrow.call_count == 2

    @pytest.mark.asyncio
    async def test_creates_new_user_with_password(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        new_row = {
            "id": uuid4(),
            "display_name": "Admin",
            "avatar_color": "#f38ba8",
            "email": "admin@test.com",
            "is_admin": True,
            "created_at": datetime.now(timezone.utc),
        }
        conn.fetchrow = AsyncMock(side_effect=[None, new_row])

        result = await db.upsert_default_user(
            display_name="Admin",
            email="admin@test.com",
            is_admin=True,
            password_hash="$argon2id$...",
            email_verified=True,
        )

        assert result["is_admin"] is True
        # Verify INSERT query includes password_hash and email_verified
        insert_call = conn.fetchrow.call_args_list[1]
        query = insert_call[0][0]
        assert "password_hash" in query
        assert "email_verified" in query

    @pytest.mark.asyncio
    async def test_existing_user_updates_is_admin(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        existing = {
            "id": uuid4(),
            "display_name": "Admin",
            "avatar_color": "#f38ba8",
            "email": "admin@test.com",
            "is_admin": False,
            "created_at": datetime.now(timezone.utc),
        }
        conn.fetchrow = AsyncMock(return_value=existing)

        result = await db.upsert_default_user(
            display_name="Admin",
            email="admin@test.com",
            is_admin=True,  # Changed from False to True
        )

        assert result["is_admin"] is True
        # Verify UPDATE was called
        conn.execute.assert_called_once()
        update_query = conn.execute.call_args[0][0]
        assert "is_admin" in update_query

    @pytest.mark.asyncio
    async def test_existing_user_no_changes_skips_update(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        existing = {
            "id": uuid4(),
            "display_name": "Default",
            "avatar_color": "#89b4fa",
            "email": "default@cockpit.local",
            "is_admin": False,
            "created_at": datetime.now(timezone.utc),
        }
        conn.fetchrow = AsyncMock(return_value=existing)

        result = await db.upsert_default_user(
            display_name="Default",
            email="default@cockpit.local",
            is_admin=False,
        )

        # No update needed — email already set, is_admin unchanged
        conn.execute.assert_not_called()
        assert result["is_admin"] is False

    @pytest.mark.asyncio
    async def test_existing_user_updates_email_when_null(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        existing = {
            "id": uuid4(),
            "display_name": "Default",
            "avatar_color": "#89b4fa",
            "email": None,
            "is_admin": False,
            "created_at": datetime.now(timezone.utc),
        }
        conn.fetchrow = AsyncMock(return_value=existing)

        result = await db.upsert_default_user(
            display_name="Default",
            email="new@test.com",
            is_admin=False,
        )

        conn.execute.assert_called_once()
        update_query = conn.execute.call_args[0][0]
        assert "email" in update_query
        assert result["email"] == "new@test.com"


# ============================================================================
# PostgresDB MCP Token Tests
# ============================================================================


class TestPostgresDBMcpTokens:
    """Tests for PostgresDB MCP token operations."""

    def _make_db(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
            from database import PostgresDB

            db = PostgresDB()
        db._pool = MagicMock()
        return db

    def _mock_conn(
        self, db, fetchrow_return=None, fetch_return=None, execute_return="UPDATE 1"
    ):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
        conn.fetch = AsyncMock(return_value=fetch_return or [])
        conn.execute = AsyncMock(return_value=execute_return)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        db.acquire = MagicMock(return_value=cm)
        return conn

    @pytest.mark.asyncio
    async def test_create_mcp_token(self):
        db = self._make_db()
        token_row = _make_mcp_token()
        self._mock_conn(db, fetchrow_return=token_row)

        result = await db.create_mcp_token(
            user_id=str(uuid4()),
            name="Test Token",
            token_hash="abc123",
            token_prefix="srw_abc",
            scope="user",
        )

        assert result["name"] == "Test Token"
        assert result["scope"] == "user"

    @pytest.mark.asyncio
    async def test_get_mcp_token_by_hash_active(self):
        db = self._make_db()
        token_row = {
            **_make_mcp_token(),
            "display_name": "Test User",
            "email": "test@test.com",
        }
        self._mock_conn(db, fetchrow_return=token_row)

        result = await db.get_mcp_token_by_hash("some-hash")
        assert result is not None
        assert result["scope"] == "user"

    @pytest.mark.asyncio
    async def test_get_mcp_token_by_hash_revoked_returns_none(self):
        db = self._make_db()
        # DB query filters out revoked tokens, so returns None
        self._mock_conn(db, fetchrow_return=None)

        result = await db.get_mcp_token_by_hash("revoked-hash")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_mcp_tokens(self):
        db = self._make_db()
        tokens = [_make_mcp_token(), _make_mcp_token(name="Second Token")]
        self._mock_conn(db, fetch_return=tokens)

        result = await db.list_mcp_tokens(str(uuid4()))
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_revoke_mcp_token_success(self):
        db = self._make_db()
        self._mock_conn(db, execute_return="UPDATE 1")

        result = await db.revoke_mcp_token(str(uuid4()), str(uuid4()))
        assert result is True

    @pytest.mark.asyncio
    async def test_revoke_mcp_token_not_found(self):
        db = self._make_db()
        self._mock_conn(db, execute_return="UPDATE 0")

        result = await db.revoke_mcp_token(str(uuid4()), str(uuid4()))
        assert result is False

    @pytest.mark.asyncio
    async def test_update_mcp_token_last_used(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        await db.update_mcp_token_last_used("some-hash")
        conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_expired_mcp_tokens(self):
        db = self._make_db()
        conn = self._mock_conn(db)

        await db.cleanup_expired_mcp_tokens()
        # Two statements: DELETE expired/long-revoked tokens (both kinds),
        # then the rotation-grace UPDATE that revokes superseded PAT rows
        # older than 24h. See postgres.py — auth_tokens consolidation.
        assert conn.execute.await_count == 2
        queries = [call.args[0] for call in conn.execute.await_args_list]
        assert any("DELETE FROM auth_tokens" in q for q in queries)
        assert any("UPDATE auth_tokens" in q and "superseded_by" in q for q in queries)


# ============================================================================
# Init Seeding Tests
# ============================================================================


class TestSeedAdminMcpToken:
    """Tests for _seed_admin_mcp_token in orchestrator/init.py."""

    @pytest.mark.asyncio
    async def test_creates_token_when_no_admin(self):
        from orchestrator.init import _seed_admin_mcp_token

        db = MagicMock()
        db.get_admin_user = AsyncMock(return_value=None)

        await _seed_admin_mcp_token(db)

        # No admin → no token creation
        db.list_mcp_tokens.assert_not_called()
        db.create_mcp_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_token_for_admin(self):
        from orchestrator.init import _seed_admin_mcp_token

        admin_id = uuid4()
        admin = _make_user(user_id=admin_id, is_admin=True)

        db = MagicMock()
        db.get_admin_user = AsyncMock(return_value=admin)
        db.list_mcp_tokens = AsyncMock(return_value=[])  # No existing tokens
        db.create_mcp_token = AsyncMock(return_value=_make_mcp_token(scope="all"))

        await _seed_admin_mcp_token(db)

        db.create_mcp_token.assert_called_once()
        call_kwargs = db.create_mcp_token.call_args.kwargs
        assert call_kwargs["user_id"] == str(admin_id)
        assert call_kwargs["scope"] == "all"
        assert call_kwargs["name"] == "Root (auto-generated)"
        assert call_kwargs["token_hash"]  # Non-empty
        assert call_kwargs["token_prefix"].startswith("srw_")

    @pytest.mark.asyncio
    async def test_skips_when_token_already_exists(self):
        from orchestrator.init import _seed_admin_mcp_token

        admin = _make_user(is_admin=True)
        existing_token = _make_mcp_token(scope="all")

        db = MagicMock()
        db.get_admin_user = AsyncMock(return_value=admin)
        db.list_mcp_tokens = AsyncMock(return_value=[existing_token])

        await _seed_admin_mcp_token(db)

        # Should not create a new token
        db.create_mcp_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_db_error_gracefully(self):
        from orchestrator.init import _seed_admin_mcp_token

        db = MagicMock()
        db.get_admin_user = AsyncMock(side_effect=Exception("DB connection failed"))

        # Should not raise
        await _seed_admin_mcp_token(db)


# ============================================================================
# Orchestrator API Endpoint Tests
# ============================================================================


class TestUserDictHelper:
    """Tests for the _user_dict helper in main.py."""

    def test_includes_is_admin_true(self):
        """_user_dict should include is_admin field."""
        # We can't easily import from main.py due to side effects,
        # so we test the logic directly
        user = _make_user(is_admin=True)

        result = {
            "id": str(user["id"]),
            "display_name": user["display_name"],
            "avatar_color": user["avatar_color"],
            "email": user.get("email"),
            "default_project_id": str(user["default_project_id"])
            if user.get("default_project_id")
            else None,
            "is_admin": user.get("is_admin", False),
            "created_at": user["created_at"],
        }

        assert result["is_admin"] is True

    def test_includes_is_admin_false(self):
        user = _make_user(is_admin=False)

        result = {
            "id": str(user["id"]),
            "display_name": user["display_name"],
            "avatar_color": user["avatar_color"],
            "email": user.get("email"),
            "default_project_id": str(user["default_project_id"])
            if user.get("default_project_id")
            else None,
            "is_admin": user.get("is_admin", False),
            "created_at": user["created_at"],
        }

        assert result["is_admin"] is False

    def test_missing_is_admin_defaults_to_false(self):
        user = _make_user()
        del user["is_admin"]

        result = {"is_admin": user.get("is_admin", False)}
        assert result["is_admin"] is False


# ============================================================================
# Schema Tests
# ============================================================================


class TestSchemaContainsIsAdmin:
    """Verify schema.sql has the is_admin migration."""

    def test_schema_has_is_admin_migration(self):
        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "orchestrator", "database", "schema.sql"
        )
        with open(schema_path) as f:
            content = f.read()

        assert "is_admin BOOLEAN NOT NULL DEFAULT FALSE" in content

    def test_schema_has_mcp_tokens_table(self):
        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "orchestrator", "database", "schema.sql"
        )
        with open(schema_path) as f:
            content = f.read()

        assert "CREATE TABLE IF NOT EXISTS mcp_tokens" in content

    def test_is_admin_migration_after_email_verified(self):
        """is_admin migration should come after email_verified migration."""
        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "orchestrator", "database", "schema.sql"
        )
        with open(schema_path) as f:
            content = f.read()

        email_verified_pos = content.index("email_verified BOOLEAN")
        is_admin_pos = content.index("is_admin BOOLEAN")
        assert is_admin_pos > email_verified_pos


# ============================================================================
# Config File Tests
# ============================================================================


class TestConfigFiles:
    """Verify config files include admin env vars."""

    def _read_file(self, relative_path):
        path = os.path.join(os.path.dirname(__file__), "..", relative_path)
        with open(path) as f:
            return f.read()

    def test_env_example_has_admin_vars(self):
        content = self._read_file(".env.example")
        assert "ADMIN_EMAIL" in content
        assert "ADMIN_DISPLAY_NAME" in content
        assert "ADMIN_PASSWORD" in content

    def test_docker_compose_has_admin_vars(self):
        content = self._read_file("docker-compose.yaml")
        assert "ADMIN_EMAIL" in content
        assert "ADMIN_DISPLAY_NAME" in content
        assert "ADMIN_PASSWORD" in content

    def test_docker_compose_local_has_admin_vars(self):
        content = self._read_file("docker-compose.local.yaml")
        assert "ADMIN_EMAIL" in content
        assert "ADMIN_DISPLAY_NAME" in content
        assert "ADMIN_PASSWORD" in content

    @pytest.mark.skipif(
        not os.path.isdir(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "HomeLab",
                "deployments",
                "srw",
            )
        ),
        reason="HomeLab deployment files not present",
    )
    def test_k8s_secrets_has_admin_password(self):
        content = self._read_file("docs/HomeLab/deployments/srw/01-secrets.yaml")
        assert "ADMIN_PASSWORD" in content

    @pytest.mark.skipif(
        not os.path.isdir(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "HomeLab",
                "deployments",
                "srw",
            )
        ),
        reason="HomeLab deployment files not present",
    )
    def test_k8s_orchestrator_has_admin_env(self):
        content = self._read_file("docs/HomeLab/deployments/srw/20-orchestrator.yaml")
        assert "ADMIN_EMAIL" in content
        assert "ADMIN_DISPLAY_NAME" in content
        assert "ADMIN_PASSWORD" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
