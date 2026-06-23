"""Tests for the BFF cookie-session auth path (`_resolve_from_cookie`).

These lock in the idle-renewal behavior: a session idle past
``SRW_SESSION_IDLE_TIMEOUT_S`` is re-validated against Keycloak (refreshed in
place) rather than blind-deleted, so an idle-but-still-valid session survives
instead of force-logging the user out. The absolute lifetime cap stays a hard
stop, and a genuine KC rejection still deletes the row and returns None.

Mirrors the AsyncMock + patch.object style of ``tests/test_mcp.py``. We call
``_resolve_from_cookie`` directly (it takes ``(session_id, db)``), patch
``_resolve_user_from_claims`` to a sentinel so the tests stay focused on the
cookie/refresh logic, and pin the idle/skew env so timing is deterministic
regardless of ambient ``.env``.
"""

import asyncio
import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from security.auth import _resolve_from_cookie
from security.kc_client import KeycloakClientError

SESSION_ID = "sess-123"
SENTINEL_USER = {"id": "user-1", "display_name": "Tester", "is_approved": True}

# Refresh response shape from kc_bff_client.refresh (see _refresh_session_in_place).
_REFRESHED = {
    "access_token": "refreshed.access.jwt",
    "refresh_token": "refreshed-refresh-token",
    "id_token": "refreshed.id.jwt",
    "expires_in": 600,
}


def _session(**overrides):
    """A valid srw_session row (timezone-aware datetimes), overridable per-test."""
    now = datetime.now(UTC)
    sess = {
        "id": SESSION_ID,
        "user_id": uuid4(),
        "kc_sub": "kc-sub-abc",
        "kc_sid": "kc-sid-xyz",
        "access_token": "stored.access.jwt",
        "refresh_token": "stored-refresh-token",
        "id_token": "stored.id.jwt",
        "access_expires_at": now + timedelta(seconds=600),
        "absolute_expires_at": now + timedelta(days=30),
        "created_at": now - timedelta(minutes=5),
        "last_seen_at": now,
        "user_agent": "pytest",
        "created_ip": "127.0.0.1",
    }
    sess.update(overrides)
    return sess


def _make_db(sess):
    db = AsyncMock()
    db.get_srw_session = AsyncMock(return_value=sess)
    db.delete_srw_session = AsyncMock()
    db.refresh_srw_session_tokens = AsyncMock()
    db.touch_srw_session_last_seen = AsyncMock()
    return db


_DEFAULT_CLAIMS = {"sub": "kc-sub-abc", "realm_access": {"roles": ["user"]}}
_DEFAULT_ID_CLAIMS = {
    "sub": "kc-sub-abc",
    "email": "u@t.com",
    "preferred_username": "tester",
}
_UNSET = object()


@contextmanager
def _auth_patches(
    *,
    validate_token=_UNSET,
    decode_id_token=_UNSET,
    refresh=_UNSET,
    refresh_side_effect=None,
):
    """Patch the auth module's KC/OIDC seams and pin the idle/skew env.

    Yields the mocks so tests can assert on calls. ``validate_token=None``
    means "stored token fails validation"; ``refresh_side_effect`` raises from
    kc_bff_client.refresh (the dead-KC case).
    """
    claims = _DEFAULT_CLAIMS if validate_token is _UNSET else validate_token
    id_claims = _DEFAULT_ID_CLAIMS if decode_id_token is _UNSET else decode_id_token
    refresh_payload = dict(_REFRESHED) if refresh is _UNSET else refresh

    refresh_mock = AsyncMock()
    if refresh_side_effect is not None:
        refresh_mock.side_effect = refresh_side_effect
    else:
        refresh_mock.return_value = refresh_payload

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "SRW_SESSION_IDLE_TIMEOUT_S": "1800",
                    "SRW_ACCESS_TOKEN_REFRESH_SKEW_S": "60",
                },
            )
        )
        m_validate = stack.enter_context(
            patch("security.auth.oidc_validator.validate_token", return_value=claims)
        )
        m_decode = stack.enter_context(
            patch(
                "security.auth.oidc_validator.decode_id_token", return_value=id_claims
            )
        )
        stack.enter_context(patch("security.auth.kc_bff_client.refresh", refresh_mock))
        m_resolve = stack.enter_context(
            patch(
                "security.auth._resolve_user_from_claims",
                AsyncMock(return_value=dict(SENTINEL_USER)),
            )
        )
        yield {
            "validate_token": m_validate,
            "decode_id_token": m_decode,
            "refresh": refresh_mock,
            "resolve_user": m_resolve,
        }


class TestResolveFromCookie:
    """Idle-renewal behavior + the unchanged guard paths."""

    @pytest.mark.asyncio
    async def test_idle_with_live_kc_renews_silently(self):
        """Idle past the window but KC alive → refresh in place, no delete."""
        now = datetime.now(UTC)
        sess = _session(
            last_seen_at=now - timedelta(hours=1),  # well past 1800s idle
            access_expires_at=now + timedelta(seconds=600),  # not near-expiry
        )
        db = _make_db(sess)

        with _auth_patches() as m:
            result = await _resolve_from_cookie(SESSION_ID, db)
            await asyncio.sleep(0)  # let the fire-and-forget touch task run

        assert result == SENTINEL_USER
        m["refresh"].assert_awaited_once_with("stored-refresh-token")
        db.refresh_srw_session_tokens.assert_awaited_once()
        db.delete_srw_session.assert_not_called()
        db.touch_srw_session_last_seen.assert_awaited_once_with(SESSION_ID)

    @pytest.mark.asyncio
    async def test_idle_with_dead_kc_deletes_and_returns_none(self):
        """Idle + KC rejects the refresh token → row deleted, None, no resolve."""
        now = datetime.now(UTC)
        sess = _session(last_seen_at=now - timedelta(hours=1))
        db = _make_db(sess)

        with _auth_patches(refresh_side_effect=KeycloakClientError("revoked")) as m:
            result = await _resolve_from_cookie(SESSION_ID, db)

        assert result is None
        db.delete_srw_session.assert_awaited_once_with(SESSION_ID)
        m["validate_token"].assert_not_called()

    @pytest.mark.asyncio
    async def test_absolute_expiry_hard_stops_without_refresh(self):
        """Past absolute lifetime → delete, no refresh — even if also idle."""
        now = datetime.now(UTC)
        sess = _session(
            absolute_expires_at=now - timedelta(seconds=1),
            last_seen_at=now - timedelta(hours=1),  # adversarial: absolute must win
        )
        db = _make_db(sess)

        with _auth_patches() as m:
            result = await _resolve_from_cookie(SESSION_ID, db)

        assert result is None
        db.delete_srw_session.assert_awaited_once_with(SESSION_ID)
        m["refresh"].assert_not_called()

    @pytest.mark.asyncio
    async def test_near_expiry_not_idle_still_refreshes(self):
        """Access token within the refresh skew (not idle) → refresh, no delete."""
        now = datetime.now(UTC)
        sess = _session(
            last_seen_at=now,  # not idle
            access_expires_at=now + timedelta(seconds=30),  # within 60s skew
        )
        db = _make_db(sess)

        with _auth_patches() as m:
            result = await _resolve_from_cookie(SESSION_ID, db)
            await asyncio.sleep(0)

        assert result == SENTINEL_USER
        m["refresh"].assert_awaited_once_with("stored-refresh-token")
        db.delete_srw_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_fully_valid_session_no_refresh_no_delete(self):
        """Fresh + far from expiry → single read + touch, no KC round-trip."""
        now = datetime.now(UTC)
        sess = _session(
            last_seen_at=now,
            access_expires_at=now + timedelta(seconds=600),
        )
        db = _make_db(sess)

        with _auth_patches() as m:
            result = await _resolve_from_cookie(SESSION_ID, db)
            await asyncio.sleep(0)

        assert result == SENTINEL_USER
        m["refresh"].assert_not_called()
        db.delete_srw_session.assert_not_called()
        db.touch_srw_session_last_seen.assert_awaited_once_with(SESSION_ID)

    @pytest.mark.asyncio
    async def test_missing_session_returns_none(self):
        """No row → None, fall through to other auth paths; nothing mutated."""
        db = _make_db(None)

        with _auth_patches() as m:
            result = await _resolve_from_cookie(SESSION_ID, db)

        assert result is None
        db.delete_srw_session.assert_not_called()
        m["refresh"].assert_not_called()

    @pytest.mark.asyncio
    async def test_post_refresh_token_fails_validation_deletes(self):
        """Idle refresh succeeds but the new token won't validate → delete, None."""
        now = datetime.now(UTC)
        sess = _session(last_seen_at=now - timedelta(hours=1))
        db = _make_db(sess)

        with _auth_patches(validate_token=None):
            result = await _resolve_from_cookie(SESSION_ID, db)

        assert result is None
        db.delete_srw_session.assert_awaited_once_with(SESSION_ID)

    @pytest.mark.asyncio
    async def test_no_sub_in_claims_deletes(self):
        """Neither access nor id token yields a sub → kill the session."""
        now = datetime.now(UTC)
        sess = _session(
            last_seen_at=now, access_expires_at=now + timedelta(seconds=600)
        )
        db = _make_db(sess)

        with _auth_patches(
            validate_token={"realm_access": {"roles": ["user"]}},  # no sub
            decode_id_token={},  # no sub
        ):
            result = await _resolve_from_cookie(SESSION_ID, db)

        assert result is None
        db.delete_srw_session.assert_awaited_once_with(SESSION_ID)

    @pytest.mark.asyncio
    async def test_idle_refresh_updates_in_memory_id_token(self):
        """A2: after refresh, the downstream decode sees the NEW id_token.

        Proves _refresh_session_in_place mutates sess in place — otherwise
        decode_id_token would be handed the stale stored id_token.
        """
        now = datetime.now(UTC)
        sess = _session(last_seen_at=now - timedelta(hours=1))
        db = _make_db(sess)
        new_id_token = "brand.new.id.jwt"

        with _auth_patches(
            refresh={
                "access_token": "refreshed.access.jwt",
                "refresh_token": "refreshed-refresh-token",
                "id_token": new_id_token,
                "expires_in": 600,
            }
        ) as m:
            result = await _resolve_from_cookie(SESSION_ID, db)
            await asyncio.sleep(0)

        assert result == SENTINEL_USER
        m["decode_id_token"].assert_called_once_with(new_id_token)
