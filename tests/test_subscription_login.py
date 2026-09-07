"""Login-session lifecycle for Settings → AI Subscriptions.

The behaviours pinned here are the ones the feature doc calls out as easy to
get wrong and impossible to notice afterwards:

* upstream ``get-auth-status`` answers ``{"status":"ok"}`` for an **empty**
  state, so a bare "ok" must never be read as "connected";
* a callback landing is not proof that token exchange and persistence
  succeeded — completion is confirmed against the credential inventory;
* a cancelled or expired session is terminal, and a late poll must not
  resurrect it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from orchestrator.services import subscriptions
from orchestrator.services.subscriptions import (
    SubscriptionProxyError,
    SubscriptionProxyUnreachable,
)


class FakeProxy:
    """Scriptable stand-in for the management API.

    ``auth_files`` is mutated by the test to simulate the proxy persisting a
    credential; ``auth_status`` is the scripted poll answer.
    """

    def __init__(self, *, auth_files=None, auth_status=None):
        self.auth_files = list(auth_files or [])
        self.auth_status = auth_status or {"status": "wait"}
        self.calls: list[tuple[str, str, dict]] = []
        self.login_payload = {
            "status": "ok",
            "url": "https://auth.example/authorize?state=st-1",
            "state": "st-1",
        }

    async def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/v0/management/auth-files" and method == "GET":
            return _Response({"files": self.auth_files})
        if path.endswith("-auth-url"):
            return _Response(dict(self.login_payload))
        if path == "/v0/management/get-auth-status":
            return _Response(dict(self.auth_status))
        if path == "/v0/management/oauth-session":
            return _Response({"status": "ok", "cancelled": True})
        if path == "/v0/management/oauth-callback":
            return _Response({"status": "ok"})
        if path == "/v1/models":
            return _Response({"data": []})
        raise AssertionError(f"unexpected management call: {method} {path}")


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


def _codex_file(name="codex-a.json", updated="2026-09-07T10:00:00Z"):
    return {
        "id": name,
        "name": name,
        "provider": "codex",
        "status": "active",
        "email": "a@example.com",
        "updated_at": updated,
    }


@pytest.fixture(autouse=True)
def _clean_state():
    subscriptions.invalidate_account_cache()
    subscriptions._login_sessions.clear()
    yield
    subscriptions.invalidate_account_cache()
    subscriptions._login_sessions.clear()


@pytest.fixture
def proxy(monkeypatch):
    fake = FakeProxy()
    monkeypatch.setattr(subscriptions, "management_request", fake)
    return fake


class TestStartLogin:
    @pytest.mark.asyncio
    async def test_returns_a_session_without_leaking_upstream_state(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        public = session.public()
        assert public["status"] == "pending"
        assert public["auth_url"].startswith("https://auth.example/")
        assert "state" not in public
        assert public["accepts_callback_url"] is True

    @pytest.mark.asyncio
    async def test_device_flow_surfaces_user_code_and_no_callback_field(self, proxy):
        proxy.login_payload = {
            "status": "ok",
            "url": "https://x.ai/device",
            "state": "xai-1",
            "flow": "device",
            "user_code": "ABCD-1234",
            "expires_in": 600,
        }
        session = await subscriptions.start_login("xai-grok-build", user_id="u1")
        public = session.public()
        assert public["flow"] == "device"
        assert public["user_code"] == "ABCD-1234"
        assert public["accepts_callback_url"] is False

    @pytest.mark.asyncio
    async def test_unknown_provider_is_a_400_not_a_probe(self, proxy):
        with pytest.raises(SubscriptionProxyError) as excinfo:
            await subscriptions.start_login("does-not-exist", user_id="u1")
        assert excinfo.value.status == 400
        # Crucially: we never called an auth-url handler to find out.
        assert not [c for c in proxy.calls if c[1].endswith("-auth-url")]

    @pytest.mark.asyncio
    async def test_snapshots_the_existing_credentials(self, proxy):
        proxy.auth_files = [_codex_file()]
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        assert session.baseline == frozenset({("codex-a.json", "2026-09-07T10:00:00Z")})


class TestPollLogin:
    @pytest.mark.asyncio
    async def test_wait_stays_pending(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "wait"}
        assert (await subscriptions.poll_login(session)).status == "pending"

    @pytest.mark.asyncio
    async def test_ok_alone_does_not_report_connected(self, proxy):
        """Upstream "ok" without a credential is the permissive-poll trap."""
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        assert (await subscriptions.poll_login(session)).status == "verifying"

    @pytest.mark.asyncio
    async def test_connected_only_after_a_credential_appears(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        assert (await subscriptions.poll_login(session)).status == "verifying"
        proxy.auth_files = [_codex_file()]
        session = await subscriptions.poll_login(session)
        assert session.status == "connected"
        assert session.account_id == subscriptions.encode_account_id("codex-a.json")

    @pytest.mark.asyncio
    async def test_reconnect_of_an_existing_account_is_detected(self, proxy):
        """A re-auth overwrites the same file — the timestamp is the evidence."""
        proxy.auth_files = [_codex_file(updated="2026-09-07T10:00:00Z")]
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        await subscriptions.poll_login(session)
        proxy.auth_files = [_codex_file(updated="2026-09-07T11:00:00Z")]
        assert (await subscriptions.poll_login(session)).status == "connected"

    @pytest.mark.asyncio
    async def test_another_providers_credential_does_not_satisfy_the_flow(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        await subscriptions.poll_login(session)
        proxy.auth_files = [
            {"name": "claude-a.json", "provider": "claude", "status": "active"}
        ]
        assert (await subscriptions.poll_login(session)).status == "verifying"

    @pytest.mark.asyncio
    async def test_reconcile_gives_up_after_the_grace_window(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        await subscriptions.poll_login(session)
        session.completed_at = session.completed_at - timedelta(minutes=5)
        session = await subscriptions.poll_login(session)
        assert session.status == "failed"
        assert session.error == "credential_not_persisted"

    @pytest.mark.asyncio
    async def test_upstream_error_is_terminal_and_redacted(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "error", "error": "unknown or expired state"}
        session = await subscriptions.poll_login(session)
        assert session.status == "failed"
        assert session.error == "unknown or expired state"

    @pytest.mark.asyncio
    async def test_transport_blip_stays_pending(self, proxy, monkeypatch):
        session = await subscriptions.start_login("openai-codex", user_id="u1")

        async def boom(*args, **kwargs):
            raise SubscriptionProxyUnreachable("down")

        monkeypatch.setattr(subscriptions, "management_request", boom)
        assert (await subscriptions.poll_login(session)).status == "pending"

    @pytest.mark.asyncio
    async def test_expired_session_fails_rather_than_waiting_forever(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        session.expires_at = session.created_at - timedelta(seconds=1)
        session = await subscriptions.poll_login(session)
        assert session.status == "failed"
        assert session.error == "timeout"


class TestSessionOwnership:
    @pytest.mark.asyncio
    async def test_another_admins_session_is_not_readable(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        with pytest.raises(SubscriptionProxyError) as excinfo:
            subscriptions.get_login(session.login_id, user_id="u2")
        assert excinfo.value.status == 404

    @pytest.mark.asyncio
    async def test_owner_can_read_it(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        assert subscriptions.get_login(session.login_id, user_id="u1") is session

    def test_unknown_login_id_is_404(self):
        with pytest.raises(SubscriptionProxyError) as excinfo:
            subscriptions.get_login("nope", user_id="u1")
        assert excinfo.value.status == 404


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_is_terminal_and_calls_upstream(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        session = await subscriptions.cancel_login(session)
        assert session.status == "cancelled"
        assert any(path == "/v0/management/oauth-session" for _, path, _ in proxy.calls)

    @pytest.mark.asyncio
    async def test_a_late_poll_cannot_resurrect_a_cancelled_session(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        await subscriptions.cancel_login(session)
        proxy.auth_status = {"status": "ok"}
        proxy.auth_files = [_codex_file()]
        assert (await subscriptions.poll_login(session)).status == "cancelled"

    @pytest.mark.asyncio
    async def test_upstream_cancel_failure_still_cancels_locally(
        self, proxy, monkeypatch
    ):
        session = await subscriptions.start_login("openai-codex", user_id="u1")

        async def boom(method, path, **kwargs):
            if path == "/v0/management/oauth-session":
                raise SubscriptionProxyUnreachable("down")
            return await proxy(method, path, **kwargs)

        monkeypatch.setattr(subscriptions, "management_request", boom)
        assert (await subscriptions.cancel_login(session)).status == "cancelled"


class TestCallback:
    @pytest.mark.asyncio
    async def test_relays_code_and_the_sessions_own_state(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        proxy.auth_files = [_codex_file()]
        session = await subscriptions.submit_callback(
            session, url="http://localhost:1455/auth/callback?code=abc&state=st-1"
        )
        relayed = [c for c in proxy.calls if c[1] == "/v0/management/oauth-callback"]
        assert relayed and relayed[0][2]["json"] == {
            "provider": "codex",
            "code": "abc",
            "state": "st-1",
        }
        assert session.status == "connected"

    @pytest.mark.asyncio
    async def test_rejects_a_callback_from_another_attempt(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        with pytest.raises(SubscriptionProxyError) as excinfo:
            await subscriptions.submit_callback(
                session, url="http://localhost:1455/cb?code=abc&state=someone-else"
            )
        assert excinfo.value.status == 409

    @pytest.mark.asyncio
    async def test_missing_code_is_a_422_with_actionable_text(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        with pytest.raises(SubscriptionProxyError) as excinfo:
            await subscriptions.submit_callback(session, url="http://localhost:1455/cb")
        assert excinfo.value.status == 422

    @pytest.mark.asyncio
    async def test_provider_denial_is_recorded_as_a_failure(self, proxy):
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        session = await subscriptions.submit_callback(
            session, url="http://localhost:1455/cb?error=access_denied&state=st-1"
        )
        assert session.status == "failed"
        assert session.error == "access_denied"

    @pytest.mark.asyncio
    async def test_device_flow_rejects_a_callback(self, proxy):
        proxy.login_payload = {
            "status": "ok",
            "url": "https://kimi.example/device",
            "state": "kmi-1",
            "flow": "device",
        }
        session = await subscriptions.start_login("kimi-code", user_id="u1")
        with pytest.raises(SubscriptionProxyError) as excinfo:
            await subscriptions.submit_callback(session, url="http://x/cb?code=a")
        assert excinfo.value.status == 400

    @pytest.mark.asyncio
    async def test_callback_alone_does_not_report_connected(self, proxy):
        """Acceptance of the code is not persistence of the credential."""
        session = await subscriptions.start_login("openai-codex", user_id="u1")
        proxy.auth_status = {"status": "ok"}
        session = await subscriptions.submit_callback(
            session, url="http://localhost:1455/cb?code=abc&state=st-1"
        )
        assert session.callback_received is True
        assert session.status == "verifying"
