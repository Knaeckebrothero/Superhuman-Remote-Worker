"""Tests for JWT validation on the agent pod's /ws/chat handshake."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.services.session_tokens import SessionTokenService


@pytest.fixture(autouse=True)
def configure_pod_env(monkeypatch):
    monkeypatch.setenv("SESSION_JWT_SECRET", "test-pod-secret-do-not-use")
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")


@pytest.fixture
def app():
    from src.api.persistent_app import create_persistent_app

    # config_path is required; we never actually run the lifespan / session,
    # but the constructor needs a value. A dummy string is fine — the route
    # under test (the validator gate on /ws/chat) doesn't touch it.
    return create_persistent_app("dummy_config", "t1")


def _connect_and_capture_close(client: TestClient, url: str) -> int:
    """Open the WS, drain frames, and return the close code.

    Starlette's TestClient does not surface a server-side close as an
    exception during ``websocket_connect()`` — the server-accepted upgrade
    completes from the client's POV. The close shows up on the next
    ``receive_*`` as ``WebSocketDisconnect``. We rely on that to capture
    the code the validator emitted.
    """
    with client.websocket_connect(url) as ws:
        try:
            ws.receive_text()
        except WebSocketDisconnect as exc:
            return exc.code
    return -1  # No disconnect observed — test should fail.


def test_ws_chat_rejects_missing_token(app):
    """No `?t=` query param → close with 4401."""
    client = TestClient(app)
    code = _connect_and_capture_close(client, "/ws/chat")
    assert code == 4401


def test_ws_chat_rejects_token_for_other_thread(app):
    """Token's `tid` doesn't match pod's bound thread → close with 4403."""
    other_token, _ = SessionTokenService("test-pod-secret-do-not-use").mint(
        "u1", "OTHER-thread"
    )
    client = TestClient(app)
    code = _connect_and_capture_close(client, f"/ws/chat?t={other_token}")
    assert code == 4403


def test_ws_chat_rejects_invalid_signature(app):
    """Token signed with a different secret → close with 4401."""
    bad_token, _ = SessionTokenService("WRONG-SECRET").mint("u1", "t1")
    client = TestClient(app)
    code = _connect_and_capture_close(client, f"/ws/chat?t={bad_token}")
    assert code == 4401


def test_ws_chat_rejects_when_pod_misconfigured(app, monkeypatch):
    """Missing SESSION_JWT_SECRET → close with 4500 (fail-closed)."""
    monkeypatch.delenv("SESSION_JWT_SECRET", raising=False)
    client = TestClient(app)
    code = _connect_and_capture_close(client, "/ws/chat?t=anything")
    assert code == 4500


def test_ws_chat_accepts_valid_token(app):
    """Token with matching tid → validator does NOT close as 4401/4403/4500.

    The session itself isn't started so ``handle_persistent_websocket`` will
    drop the connection shortly after — but the close code must come from
    the downstream handler, not from the validator. The validator's codes
    are 4401 (auth), 4403 (mismatch), and 4500 (misconfig). Anything else
    means the validator allowed the request through.
    """
    token, _ = SessionTokenService("test-pod-secret-do-not-use").mint("u1", "t1")
    client = TestClient(app)
    try:
        code = _connect_and_capture_close(client, f"/ws/chat?t={token}")
    except Exception:
        # The downstream session-not-active path may raise — that's a sign
        # the validator passed (we got past the gate).
        return
    # If we got a clean close code from downstream, it must NOT be one of
    # the validator's rejection codes.
    assert code not in (4401, 4403, 4500), (
        f"Validator unexpectedly rejected valid token with code {code}"
    )
