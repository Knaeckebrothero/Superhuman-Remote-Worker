"""Tests for the Headscale API client — pre-auth key generation and node management.

Tests cover:
1. HeadscaleClient.__init__ — availability detection from env vars
2. init() — HTTP client creation, user resolution, API connectivity
3. close() — client cleanup
4. create_auth_key() — key generation with correct tags, expiration, ephemeral flag
5. delete_node() — node lookup by hostname and deletion
6. _resolve_user_id() — user ID resolution from user list
7. is_available property — reflects configuration and init state
8. Error handling: HTTP errors, missing user, API timeout, invalid responses
9. Edge cases: empty node list, multiple nodes, already deleted node, no client
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# =============================================================================
# Helpers
# =============================================================================


def _make_user_list_response(*users):
    """Build a mock /api/v1/user response with the given user dicts."""
    return {"users": list(users)}


def _make_user(name: str, user_id: int) -> dict:
    return {"id": user_id, "name": name, "createdAt": "2026-01-01T00:00:00Z"}


def _make_node(node_id: int, given_name: str, name: str = "") -> dict:
    return {
        "id": node_id,
        "givenName": given_name,
        "name": name or given_name,
        "ipAddresses": ["100.64.0.1"],
        "online": True,
    }


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


# =============================================================================
# Test: __init__ and is_available property
# =============================================================================


class TestHeadscaleClientInit:
    """Tests for HeadscaleClient construction and availability detection."""

    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    def test_available_when_both_env_vars_set(self):
        """Client is available when both URL and API key are configured."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is True

    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    def test_unavailable_when_api_key_missing(self):
        """Client is unavailable when API key is not set."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is False

    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "")
    def test_unavailable_when_url_missing(self):
        """Client is unavailable when Headscale URL is not set."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is False

    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "")
    def test_unavailable_when_both_missing(self):
        """Client is unavailable when neither env var is set."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is False

    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    def test_initial_state_no_client_no_user_id(self):
        """Freshly constructed client has no HTTP client or user ID yet."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client._client is None
        assert client._user_id is None


# =============================================================================
# Test: init()
# =============================================================================


class TestInit:
    """Tests for HeadscaleClient.init() — HTTP client setup and user resolution."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_creates_client_and_resolves_user(self):
        """init() creates an httpx client and resolves the user ID."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()

        mock_resp = _mock_response(
            _make_user_list_response(_make_user("srw", 42))
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        assert client._user_id == "42"
        assert client.is_available is True

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "nonexistent")
    async def test_init_marks_unavailable_when_user_not_found(self):
        """init() sets available=False when the configured user doesn't exist."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()

        mock_resp = _mock_response(
            _make_user_list_response(_make_user("srw", 42), _make_user("admin", 1))
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        assert client._user_id is None
        assert client.is_available is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "")
    async def test_init_noop_when_not_configured(self):
        """init() does nothing when client is not available."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        await client.init()

        assert client._client is None
        assert client._user_id is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_handles_api_error_during_user_resolution(self):
        """init() sets available=False when user list API call fails."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()

        mock_resp = _mock_response({}, status_code=500)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        assert client._user_id is None
        assert client.is_available is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_handles_network_timeout(self):
        """init() sets available=False when user list times out."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()

        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(side_effect=httpx.TimeoutException("Connection timed out"))

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        assert client._user_id is None
        assert client.is_available is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_handles_empty_user_list(self):
        """init() sets available=False when the API returns an empty user list."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()

        mock_resp = _mock_response({"users": []})
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        assert client._user_id is None
        assert client.is_available is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_sets_correct_headers(self):
        """init() creates the HTTP client with correct auth header and content type."""
        from vm.controller.headscale_client import HeadscaleClient

        captured_kwargs = {}

        def capture_constructor(**kwargs):
            captured_kwargs.update(kwargs)
            mock_http = AsyncMock()
            mock_resp = _mock_response(
                _make_user_list_response(_make_user("srw", 1))
            )
            mock_http.get = AsyncMock(return_value=mock_resp)
            return mock_http

        client = HeadscaleClient()

        with patch("vm.controller.headscale_client.httpx.AsyncClient", side_effect=capture_constructor):
            await client.init()

        assert captured_kwargs["headers"]["Authorization"] == "Bearer test-api-key"
        assert captured_kwargs["headers"]["Content-Type"] == "application/json"
        assert captured_kwargs["base_url"] == "http://headscale:8080"
        assert captured_kwargs["timeout"] == 15.0


# =============================================================================
# Test: close()
# =============================================================================


class TestClose:
    """Tests for HeadscaleClient.close() — HTTP client cleanup."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_close_calls_aclose_on_client(self):
        """close() calls aclose() on the underlying httpx client."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        await client.close()

        mock_http.aclose.assert_awaited_once()
        assert client._client is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "")
    async def test_close_noop_when_no_client(self):
        """close() does nothing when no HTTP client exists."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client._client is None

        # Should not raise
        await client.close()
        assert client._client is None


# =============================================================================
# Test: create_auth_key()
# =============================================================================


class TestCreateAuthKey:
    """Tests for HeadscaleClient.create_auth_key() — ephemeral key generation."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_success(self):
        """create_auth_key() returns a key string on success."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_resp = _mock_response({
            "preAuthKey": {
                "key": "hskey-auth-abc123",
                "user": "srw",
                "ephemeral": True,
                "reusable": False,
            }
        })
        mock_http.post = AsyncMock(return_value=mock_resp)

        key = await client.create_auth_key("job-uuid-111")

        assert key == "hskey-auth-abc123"
        mock_http.post.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_sends_correct_payload(self):
        """create_auth_key() sends ephemeral=True, reusable=False, tag:vm."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_resp = _mock_response({
            "preAuthKey": {"key": "hskey-auth-xyz"}
        })
        mock_http.post = AsyncMock(return_value=mock_resp)

        await client.create_auth_key("test-job-id")

        call_args = mock_http.post.call_args
        assert call_args[0][0] == "/api/v1/preauthkey"
        payload = call_args[1]["json"]
        assert payload["user"] == "42"
        assert payload["reusable"] is False
        assert payload["ephemeral"] is True
        assert payload["aclTags"] == ["tag:vm"]
        assert "expiration" in payload

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.AUTH_KEY_EXPIRY_MINUTES", 10)
    async def test_create_auth_key_expiry_format(self):
        """create_auth_key() sends an ISO 8601 expiration timestamp."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "1"

        mock_resp = _mock_response({"preAuthKey": {"key": "k"}})
        mock_http.post = AsyncMock(return_value=mock_resp)

        await client.create_auth_key("test-job")

        payload = mock_http.post.call_args[1]["json"]
        expiry = payload["expiration"]
        # Should be in the format YYYY-MM-DDTHH:MM:SSZ
        assert expiry.endswith("Z")
        assert "T" in expiry

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_when_unavailable(self):
        """create_auth_key() returns None when client is not available."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        client._available = False

        result = await client.create_auth_key("job-id")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_when_no_client(self):
        """create_auth_key() returns None when HTTP client is not initialized."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        # _available is True, but _client is None (init() not called)
        assert client._client is None

        result = await client.create_auth_key("job-id")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_on_http_error(self):
        """create_auth_key() returns None when the API returns an error."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_resp = _mock_response({}, status_code=403)
        mock_http.post = AsyncMock(return_value=mock_resp)

        result = await client.create_auth_key("job-id")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_on_timeout(self):
        """create_auth_key() returns None when the request times out."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_http.post = AsyncMock(
            side_effect=httpx.TimeoutException("read timed out")
        )

        result = await client.create_auth_key("job-id")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_on_missing_key_in_response(self):
        """create_auth_key() returns None when API response lacks the key field."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        # Response has preAuthKey but no "key" inside it
        mock_resp = _mock_response({"preAuthKey": {"user": "srw", "ephemeral": True}})
        mock_http.post = AsyncMock(return_value=mock_resp)

        result = await client.create_auth_key("job-id")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_on_empty_response(self):
        """create_auth_key() returns None when API response is completely empty."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_resp = _mock_response({})
        mock_http.post = AsyncMock(return_value=mock_resp)

        result = await client.create_auth_key("job-id")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_create_auth_key_returns_none_on_connection_error(self):
        """create_auth_key() returns None when connection to Headscale fails."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_http.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await client.create_auth_key("job-id")
        assert result is None


# =============================================================================
# Test: delete_node()
# =============================================================================


class TestDeleteNode:
    """Tests for HeadscaleClient.delete_node() — node lookup and deletion."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_success_by_given_name(self):
        """delete_node() finds node by givenName and deletes it."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        job_id = "aaaa-bbbb-cccc"
        list_resp = _mock_response({
            "nodes": [
                _make_node(10, "vm-other-job"),
                _make_node(99, f"vm-{job_id}"),
            ]
        })
        delete_resp = _mock_response({})
        mock_http.get = AsyncMock(return_value=list_resp)
        mock_http.delete = AsyncMock(return_value=delete_resp)

        result = await client.delete_node(job_id)

        assert result is True
        mock_http.delete.assert_awaited_once_with("/api/v1/node/99")

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_success_by_name_field(self):
        """delete_node() finds node by 'name' field when givenName differs."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        job_id = "1111-2222-3333"
        # givenName is different, but name matches
        list_resp = _mock_response({
            "nodes": [
                _make_node(55, "some-other-name", f"vm-{job_id}"),
            ]
        })
        delete_resp = _mock_response({})
        mock_http.get = AsyncMock(return_value=list_resp)
        mock_http.delete = AsyncMock(return_value=delete_resp)

        result = await client.delete_node(job_id)

        assert result is True
        mock_http.delete.assert_awaited_once_with("/api/v1/node/55")

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_not_found(self):
        """delete_node() returns False when no matching node exists."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        list_resp = _mock_response({
            "nodes": [
                _make_node(1, "vm-other-job-1"),
                _make_node(2, "vm-other-job-2"),
            ]
        })
        mock_http.get = AsyncMock(return_value=list_resp)

        result = await client.delete_node("nonexistent-job")

        assert result is False
        mock_http.delete.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_empty_node_list(self):
        """delete_node() returns False when the node list is empty."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        list_resp = _mock_response({"nodes": []})
        mock_http.get = AsyncMock(return_value=list_resp)

        result = await client.delete_node("some-job")

        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_returns_false_when_unavailable(self):
        """delete_node() returns False when client is not available."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        client._available = False

        result = await client.delete_node("job-id")
        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_returns_false_when_no_client(self):
        """delete_node() returns False when HTTP client is not initialized."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client._client is None

        result = await client.delete_node("job-id")
        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_handles_list_http_error(self):
        """delete_node() returns False when the node list API call fails."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        list_resp = _mock_response({}, status_code=500)
        mock_http.get = AsyncMock(return_value=list_resp)

        result = await client.delete_node("job-id")
        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_handles_delete_http_error(self):
        """delete_node() returns False when the delete API call fails."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        job_id = "delete-fail-job"
        list_resp = _mock_response({
            "nodes": [_make_node(77, f"vm-{job_id}")]
        })
        delete_resp = _mock_response({}, status_code=500)
        mock_http.get = AsyncMock(return_value=list_resp)
        mock_http.delete = AsyncMock(return_value=delete_resp)

        result = await client.delete_node(job_id)
        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_handles_timeout(self):
        """delete_node() returns False when the node list request times out."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_http.get = AsyncMock(
            side_effect=httpx.TimeoutException("read timed out")
        )

        result = await client.delete_node("job-id")
        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_uses_correct_hostname(self):
        """delete_node() constructs hostname as 'vm-{job_id}'."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        job_id = "test-uuid-value"
        # Return a node with matching givenName
        list_resp = _mock_response({
            "nodes": [_make_node(1, f"vm-{job_id}")]
        })
        delete_resp = _mock_response({})
        mock_http.get = AsyncMock(return_value=list_resp)
        mock_http.delete = AsyncMock(return_value=delete_resp)

        await client.delete_node(job_id)

        # Verify the GET was called to list nodes
        mock_http.get.assert_awaited_once_with("/api/v1/node")

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_picks_first_matching_node(self):
        """delete_node() deletes the first matching node when multiple match."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        job_id = "dup-job"
        # Two nodes with the same hostname (shouldn't happen, but edge case)
        list_resp = _mock_response({
            "nodes": [
                _make_node(10, f"vm-{job_id}"),
                _make_node(20, f"vm-{job_id}"),
            ]
        })
        delete_resp = _mock_response({})
        mock_http.get = AsyncMock(return_value=list_resp)
        mock_http.delete = AsyncMock(return_value=delete_resp)

        result = await client.delete_node(job_id)

        assert result is True
        # Should delete the first match (id=10), not both
        mock_http.delete.assert_awaited_once_with("/api/v1/node/10")

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_handles_missing_nodes_key(self):
        """delete_node() returns False when API response lacks 'nodes' key."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        # Response is valid JSON but missing expected structure
        list_resp = _mock_response({"status": "ok"})
        mock_http.get = AsyncMock(return_value=list_resp)

        result = await client.delete_node("job-id")
        assert result is False

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_delete_node_handles_connection_error(self):
        """delete_node() returns False on a connection error."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http
        client._user_id = "42"

        mock_http.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await client.delete_node("job-id")
        assert result is False


# =============================================================================
# Test: _resolve_user_id()
# =============================================================================


class TestResolveUserId:
    """Tests for HeadscaleClient._resolve_user_id() — user lookup."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_returns_id_as_string(self):
        """_resolve_user_id() returns the user ID as a string."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response(
            _make_user_list_response(
                _make_user("admin", 1),
                _make_user("srw", 42),
                _make_user("test", 99),
            )
        )
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")

        assert result == "42"
        mock_http.get.assert_awaited_once_with("/api/v1/user")

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_returns_none_when_not_found(self):
        """_resolve_user_id() returns None when the username doesn't exist."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response(
            _make_user_list_response(_make_user("admin", 1), _make_user("other", 2))
        )
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_empty_user_list(self):
        """_resolve_user_id() returns None when the user list is empty."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response({"users": []})
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_returns_none_on_http_error(self):
        """_resolve_user_id() returns None when the API call fails."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response({}, status_code=500)
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_returns_none_when_no_client(self):
        """_resolve_user_id() returns None when HTTP client is not initialized."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client._client is None

        result = await client._resolve_user_id("srw")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_handles_timeout(self):
        """_resolve_user_id() returns None when the request times out."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_http.get = AsyncMock(
            side_effect=httpx.TimeoutException("read timed out")
        )

        result = await client._resolve_user_id("srw")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_handles_missing_users_key(self):
        """_resolve_user_id() returns None when API response lacks 'users' key."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response({"status": "ok"})
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")
        assert result is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_returns_first_match(self):
        """_resolve_user_id() returns the first matching user if duplicates exist."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response(
            _make_user_list_response(
                _make_user("srw", 10),
                _make_user("srw", 20),  # Duplicate name (shouldn't happen)
            )
        )
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")
        assert result == "10"

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_resolve_user_id_case_sensitive(self):
        """_resolve_user_id() performs case-sensitive name matching."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        client._client = mock_http

        mock_resp = _mock_response(
            _make_user_list_response(_make_user("SRW", 1))
        )
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = await client._resolve_user_id("srw")
        assert result is None


# =============================================================================
# Test: Graceful degradation — all methods safe when unconfigured
# =============================================================================


class TestGracefulDegradation:
    """Verify all public methods degrade gracefully when Headscale is unconfigured."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "")
    async def test_full_lifecycle_when_unconfigured(self):
        """All methods return safe defaults when Headscale is not configured."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is False

        await client.init()
        assert client._client is None

        key = await client.create_auth_key("any-job")
        assert key is None

        deleted = await client.delete_node("any-job")
        assert deleted is False

        # close() should be safe too
        await client.close()

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    async def test_methods_safe_before_init(self):
        """create_auth_key and delete_node return defaults before init() is called."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is True
        # But _client is None because init() wasn't called

        key = await client.create_auth_key("some-job")
        assert key is None

        deleted = await client.delete_node("some-job")
        assert deleted is False


# =============================================================================
# Test: Integration-style — init + create_auth_key + delete_node + close
# =============================================================================


class TestLifecycleIntegration:
    """Integration-style tests that exercise the full client lifecycle."""

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_create_key_delete_close(self):
        """Full lifecycle: init -> create key -> delete node -> close."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()

        # Set up mock HTTP client
        mock_http = AsyncMock(spec=httpx.AsyncClient)

        # init() will call GET /api/v1/user
        user_resp = _mock_response(
            _make_user_list_response(_make_user("srw", 5))
        )

        # create_auth_key() will call POST /api/v1/preauthkey
        key_resp = _mock_response({
            "preAuthKey": {"key": "hskey-auth-lifecycle-test"}
        })

        job_id = "lifecycle-test-job"

        # delete_node() will call GET /api/v1/node then DELETE /api/v1/node/{id}
        node_resp = _mock_response({
            "nodes": [_make_node(88, f"vm-{job_id}")]
        })
        delete_resp = _mock_response({})

        # Configure mock to return different responses per call
        mock_http.get = AsyncMock(side_effect=[user_resp, node_resp])
        mock_http.post = AsyncMock(return_value=key_resp)
        mock_http.delete = AsyncMock(return_value=delete_resp)

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        assert client.is_available is True
        assert client._user_id == "5"

        # Create a key
        key = await client.create_auth_key(job_id)
        assert key == "hskey-auth-lifecycle-test"

        # Delete the node
        deleted = await client.delete_node(job_id)
        assert deleted is True

        # Close
        await client.close()
        assert client._client is None

    @pytest.mark.asyncio
    @patch("vm.controller.headscale_client.HEADSCALE_API_KEY", "test-api-key")
    @patch("vm.controller.headscale_client.HEADSCALE_URL", "http://headscale:8080")
    @patch("vm.controller.headscale_client.HEADSCALE_USER", "srw")
    async def test_init_failure_blocks_subsequent_operations(self):
        """When init() fails to resolve user, create_auth_key and delete_node are no-ops."""
        from vm.controller.headscale_client import HeadscaleClient

        client = HeadscaleClient()
        assert client.is_available is True

        # init() — user not found
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        user_resp = _mock_response(
            _make_user_list_response(_make_user("other", 1))
        )
        mock_http.get = AsyncMock(return_value=user_resp)

        with patch("vm.controller.headscale_client.httpx.AsyncClient", return_value=mock_http):
            await client.init()

        # Now unavailable
        assert client.is_available is False

        # Subsequent operations should be no-ops
        key = await client.create_auth_key("job-id")
        assert key is None

        deleted = await client.delete_node("job-id")
        assert deleted is False

        await client.close()
