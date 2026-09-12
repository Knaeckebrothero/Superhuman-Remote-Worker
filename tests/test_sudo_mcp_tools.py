"""The sudo approval tools as an approver experiences them over MCP.

Three things are pinned here, all observed failing on a real VM job:
the listing must show the command line the gate evaluated (``command`` alone
is ``/bin/bash`` for a ``-c`` wrapper) and the full UUID, and approve/deny
must accept the short id the listing prints back.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MCP_TRANSPORT", "stdio")

from mcp_server import server as _mcp_server_mod  # noqa: E402

REQUEST_ID = "f678a94d-2f1e-4f0a-9a1c-7b3d5e2c8a10"
OTHER_ID = "f678a94d-9c3b-4d7e-8f21-0a4b6c9d1e33"
THIRD_ID = "0a726be6-1b2c-4d3e-9f80-5a6b7c8d9e01"

LOGIN_SHELL_REQUEST = {
    "id": REQUEST_ID,
    "job_id": "74b871dd-1111-2222-3333-444455556666",
    "vm_name": "agent-vm-397b",
    "command": "/bin/bash",
    "arguments": ["bash", "--login", "-c", "id && docker version"],
    "status": "pending",
    "requesting_user": "agent-host",
    "target_user": "agent-host",
    "requested_at": "2026-09-05T18:12:00+00:00",
    "expires_at": "2026-09-05T18:42:00+00:00",
}


def _client(**attrs):
    client = AsyncMock()
    for name, value in attrs.items():
        getattr(client, name).return_value = value
    return client


class TestListing:
    @pytest.mark.asyncio
    async def test_listing_shows_the_command_line_the_gate_evaluated(self):
        client = _client(list_sudo_requests=[LOGIN_SHELL_REQUEST])
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.list_sudo_requests()

        assert "Command: /bin/bash --login -c 'id && docker version'" in out

    @pytest.mark.asyncio
    async def test_listing_prints_the_full_uuid(self):
        """An approver cannot act on an id the listing truncated."""
        client = _client(list_sudo_requests=[LOGIN_SHELL_REQUEST])
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.list_sudo_requests()

        assert REQUEST_ID in out
        assert "f678a94d" in out

    @pytest.mark.asyncio
    async def test_listing_shows_the_expiry(self):
        client = _client(list_sudo_requests=[LOGIN_SHELL_REQUEST])
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.list_sudo_requests()

        assert "Expires: 2026-09-05T18:42:00+00:00" in out

    @pytest.mark.asyncio
    async def test_empty_listing_is_unchanged(self):
        client = _client(list_sudo_requests=[])
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.list_sudo_requests()

        assert out == "No sudo approval requests found."


class TestShortIdResolution:
    @pytest.mark.asyncio
    async def test_full_uuid_is_passed_straight_through(self):
        client = _client(approve_sudo_request={"status": "approved"})
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.approve_sudo_request(REQUEST_ID)

        client.approve_sudo_request.assert_awaited_once_with(REQUEST_ID, reason="")
        client.list_sudo_requests.assert_not_awaited()
        assert "approved" in out

    @pytest.mark.asyncio
    async def test_unique_short_id_resolves_to_the_full_uuid(self):
        """The listing prints 8 characters; approving from it must work."""
        client = _client(
            list_sudo_requests=[
                LOGIN_SHELL_REQUEST,
                {**LOGIN_SHELL_REQUEST, "id": THIRD_ID},
            ],
            approve_sudo_request={"status": "approved"},
        )
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.approve_sudo_request("f678a94d")

        client.approve_sudo_request.assert_awaited_once_with(REQUEST_ID, reason="")
        assert REQUEST_ID in out

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_is_an_error_and_decides_nothing(self):
        client = _client(
            list_sudo_requests=[
                LOGIN_SHELL_REQUEST,
                {**LOGIN_SHELL_REQUEST, "id": OTHER_ID},
            ]
        )
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.approve_sudo_request("f678a94d")

        client.approve_sudo_request.assert_not_awaited()
        assert "ambiguous" in out.lower()
        assert REQUEST_ID in out and OTHER_ID in out

    @pytest.mark.asyncio
    async def test_unknown_prefix_is_an_error(self):
        client = _client(list_sudo_requests=[LOGIN_SHELL_REQUEST])
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.deny_sudo_request("deadbeef", reason="no")

        client.deny_sudo_request.assert_not_awaited()
        assert "deadbeef" in out
        assert "no sudo request" in out.lower()

    @pytest.mark.asyncio
    async def test_deny_resolves_the_short_id_too(self):
        client = _client(
            list_sudo_requests=[LOGIN_SHELL_REQUEST],
            deny_sudo_request={"status": "denied"},
        )
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            out = await _mcp_server_mod.deny_sudo_request("f678a94d", reason="not now")

        client.deny_sudo_request.assert_awaited_once_with(REQUEST_ID, reason="not now")
        assert "denied" in out

    @pytest.mark.asyncio
    async def test_resolution_searches_beyond_the_pending_page(self):
        """A decided request is still addressable by its short id."""
        client = _client(
            list_sudo_requests=[{**LOGIN_SHELL_REQUEST, "status": "approved"}],
            approve_sudo_request={"status": "approved"},
        )
        with patch.object(_mcp_server_mod, "_get_client", return_value=client):
            await _mcp_server_mod.approve_sudo_request("f678a94d")

        assert client.list_sudo_requests.await_args.kwargs.get("status") is None
