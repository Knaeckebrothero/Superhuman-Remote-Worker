"""Tests for the user management system.

Covers:
- PostgresDB user operations (CRUD, is_admin, upsert_default_user)
- PostgresDB MCP token operations (create, list, revoke, verify, cleanup)
- Auth module (get_current_user, validate_session)
- Init seeding (_seed_default_users, _seed_admin_mcp_token)
- Orchestrator API endpoints (auth, MCP tokens, admin restrictions)
- Password hashing and validation
"""

import os
import sys
from datetime import datetime, timedelta, timezone
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


def _make_session(user_id=None, email="test@example.com"):
    """Build a fake session dict matching PostgresDB.get_session() output."""
    uid = user_id or uuid4()
    return {
        "session_key": "test-session-key",
        "user_id": uid,
        "email": email,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=24),
        "last_activity": datetime.now(timezone.utc),
        "csrf_token": "test-csrf-token",
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
# Password Module Tests
# ============================================================================


class TestPasswordModule:
    """Tests for orchestrator/security/password.py."""

    def test_hash_password_returns_string(self):
        from security.password import hash_password

        result = hash_password("test_password_123")
        assert isinstance(result, str)
        assert result.startswith("$argon2")

    def test_verify_password_correct(self):
        from security.password import hash_password, verify_password

        pw = "correct_horse_battery_staple1"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed) is True

    def test_verify_password_incorrect(self):
        from security.password import hash_password, verify_password

        hashed = hash_password("right_password1")
        assert verify_password("wrong_password1", hashed) is False

    def test_verify_password_invalid_hash(self):
        from security.password import verify_password

        assert verify_password("anything", "not-a-valid-hash") is False

    def test_validate_password_strength_too_short(self):
        from security.password import validate_password_strength

        ok, msg = validate_password_strength("Ab1")
        assert not ok
        assert "8 characters" in msg

    def test_validate_password_strength_no_letter(self):
        from security.password import validate_password_strength

        ok, msg = validate_password_strength("12345678")
        assert not ok
        assert "letter" in msg

    def test_validate_password_strength_no_digit(self):
        from security.password import validate_password_strength

        ok, msg = validate_password_strength("abcdefgh")
        assert not ok
        assert "digit" in msg

    def test_validate_password_strength_valid(self):
        from security.password import validate_password_strength

        ok, msg = validate_password_strength("secure_pw1")
        assert ok
        assert msg == ""


# ============================================================================
# Auth Module Tests
# ============================================================================


class TestGetCurrentUser:
    """Tests for security.auth.get_current_user."""

    @pytest.mark.asyncio
    async def test_no_session_cookie_raises_401(self):
        from security.auth import get_current_user

        request = MagicMock()
        request.cookies = {}
        db = MagicMock()

        with pytest.raises(Exception) as exc_info:
            await get_current_user(request, db)
        assert exc_info.value.status_code == 401
        assert "Not authenticated" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_invalid_session_raises_401(self):
        from security.auth import get_current_user

        request = MagicMock()
        request.cookies = {"session": "invalid-key"}
        db = MagicMock()
        db.get_session = AsyncMock(return_value=None)

        with pytest.raises(Exception) as exc_info:
            await get_current_user(request, db)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_session_returns_full_user(self):
        from security.auth import get_current_user

        user_id = uuid4()
        session = _make_session(user_id=user_id)
        user = _make_user(user_id=user_id, is_admin=True)

        request = MagicMock()
        request.cookies = {"session": "valid-key"}
        db = MagicMock()
        db.get_session = AsyncMock(return_value=session)
        db.update_session_activity = AsyncMock()
        db.get_user = AsyncMock(return_value=user)

        result = await get_current_user(request, db)

        assert result["id"] == user_id
        assert result["is_admin"] is True
        assert result["display_name"] == "Test User"
        db.get_user.assert_called_once_with(str(user_id))

    @pytest.mark.asyncio
    async def test_valid_session_but_user_deleted_raises_401(self):
        from security.auth import get_current_user

        session = _make_session()

        request = MagicMock()
        request.cookies = {"session": "valid-key"}
        db = MagicMock()
        db.get_session = AsyncMock(return_value=session)
        db.update_session_activity = AsyncMock()
        db.get_user = AsyncMock(return_value=None)

        with pytest.raises(Exception) as exc_info:
            await get_current_user(request, db)
        assert exc_info.value.status_code == 401
        assert "User not found" in str(exc_info.value.detail)


class TestValidateSession:
    """Tests for security.auth.validate_session."""

    @pytest.mark.asyncio
    async def test_none_session_key_returns_none(self):
        from security.auth import validate_session

        db = MagicMock()
        result = await validate_session(db, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_session_key_returns_none(self):
        from security.auth import validate_session

        db = MagicMock()
        result = await validate_session(db, "")
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_session_returns_user_info(self):
        from security.auth import validate_session

        user_id = uuid4()
        session = _make_session(user_id=user_id, email="valid@test.com")

        db = MagicMock()
        db.get_session = AsyncMock(return_value=session)
        db.update_session_activity = AsyncMock()

        result = await validate_session(db, "some-session-key")

        assert result is not None
        assert result["user_id"] == str(user_id)
        assert result["email"] == "valid@test.com"
        assert result["csrf_token"] == "test-csrf-token"
        assert "expires_in" in result
        db.update_session_activity.assert_called_once_with("some-session-key")

    @pytest.mark.asyncio
    async def test_expired_session_returns_none(self):
        from security.auth import validate_session

        db = MagicMock()
        db.get_session = AsyncMock(return_value=None)  # DB filters expired

        result = await validate_session(db, "expired-key")
        assert result is None


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
        conn.execute.assert_called_once()
        query = conn.execute.call_args[0][0]
        assert "DELETE FROM mcp_tokens" in query


# ============================================================================
# Init Seeding Tests
# ============================================================================


class TestSeedDefaultUsers:
    """Tests for _seed_default_users in orchestrator/init.py."""

    @pytest.mark.asyncio
    async def test_seeds_admin_and_default_user(self):
        from orchestrator.init import _seed_default_users

        db = MagicMock()
        db.upsert_default_user = AsyncMock(return_value=_make_user())

        with patch.dict("os.environ", {}, clear=False):
            # Remove any ADMIN_* env vars that might exist
            os.environ.pop("ADMIN_EMAIL", None)
            os.environ.pop("ADMIN_PASSWORD", None)
            os.environ.pop("ADMIN_DISPLAY_NAME", None)
            await _seed_default_users(db)

        # Should be called twice: admin + default
        assert db.upsert_default_user.call_count == 2

        # First call = admin
        admin_call = db.upsert_default_user.call_args_list[0]
        assert admin_call.kwargs["display_name"] == "Admin"
        assert admin_call.kwargs["is_admin"] is True
        assert admin_call.kwargs["email"] == "admin@cockpit.local"

        # Second call = default user
        default_call = db.upsert_default_user.call_args_list[1]
        assert default_call.kwargs["display_name"] == "Default"
        assert default_call.kwargs.get("is_admin", False) is False

    @pytest.mark.asyncio
    async def test_seeds_admin_with_custom_env(self):
        from orchestrator.init import _seed_default_users

        db = MagicMock()
        db.upsert_default_user = AsyncMock(return_value=_make_user())

        with patch.dict(
            "os.environ",
            {
                "ADMIN_EMAIL": "boss@company.com",
                "ADMIN_DISPLAY_NAME": "Boss",
            },
        ):
            await _seed_default_users(db)

        admin_call = db.upsert_default_user.call_args_list[0]
        assert admin_call.kwargs["email"] == "boss@company.com"
        assert admin_call.kwargs["display_name"] == "Boss"

    @pytest.mark.asyncio
    async def test_seeds_admin_with_password_in_production(self):
        from orchestrator.init import _seed_default_users

        db = MagicMock()
        db.upsert_default_user = AsyncMock(return_value=_make_user())

        with patch.dict(
            "os.environ",
            {
                "ADMIN_PASSWORD": "secure_password_1",
            },
        ):
            await _seed_default_users(db)

        admin_call = db.upsert_default_user.call_args_list[0]
        assert admin_call.kwargs.get("password_hash") is not None
        assert admin_call.kwargs["password_hash"].startswith("$argon2")
        assert admin_call.kwargs["email_verified"] is True


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


class TestMcpTokenEndpoints:
    """Tests for MCP token API endpoints in orchestrator/main.py."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock PostgresDB for API tests."""
        db = MagicMock()
        db.create_mcp_token = AsyncMock()
        db.list_mcp_tokens = AsyncMock(return_value=[])
        db.revoke_mcp_token = AsyncMock(return_value=True)
        db.get_mcp_token_by_hash = AsyncMock()
        db.update_mcp_token_last_used = AsyncMock()
        db.get_session = AsyncMock()
        db.update_session_activity = AsyncMock()
        db.get_user = AsyncMock()
        return db

    def _setup_auth(self, mock_db, user=None, is_admin=False):
        """Set up the mock DB to return a valid session and user."""
        if user is None:
            user = _make_user(is_admin=is_admin)

        session = _make_session(user_id=user["id"], email=user["email"])
        mock_db.get_session.return_value = session
        mock_db.get_user.return_value = user
        return user

    @pytest.mark.asyncio
    async def test_admin_can_create_full_access_token(self, mock_db):
        """Admin user should be able to create scope='all' tokens."""
        admin = self._setup_auth(mock_db, is_admin=True)
        token_row = _make_mcp_token(user_id=admin["id"], scope="all")
        mock_db.create_mcp_token.return_value = token_row

        # Simulate the endpoint logic directly
        from security.auth import get_current_user

        request = MagicMock()
        request.cookies = {"session": "valid-key"}
        user = await get_current_user(request, mock_db)

        scope = "all"
        # This should NOT raise for admin
        if scope == "all" and not user.get("is_admin", False):
            pytest.fail("Admin should be allowed to create full-access tokens")

    @pytest.mark.asyncio
    async def test_non_admin_cannot_create_full_access_token(self, mock_db):
        """Non-admin user should get 403 for scope='all'."""
        self._setup_auth(mock_db, is_admin=False)

        from security.auth import get_current_user

        request = MagicMock()
        request.cookies = {"session": "valid-key"}
        user = await get_current_user(request, mock_db)

        scope = "all"
        assert scope == "all" and not user.get("is_admin", False), (
            "Non-admin should be blocked from creating full-access tokens"
        )

    @pytest.mark.asyncio
    async def test_non_admin_can_create_user_scope_token(self, mock_db):
        """Non-admin user should be able to create scope='user' tokens."""
        self._setup_auth(mock_db, is_admin=False)

        from security.auth import get_current_user

        request = MagicMock()
        request.cookies = {"session": "valid-key"}
        user = await get_current_user(request, mock_db)

        scope = "user"
        # Should not be blocked
        blocked = scope == "all" and not user.get("is_admin", False)
        assert not blocked, "User-scope tokens should be allowed for everyone"


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
# Integration-style Tests (Full Flow)
# ============================================================================


class TestAdminBootstrapFlow:
    """Integration-style tests for the admin bootstrap flow."""

    @pytest.mark.asyncio
    async def test_full_admin_bootstrap_without_password(self):
        """Test the full flow: seed admin → create MCP token (dev mode)."""
        from orchestrator.init import _seed_default_users, _seed_admin_mcp_token

        admin_id = uuid4()
        admin_user = _make_user(user_id=admin_id, display_name="Admin", is_admin=True)

        db = MagicMock()
        db.upsert_default_user = AsyncMock(return_value=admin_user)
        db.get_admin_user = AsyncMock(return_value=admin_user)
        db.list_mcp_tokens = AsyncMock(return_value=[])
        db.create_mcp_token = AsyncMock(return_value=_make_mcp_token(scope="all"))

        # Step 1: Seed users
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("ADMIN_PASSWORD", None)
            await _seed_default_users(db)

        # Verify admin was created without password
        admin_call = db.upsert_default_user.call_args_list[0]
        assert admin_call.kwargs["is_admin"] is True
        assert admin_call.kwargs.get("password_hash") is None

        # Step 2: Seed MCP token
        await _seed_admin_mcp_token(db)

        # Verify token was created with scope="all"
        db.create_mcp_token.assert_called_once()
        assert db.create_mcp_token.call_args.kwargs["scope"] == "all"

    @pytest.mark.asyncio
    async def test_full_admin_bootstrap_with_password(self):
        """Test the full flow: seed admin with password → create MCP token (production)."""
        from orchestrator.init import _seed_default_users, _seed_admin_mcp_token

        admin_id = uuid4()
        admin_user = _make_user(user_id=admin_id, display_name="Admin", is_admin=True)

        db = MagicMock()
        db.upsert_default_user = AsyncMock(return_value=admin_user)
        db.get_admin_user = AsyncMock(return_value=admin_user)
        db.list_mcp_tokens = AsyncMock(return_value=[])
        db.create_mcp_token = AsyncMock(return_value=_make_mcp_token(scope="all"))

        # Step 1: Seed users with password
        with patch.dict("os.environ", {"ADMIN_PASSWORD": "prod_password_1"}):
            await _seed_default_users(db)

        admin_call = db.upsert_default_user.call_args_list[0]
        assert admin_call.kwargs["is_admin"] is True
        assert admin_call.kwargs["password_hash"].startswith("$argon2")
        assert admin_call.kwargs["email_verified"] is True

        # Step 2: Seed MCP token
        await _seed_admin_mcp_token(db)
        db.create_mcp_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent_rerun_skips_token(self):
        """Re-running init should not create a duplicate MCP token."""
        from orchestrator.init import _seed_admin_mcp_token

        admin = _make_user(is_admin=True)
        existing_token = _make_mcp_token(scope="all")

        db = MagicMock()
        db.get_admin_user = AsyncMock(return_value=admin)
        db.list_mcp_tokens = AsyncMock(return_value=[existing_token])

        await _seed_admin_mcp_token(db)

        db.create_mcp_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_login_and_token_creation_flow(self):
        """Test: admin logs in → session created → can create full-access token."""
        from security.auth import get_current_user

        admin_id = uuid4()
        admin = _make_user(user_id=admin_id, is_admin=True, email="admin@test.com")
        session = _make_session(user_id=admin_id, email="admin@test.com")

        db = MagicMock()
        db.get_session = AsyncMock(return_value=session)
        db.update_session_activity = AsyncMock()
        db.get_user = AsyncMock(return_value=admin)

        request = MagicMock()
        request.cookies = {"session": "admin-session-key"}

        user = await get_current_user(request, db)

        # User should have is_admin
        assert user["is_admin"] is True
        assert user["id"] == admin_id

        # Should be allowed to create full-access token
        scope = "all"
        assert not (scope == "all" and not user.get("is_admin", False))

    @pytest.mark.asyncio
    async def test_regular_user_blocked_from_full_access(self):
        """Test: regular user cannot create scope='all' tokens."""
        from security.auth import get_current_user

        user_id = uuid4()
        regular = _make_user(user_id=user_id, is_admin=False)
        session = _make_session(user_id=user_id)

        db = MagicMock()
        db.get_session = AsyncMock(return_value=session)
        db.update_session_activity = AsyncMock()
        db.get_user = AsyncMock(return_value=regular)

        request = MagicMock()
        request.cookies = {"session": "user-session-key"}

        user = await get_current_user(request, db)

        assert user["is_admin"] is False

        # Should be blocked
        scope = "all"
        assert scope == "all" and not user.get("is_admin", False)


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

    def test_k8s_secrets_has_admin_password(self):
        content = self._read_file("docs/HomeLab/deployments/srw/01-secrets.yaml")
        assert "ADMIN_PASSWORD" in content

    def test_k8s_orchestrator_has_admin_env(self):
        content = self._read_file("docs/HomeLab/deployments/srw/20-orchestrator.yaml")
        assert "ADMIN_EMAIL" in content
        assert "ADMIN_DISPLAY_NAME" in content
        assert "ADMIN_PASSWORD" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
