"""Tests for the internal SSH-gateway target resolution endpoint.

``GET /api/internal/ssh-targets/{handle}`` is how the SSH gateway (which
holds no database credentials) turns a presented handle + key fingerprint
into a routable workspace target. It is the seam plan 1's design rests on:

* Anti-enumeration: unknown handle, unknown/unapproved key, and "your key is
  fine but you don't own this thread" must be indistinguishable (identical
  404, same detail) — see ``PostgresDB.get_thread_id_by_ssh_handle`` and
  ``PostgresDB.resolve_user_by_ssh_fingerprint``'s docstrings, which both
  name this endpoint as the reason.
* No wake-on-connect (D5): resolving a handle must never restore a suspended
  workspace as a side effect, so this deliberately does NOT reuse
  ``_resolve_thread_for_forwarding`` (which does exactly that) or
  ``thread_runtime_is_preparable`` (which folds 'suspended' into "OK to
  prepare" and says by its own docstring that it is not a lifecycle
  predicate).
* Liveness is three independent axes — session status, lane +
  its retirement marker, and workspace_container/vm status — collapsed by
  ``resolve_workspace_state``. The idle sweeper's own query keys on exactly
  "session ended, workspace still ready", so that combination is the common
  case this must get right, not an edge case.
"""

import pytest
from fastapi import HTTPException

import main
from services.ssh_gateway_targets import resolve_workspace_state
from tests._route_inventory import mounted_routes

USER = "00000000-0000-0000-0000-000000000001"
THREAD = "00000000-0000-0000-0000-000000000002"
FINGERPRINT = "SHA256:" + "A" * 43


def _thread(**over):
    base = {
        "id": THREAD,
        "user_id": USER,
        "status": "active",
        "execution_lane": "pinned",
        "runtime_retirement_token": None,
    }
    base.update(over)
    return base


def test_live_when_workspace_ready():
    assert (
        resolve_workspace_state(_thread(), {"workspace_container": {"status": "ready"}})
        == "live"
    )


def test_never_provisioned_is_absence_of_status_not_absence_of_key():
    """workspace_container exists on every thread carrying only git fields."""
    assert (
        resolve_workspace_state(
            _thread(),
            {"workspace_container": {"git_remote_url": "x", "repo_name": "y"}},
        )
        == "never_provisioned"
    )
    assert resolve_workspace_state(_thread(), {}) == "never_provisioned"


def test_reclaimed_is_distinct_from_suspended():
    assert (
        resolve_workspace_state(
            _thread(), {"workspace_container": {"status": "suspended"}}
        )
        == "suspended"
    )
    assert (
        resolve_workspace_state(
            _thread(),
            {"workspace_container": {"status": "suspended", "volume_reclaimed": True}},
        )
        == "reclaimed"
    )


def test_ended_session_reported_even_when_workspace_still_ready():
    """The idle sweeper keys on exactly this combination, so it is the common
    case, not an edge case."""
    assert (
        resolve_workspace_state(
            _thread(status="ended"), {"workspace_container": {"status": "ready"}}
        )
        == "ended"
    )


def test_pinned_retirement_token_means_ending():
    assert (
        resolve_workspace_state(
            _thread(runtime_retirement_token="tok"),
            {"workspace_container": {"status": "ready"}},
        )
        == "ending"
    )


def test_stateless_retirement_flags_mean_ending():
    assert (
        resolve_workspace_state(
            _thread(execution_lane="stateless"),
            {
                "workspace_container": {"status": "ready"},
                "_stateless_workspace_retirement_pending": True,
            },
        )
        == "ending"
    )


def test_session_suspended_is_not_workspace_suspended():
    """threads.status='suspended' means awaiting-user, not a stopped workspace."""
    assert (
        resolve_workspace_state(
            _thread(status="suspended"), {"workspace_container": {"status": "ready"}}
        )
        == "live"
    )


@pytest.fixture
def internal(monkeypatch):
    async def _allow(request):
        return None

    monkeypatch.setattr(main, "require_internal", _allow)


@pytest.mark.asyncio
async def test_requires_internal_key(monkeypatch):
    async def _deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    monkeypatch.setattr(main, "require_internal", _deny)
    with pytest.raises(HTTPException) as excinfo:
        await main.get_ssh_target(
            request=object(), handle="s-7f3a91c2", fingerprint=FINGERPRINT
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_handle_and_unauthorized_are_identical(internal, monkeypatch):
    """Anti-enumeration: an attacker must not learn which handles exist."""

    async def _no_thread(handle):
        return None

    monkeypatch.setattr(main.postgres_db, "get_thread_id_by_ssh_handle", _no_thread)
    with pytest.raises(HTTPException) as unknown:
        await main.get_ssh_target(
            request=object(), handle="s-aaaaaaaa", fingerprint=FINGERPRINT
        )

    async def _thread_ok(handle):
        return THREAD

    async def _user(fp):
        return {"id": "00000000-0000-0000-0000-000000000009"}

    async def _no_access(user, db, entity_id):
        return False

    monkeypatch.setattr(main.postgres_db, "get_thread_id_by_ssh_handle", _thread_ok)
    monkeypatch.setattr(main.postgres_db, "resolve_user_by_ssh_fingerprint", _user)
    monkeypatch.setattr(main, "user_can_access_ide_entity", _no_access)
    with pytest.raises(HTTPException) as denied:
        await main.get_ssh_target(
            request=object(), handle="s-7f3a91c2", fingerprint=FINGERPRINT
        )

    assert unknown.value.status_code == denied.value.status_code == 404
    assert unknown.value.detail == denied.value.detail


@pytest.mark.asyncio
async def test_rejects_a_malformed_handle_before_touching_the_database(
    internal, monkeypatch
):
    called = False

    async def _tripwire(handle):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(main.postgres_db, "get_thread_id_by_ssh_handle", _tripwire)
    with pytest.raises(HTTPException):
        await main.get_ssh_target(
            request=object(), handle="s-abc\nProxyCommand x", fingerprint=FINGERPRINT
        )
    assert called is False


@pytest.mark.asyncio
async def test_non_live_state_returns_200_with_no_pod_ip(internal, monkeypatch):
    """The gateway needs a readable reason, so this is not an error response."""

    async def _thread_id(handle):
        return THREAD

    async def _user(fp):
        return {"id": USER}

    async def _access(user, db, entity_id):
        return True

    async def _get_thread(tid):
        return _thread(status="active")

    monkeypatch.setattr(main.postgres_db, "get_thread_id_by_ssh_handle", _thread_id)
    monkeypatch.setattr(main.postgres_db, "resolve_user_by_ssh_fingerprint", _user)
    monkeypatch.setattr(main, "user_can_access_ide_entity", _access)
    monkeypatch.setattr(main.postgres_db, "get_thread", _get_thread)
    monkeypatch.setattr(
        main,
        "thread_metadata_object",
        lambda t: {"workspace_container": {"status": "suspended"}},
    )

    result = await main.get_ssh_target(
        request=object(), handle="s-7f3a91c2", fingerprint=FINGERPRINT
    )
    assert result["state"] == "suspended"
    assert result["pod_ip"] is None


def test_endpoint_does_not_use_the_restoring_resolver():
    """_resolve_thread_for_forwarding restores suspended workspaces as a side
    effect. Using it here would silently implement wake-on-connect, which the
    design rules out."""
    import inspect

    source = inspect.getsource(main.get_ssh_target)
    for forbidden in (
        "_resolve_thread_for_forwarding",
        "thread_runtime_is_preparable",
        "resolve_pod_ip",
    ):
        assert forbidden not in source, f"{forbidden} must not be used here"


def test_ssh_target_route_is_mounted():
    """Every test above calls the handler directly, so none of them prove
    FastAPI actually serves this path at this method — a typo'd decorator
    path or a route registered under the wrong verb would pass every other
    test here and still 404 in production. Same rationale as
    ``test_ssh_key_routes_are_mounted`` in test_ssh_key_endpoints.py."""
    routes = mounted_routes(main.app)
    assert ("GET", "/api/internal/ssh-targets/{handle}") in routes
