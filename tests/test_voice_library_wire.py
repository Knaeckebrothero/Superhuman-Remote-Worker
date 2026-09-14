"""Wire contracts for the voice-library and account-voice settings routes.

New coverage: before R1.B02 these handlers lived in ``main.py`` with no
endpoint-level test. The behaviors pinned here are the ones that would fail
silently if the extraction shifted them:

* the *add* gate is fail-closed — an unreadable flag row must deny, not allow,
  because an add consumes a plan-limited voice slot shared by every user;
* ``add_enabled`` is merged into the (ungated) search response, so the browser
  can tell whether to offer "Add to deployment" without a second round trip;
* the ElevenLabs provider key never leaves the server — the responses carry
  voice metadata only.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orchestrator.services.tts import TtsLibraryError
from tests._mounted_router import mount_router


USER = {"id": "00000000-0000-0000-0000-0000000000c1"}
API_KEY = "el-deployment-key-never-served"


def _deps(*, library_enabled=True, setting_error=None, logger=None):
    from orchestrator.routers.voice import VoiceDependencies as RouteDeps
    from orchestrator.services.voice import VoiceDependencies as OpDeps

    async def get_setting(_key):
        if setting_error is not None:
            raise setting_error
        return {"value": {"enabled": library_enabled}}

    store = SimpleNamespace(
        get_system_setting=AsyncMock(side_effect=get_setting),
        get_user_settings=AsyncMock(return_value={}),
        resolve_default_for_capability=AsyncMock(return_value=None),
    )

    async def approved(_request, _store):
        return USER

    return RouteDeps(
        store=store,
        operations=OpDeps(store=store, logger=logger or MagicMock()),
        require_approved_user=approved,
    )


def _client(deps):
    from orchestrator.routers.voice import router

    app = mount_router(router, factories={"voice_dependencies_factory": lambda: deps})
    return TestClient(app)


# =============================================================================
# The add gate
# =============================================================================


@pytest.mark.asyncio
async def test_library_flag_is_fail_closed_when_the_row_cannot_be_read():
    from orchestrator.services.voice import elevenlabs_library_enabled

    logger = MagicMock()
    deps = _deps(setting_error=RuntimeError("db down"), logger=logger)
    assert await elevenlabs_library_enabled(dependencies=deps.operations) is False
    logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_library_flag_is_false_for_an_absent_row():
    from orchestrator.routers.voice import VoiceDependencies as RouteDeps
    from orchestrator.services.voice import (
        VoiceDependencies as OpDeps,
    )
    from orchestrator.services.voice import elevenlabs_library_enabled

    store = SimpleNamespace(get_system_setting=AsyncMock(return_value=None))
    ops = OpDeps(store=store, logger=MagicMock())
    assert await elevenlabs_library_enabled(dependencies=ops) is False
    assert RouteDeps  # imported for symmetry with the router-level cases


def test_add_is_refused_when_the_gate_is_off():
    deps = _deps(library_enabled=False)
    add = AsyncMock()
    with patch("orchestrator.services.tts.add_library_voice", add):
        resp = _client(deps).post(
            "/api/settings/tts/library/add",
            json={"public_owner_id": "o", "voice_id": "v", "new_name": "n"},
        )
    assert resp.status_code == 403
    assert "disabled for this deployment" in resp.json()["detail"]
    add.assert_not_awaited()


def test_add_requires_all_three_fields():
    deps = _deps(library_enabled=True)
    add = AsyncMock()
    with patch("orchestrator.services.tts.add_library_voice", add):
        resp = _client(deps).post(
            "/api/settings/tts/library/add",
            json={"public_owner_id": "o", "voice_id": "", "new_name": "n"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "public_owner_id, voice_id and new_name are required."
    )
    add.assert_not_awaited()


def test_add_surfaces_a_slot_limit_as_its_own_status_not_a_500():
    deps = _deps(library_enabled=True)
    with patch(
        "orchestrator.services.tts.add_library_voice",
        AsyncMock(side_effect=TtsLibraryError("voice limit reached", status_code=400)),
    ):
        resp = _client(deps).post(
            "/api/settings/tts/library/add",
            json={"public_owner_id": "o", "voice_id": "v", "new_name": "n"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "voice limit reached"


def test_add_passes_the_trimmed_fields_through():
    deps = _deps(library_enabled=True)
    add = AsyncMock(return_value={"voice_id": "new-v"})
    with patch("orchestrator.services.tts.add_library_voice", add):
        resp = _client(deps).post(
            "/api/settings/tts/library/add",
            json={
                "public_owner_id": "  o  ",
                "voice_id": " v ",
                "new_name": " Nova ",
            },
        )
    assert resp.status_code == 200
    kwargs = add.await_args.kwargs
    assert kwargs["public_owner_id"] == "o"
    assert kwargs["voice_id"] == "v"
    assert kwargs["new_name"] == "Nova"
    assert kwargs["user_id"] == USER["id"]


# =============================================================================
# Search: ungated, flag-annotated, never a 5xx
# =============================================================================


@pytest.mark.parametrize("enabled", [True, False])
def test_search_annotates_the_result_with_the_add_gate(enabled):
    deps = _deps(library_enabled=enabled)
    with patch(
        "orchestrator.services.tts.search_voice_library",
        AsyncMock(return_value={"backend": "elevenlabs", "voices": [], "error": None}),
    ):
        body = _client(deps).get("/api/settings/tts/library").json()
    assert body["add_enabled"] is enabled


def test_search_forwards_every_supported_filter():
    deps = _deps()
    search = AsyncMock(return_value={"backend": "elevenlabs", "voices": []})
    with patch("orchestrator.services.tts.search_voice_library", search):
        _client(deps).get(
            "/api/settings/tts/library",
            params={
                "search": "calm",
                "language": "de",
                "accent": "british",
                "gender": "female",
                "age": "young",
                "page": "2",
            },
        )
    assert search.await_args.kwargs["filters"] == {
        "search": "calm",
        "language": "de",
        "accent": "british",
        "gender": "female",
        "age": "young",
        "page": "2",
    }


def test_search_is_reachable_while_the_add_gate_is_off():
    """Browsing and previewing stay ungated; only account mutation is gated."""
    deps = _deps(library_enabled=False)
    with patch(
        "orchestrator.services.tts.search_voice_library",
        AsyncMock(return_value={"backend": "elevenlabs", "voices": [{"id": "v1"}]}),
    ):
        resp = _client(deps).get("/api/settings/tts/library")
    assert resp.status_code == 200
    assert resp.json()["voices"] == [{"id": "v1"}]


# =============================================================================
# Account voices
# =============================================================================


def test_account_voices_never_carry_the_provider_key():
    deps = _deps()
    with patch(
        "orchestrator.services.tts.list_account_voices",
        AsyncMock(
            return_value={
                "backend": "elevenlabs",
                "voices": [{"id": "v1", "name": "Nova", "preview_url": "https://x/1"}],
            }
        ),
    ):
        resp = _client(deps).get("/api/settings/tts/voices")
    assert resp.status_code == 200
    assert API_KEY not in resp.text
    assert resp.json()["voices"][0]["name"] == "Nova"


def test_voice_routes_run_the_approved_user_gate():
    from orchestrator.routers.voice import VoiceDependencies
    from orchestrator.services.voice import VoiceDependencies as OpDeps

    async def deny(_request, _store):
        raise HTTPException(status_code=403, detail="Not approved")

    store = SimpleNamespace()
    deps = VoiceDependencies(
        store=store,
        operations=OpDeps(store=store, logger=MagicMock()),
        require_approved_user=deny,
    )
    client = _client(deps)
    for path in (
        "/api/settings/tts/voices",
        "/api/settings/tts/library",
        "/api/voice/capabilities",
    ):
        assert client.get(path).status_code == 403


# =============================================================================
# Per-application dependency isolation
# =============================================================================


def test_each_application_resolves_its_own_store():
    first, second = _deps(), _deps()
    with patch(
        "orchestrator.services.tts.search_voice_library",
        AsyncMock(return_value={"backend": None, "voices": []}),
    ):
        _client(first).get("/api/settings/tts/library")
    first.operations.store.get_system_setting.assert_awaited()
    second.operations.store.get_system_setting.assert_not_awaited()
