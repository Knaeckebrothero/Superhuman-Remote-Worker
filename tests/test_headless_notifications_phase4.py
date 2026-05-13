"""Tests for Phase 4 of headless persistent sessions: magic-link tokens,
email dispatch, dedup + rate limiting. The DB round-trip (real INSERT +
SELECT + CAS UPDATE) is covered in integration tests; here we mock the
postgres pool and exercise the service module's contract surface.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import orchestrator.services.headless_notifications as hn


# ---------------------------------------------------------------------------
# Helper: a fake db.acquire() context manager backed by a recordable mock conn
# ---------------------------------------------------------------------------


def _make_db(**overrides):
    """Build a MagicMock db with .acquire() yielding a mock connection
    whose methods can be stubbed via overrides.

    overrides: dict of attr-name → coroutine return value or AsyncMock.
    """
    fake_conn = MagicMock()
    for attr in ("fetchval", "fetchrow", "execute", "fetch"):
        if attr in overrides:
            val = overrides[attr]
            if callable(val) and not isinstance(val, AsyncMock):
                setattr(fake_conn, attr, AsyncMock(side_effect=val))
            else:
                setattr(fake_conn, attr, AsyncMock(return_value=val))
        else:
            setattr(fake_conn, attr, AsyncMock(return_value=None))

    class _Acquire:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    db = MagicMock()
    db.acquire = lambda: _Acquire()
    db._fake_conn = fake_conn
    return db


# ---------------------------------------------------------------------------
# Section 1 — Token generation and validation primitives
# ---------------------------------------------------------------------------


class TestHashToken:
    def test_returns_sha256_hex(self):
        h = hn._hash_token("hello")
        # Deterministic SHA-256("hello").hexdigest()
        expected = hashlib.sha256(b"hello").hexdigest()
        assert h == expected

    def test_different_inputs_different_hashes(self):
        assert hn._hash_token("a") != hn._hash_token("b")


class TestGenerateMagicLinkToken:
    @pytest.mark.asyncio
    async def test_inserts_hash_not_raw(self):
        """The DB only ever stores SHA-256(token), never the raw token."""
        db = _make_db(fetchval="row-uuid")
        raw, row_id = await hn.generate_magic_link_token(
            db,
            purpose="approve_permission",
            user_id="user-1",
            approval_id="approval-1",
            thread_id="thread-1",
            intended_decision="approved",
        )
        assert row_id == "row-uuid"
        # The INSERT bound $1 to the *hash* of the raw token, not the raw.
        bound_args = db._fake_conn.fetchval.await_args.args
        sql = bound_args[0]
        assert "INSERT INTO magic_link_tokens" in sql
        bound_token_hash = bound_args[1]
        assert bound_token_hash == hn._hash_token(raw)
        assert bound_token_hash != raw

    @pytest.mark.asyncio
    async def test_rejects_invalid_decision(self):
        db = _make_db(fetchval="row-uuid")
        with pytest.raises(ValueError):
            await hn.generate_magic_link_token(
                db,
                purpose="approve_permission",
                user_id="u",
                approval_id="a",
                thread_id="t",
                intended_decision="maybe",
            )

    @pytest.mark.asyncio
    async def test_returns_url_safe_token(self):
        """secrets.token_urlsafe is base64-url — no padding, only safe chars."""
        db = _make_db(fetchval="x")
        raw, _ = await hn.generate_magic_link_token(
            db,
            purpose="approve_permission",
            user_id=None,
            approval_id=None,
            thread_id=None,
        )
        # url-safe charset: ASCII letters, digits, -, _.
        assert all(c.isalnum() or c in "-_" for c in raw)


class TestValidateMagicLink:
    @pytest.mark.asyncio
    async def test_returns_none_when_token_missing(self):
        db = _make_db(fetchrow=None)
        result = await hn.validate_magic_link(db, "anything")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_token(self):
        db = _make_db()
        result = await hn.validate_magic_link(db, "")
        assert result is None
        db._fake_conn.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_already_used(self):
        db = _make_db(
            fetchrow={
                "id": "x",
                "purpose": "approve_permission",
                "user_id": "u",
                "approval_id": "a",
                "thread_id": "t",
                "intended_decision": "approved",
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                "used_at": datetime.now(timezone.utc),  # already used
                "consumed_decision": "approved",
            }
        )
        result = await hn.validate_magic_link(db, "raw")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_expired(self):
        db = _make_db(
            fetchrow={
                "id": "x",
                "purpose": "approve_permission",
                "user_id": "u",
                "approval_id": "a",
                "thread_id": "t",
                "intended_decision": "approved",
                "expires_at": datetime.now(timezone.utc)
                - timedelta(minutes=1),  # expired
                "used_at": None,
                "consumed_decision": None,
            }
        )
        result = await hn.validate_magic_link(db, "raw")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_row_when_valid(self):
        db = _make_db(
            fetchrow={
                "id": "x",
                "purpose": "approve_permission",
                "user_id": "u",
                "approval_id": "a",
                "thread_id": "t",
                "intended_decision": "approved",
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                "used_at": None,
                "consumed_decision": None,
            }
        )
        result = await hn.validate_magic_link(db, "raw")
        assert result is not None
        assert result["id"] == "x"
        assert result["intended_decision"] == "approved"

    @pytest.mark.asyncio
    async def test_looks_up_by_hash_not_raw(self):
        """The SELECT binds the hash of the raw token, never the raw."""
        db = _make_db(fetchrow=None)
        await hn.validate_magic_link(db, "my-secret-token")
        bound = db._fake_conn.fetchrow.await_args.args
        assert bound[1] == hn._hash_token("my-secret-token")


class TestConsumeMagicLink:
    @pytest.mark.asyncio
    async def test_returns_row_on_first_consume(self):
        db = _make_db(
            fetchrow={
                "id": "x",
                "purpose": "approve_permission",
                "user_id": "u",
                "approval_id": "a",
                "thread_id": "t",
                "intended_decision": "approved",
                "consumed_decision": "approved",
            }
        )
        result = await hn.consume_magic_link(db, "x", "approved")
        assert result is not None
        assert result["consumed_decision"] == "approved"
        # The UPDATE binds WHERE used_at IS NULL (single-use CAS).
        sql = db._fake_conn.fetchrow.await_args.args[0]
        assert "WHERE id = $1 AND used_at IS NULL" in sql

    @pytest.mark.asyncio
    async def test_returns_none_on_double_consume(self):
        """CAS lost (used_at already set) → fetchrow returns None."""
        db = _make_db(fetchrow=None)
        result = await hn.consume_magic_link(db, "x", "approved")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_invalid_decision(self):
        db = _make_db()
        result = await hn.consume_magic_link(db, "x", "maybe")
        assert result is None
        db._fake_conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# Section 2 — Rate-limit probe
# ---------------------------------------------------------------------------
#
# The standalone dedup probe (`already_notified`) was retired 2026-05-13 —
# the sweeper SQL is now the authoritative dedup, and an in-process probe
# only duplicated logic that could drift apart. See
# orchestrator/main.py:thread_permission_notify_sweeper for the widened
# IN-set that absorbed the probe's responsibilities.


class TestThreadRateLimited:
    @pytest.mark.asyncio
    async def test_under_both_limits_returns_false(self):
        db = _make_db()
        # Two fetchvals: 5-min count, 60-min count. Sequence via side_effect.
        db._fake_conn.fetchval = AsyncMock(side_effect=[0, 0])
        assert await hn.thread_rate_limited(db, "t") is False

    @pytest.mark.asyncio
    async def test_over_5min_limit_returns_true(self):
        db = _make_db()
        db._fake_conn.fetchval = AsyncMock(side_effect=[5, 5])
        assert await hn.thread_rate_limited(db, "t") is True

    @pytest.mark.asyncio
    async def test_over_hour_limit_returns_true(self):
        db = _make_db()
        # Under 5min limit, but at hour limit.
        db._fake_conn.fetchval = AsyncMock(side_effect=[0, 10])
        assert await hn.thread_rate_limited(db, "t") is True


# ---------------------------------------------------------------------------
# Section 3 — send_permission_pending_email orchestration
# ---------------------------------------------------------------------------


def _email_service_mock(*, is_configured=True, send_returns=True):
    es = MagicMock()
    es.is_configured = is_configured
    es.cockpit_url = "http://cockpit"
    es._send = AsyncMock(return_value=send_returns)
    return es


class TestSendPermissionPendingEmail:
    @pytest.mark.asyncio
    async def test_skips_when_rate_limited(self):
        db = _make_db()
        # Sequence: 5min-count → 10 (over), 60min-count → 10 (over).
        db._fake_conn.fetchval = AsyncMock(side_effect=[10, 10])
        es = _email_service_mock()
        result = await hn.send_permission_pending_email(
            db,
            es,
            thread_id="t",
            approval_id="a",
            cockpit_external_url="http://x",
        )
        assert result == {"status": "skipped_rate_limit"}
        es._send.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_send_path(self):
        """Happy path: dedup ok, rate ok, user has email, SMTP works."""
        thread_row = {
            "id": "t",
            "user_id": "user-uuid",
            "title": "My session",
        }
        permission_row = {
            "id": "a",
            "tool_name": "run_command",
            "tool_args": '{"cmd": "ls"}',
            "requested_at": datetime.now(timezone.utc) - timedelta(minutes=2),
            "status": "pending",
        }
        user_row = {
            "id": "user-uuid",
            "email": "user@example.com",
            "display_name": "Alice",
        }

        # Sequencing — order matters:
        #   thread_rate_limited (fetchval x2)    → 0, 0
        #   thread row (fetchrow)                → thread_row
        #   permission row (fetchrow)            → permission_row
        #   user row (fetchrow)                  → user_row
        #   generate_magic_link_token x2 (fetchval) → "tok-row-1", "tok-row-2"
        #   record_notification (execute)        → None
        db = _make_db()
        db._fake_conn.fetchval = AsyncMock(
            side_effect=[
                0,  # rate_limited 5min
                0,  # rate_limited 60min
                "tok-row-1",  # generate_magic_link_token approve
                "tok-row-2",  # generate_magic_link_token deny
            ]
        )
        db._fake_conn.fetchrow = AsyncMock(
            side_effect=[thread_row, permission_row, user_row]
        )
        db._fake_conn.execute = AsyncMock()

        es = _email_service_mock(send_returns=True)
        result = await hn.send_permission_pending_email(
            db,
            es,
            thread_id="t",
            approval_id="a",
            cockpit_external_url="http://cockpit",
        )
        assert result["status"] == "sent"
        assert result["email_to"] == "user@example.com"
        # _send was called with the user's address.
        send_kwargs = es._send.await_args.kwargs
        assert send_kwargs["to"] == "user@example.com"
        assert "run_command" in send_kwargs["subject"]
        assert "approve" in send_kwargs["body_html"].lower()
        # thread_notifications.INSERT happened.
        assert db._fake_conn.execute.await_count >= 1

    @pytest.mark.asyncio
    async def test_skips_when_user_has_no_email(self):
        thread_row = {"id": "t", "user_id": "user-uuid", "title": "x"}
        permission_row = {
            "id": "a",
            "tool_name": "run_command",
            "tool_args": "{}",
            "requested_at": datetime.now(timezone.utc),
            "status": "pending",
        }
        user_row = {"id": "user-uuid", "email": None, "display_name": "Alice"}

        db = _make_db()
        db._fake_conn.fetchval = AsyncMock(side_effect=[0, 0])
        db._fake_conn.fetchrow = AsyncMock(
            side_effect=[thread_row, permission_row, user_row]
        )
        db._fake_conn.execute = AsyncMock()

        es = _email_service_mock()
        result = await hn.send_permission_pending_email(
            db,
            es,
            thread_id="t",
            approval_id="a",
            cockpit_external_url="http://cockpit",
        )
        assert result == {"status": "skipped_no_email"}
        es._send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_smtp_unconfigured(self):
        thread_row = {"id": "t", "user_id": "user-uuid", "title": "x"}
        permission_row = {
            "id": "a",
            "tool_name": "run_command",
            "tool_args": "{}",
            "requested_at": datetime.now(timezone.utc),
            "status": "pending",
        }
        user_row = {
            "id": "user-uuid",
            "email": "user@example.com",
            "display_name": "Alice",
        }

        db = _make_db()
        db._fake_conn.fetchval = AsyncMock(side_effect=[0, 0])
        db._fake_conn.fetchrow = AsyncMock(
            side_effect=[thread_row, permission_row, user_row]
        )
        db._fake_conn.execute = AsyncMock()

        es = _email_service_mock(is_configured=False)
        result = await hn.send_permission_pending_email(
            db,
            es,
            thread_id="t",
            approval_id="a",
            cockpit_external_url="http://cockpit",
        )
        assert result == {"status": "skipped_smtp"}
        es._send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_request_already_resolved(self):
        thread_row = {"id": "t", "user_id": "user-uuid", "title": "x"}
        permission_row = {
            "id": "a",
            "tool_name": "run_command",
            "tool_args": "{}",
            "requested_at": datetime.now(timezone.utc),
            "status": "approved",  # already resolved
        }
        db = _make_db()
        db._fake_conn.fetchval = AsyncMock(side_effect=[0, 0])
        db._fake_conn.fetchrow = AsyncMock(side_effect=[thread_row, permission_row])
        db._fake_conn.execute = AsyncMock()
        es = _email_service_mock()
        result = await hn.send_permission_pending_email(
            db,
            es,
            thread_id="t",
            approval_id="a",
            cockpit_external_url="http://cockpit",
        )
        assert result["status"] == "skipped_already_resolved"
        es._send.assert_not_called()
        # The race must be recorded in thread_notifications so the
        # sweeper's widened IN-set can suppress re-dispatch.
        assert db._fake_conn.execute.await_count >= 1
        recorded_sql = db._fake_conn.execute.await_args.args[0]
        assert "INSERT INTO thread_notifications" in recorded_sql
        bound = db._fake_conn.execute.await_args.args
        assert "skipped_already_resolved" in bound


# ---------------------------------------------------------------------------
# Section 4 — Magic-link URL composition
# ---------------------------------------------------------------------------


class TestBuildMagicLinkUrl:
    def test_appends_magic_approve_path(self):
        url = hn._build_magic_link_url("http://cockpit.example.com/", "raw-tok")
        assert url == "http://cockpit.example.com/magic/approve/raw-tok"

    def test_url_encodes_special_chars_in_token(self):
        url = hn._build_magic_link_url("http://x", "tok with space")
        assert "%20" in url or "+" in url
        # Must not contain a raw space.
        assert " " not in url

    def test_strips_trailing_slash_from_base(self):
        url = hn._build_magic_link_url("http://x///", "tok")
        assert url.startswith("http://x/magic/approve/")
        assert "////" not in url


class TestBuildPermissionEmailBodies:
    def test_returns_text_and_html(self):
        text, html = hn._build_permission_email_bodies(
            tool_name="run_command",
            tool_args_preview='{"cmd": "ls"}',
            approve_url="http://x/magic/approve/T1",
            deny_url="http://x/magic/approve/T2",
            cockpit_link="http://x/sessions/abc",
            request_age_minutes=3,
        )
        # Both bodies mention the tool and the links.
        assert "run_command" in text
        assert "run_command" in html
        assert "http://x/magic/approve/T1" in text
        assert "http://x/magic/approve/T1" in html
        assert "http://x/magic/approve/T2" in text
        assert "http://x/magic/approve/T2" in html
        # HTML escapes the args content.
        assert "&quot;" in html or "{&quot;cmd" in html or '"cmd"' in html


# ---------------------------------------------------------------------------
# Section 5 — _truncate_args_for_email
# ---------------------------------------------------------------------------


class TestTruncateArgsForEmail:
    def test_short_args_unchanged(self):
        out = hn._truncate_args_for_email({"a": 1})
        assert "1" in out
        assert "truncated" not in out

    def test_long_args_truncated_with_marker(self):
        out = hn._truncate_args_for_email({"x": "a" * 5000})
        assert "truncated" in out
        assert len(out) < 5000

    def test_handles_unserializable_input(self):
        class _NotJsonable:
            def __repr__(self):
                return "<NotJsonable>"

        # No exception; falls back to str() (json.dumps with default=str
        # handles this — but if the type explodes, we still get something).
        out = hn._truncate_args_for_email({"x": _NotJsonable()})
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# Section 6 — Sweeper dedup SQL contract
# ---------------------------------------------------------------------------
#
# Captures the SELECT the permission-notify sweeper builds and asserts on
# its shape. Live DB round-trips are covered by the smoke runbook; here we
# pin the SQL contract so regressions on the IN-set or recency floor are
# caught at unit-test time.


class TestPermissionNotifySweeperSQL:
    @pytest.mark.asyncio
    async def test_dedup_filter_includes_permanent_skips_and_floor(self):
        import asyncio

        import orchestrator.main as orch_main

        captured: dict = {}
        evt = asyncio.Event()

        async def _fake_fetch(query: str, *args):
            captured["query"] = query
            captured["args"] = args
            # Signal shutdown so the sweeper's wait_for exits and the
            # loop terminates cleanly after this first fetch.
            evt.set()
            return []

        fake_conn = MagicMock()
        fake_conn.fetch = _fake_fetch

        class _Acquire:
            async def __aenter__(self_inner):
                return fake_conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return None

        orch_main.postgres_db = MagicMock()
        orch_main.postgres_db.acquire = lambda: _Acquire()

        await orch_main.thread_permission_notify_sweeper(evt)

        q = captured.get("query", "")
        # Permanent skips suppress forever.
        assert "'skipped_no_email'" in q
        assert "'skipped_already_resolved'" in q
        # Transient skips have a recency floor.
        assert "'skipped_rate_limit'" in q
        assert "'skipped_smtp'" in q
        assert "make_interval(secs => $2)" in q
        # The recency-floor parameter is the second bind, expressed as
        # int seconds = 2 × sweeper interval (default 30s → 60s).
        args = captured.get("args", ())
        assert len(args) == 2
        # Args is (age_threshold_str, recency_floor_secs).
        assert isinstance(args[1], int)
        assert args[1] >= 60
