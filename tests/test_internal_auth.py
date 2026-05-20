"""P4b — Track B internal-auth helpers + endpoint coverage.

The agent ↔ orchestrator boundary is now defended at two layers:

  1. ``helm/templates/ingress.yaml`` strips ``/api/agents/`` and
     ``/api/internal/`` from the public API ingress (a Traefik
     ``IPAllowList`` middleware on a high-priority Ingress).
  2. Every agent-internal endpoint calls ``require_internal`` (or, for
     the dual-callable job mutations, ``require_internal_or_job_access``).

This file tests layer 2. Layer 1 lives in the Helm chart and is
out-of-scope for unit tests — it's exercised in cluster.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import security.access as access_module


def _make_request(headers: dict[str, str] | None = None) -> MagicMock:
    """Build a stub FastAPI Request with the given headers dict."""
    req = MagicMock()
    req.headers = headers or {}
    req.cookies = {}
    return req


def _patch_caller_and_db(user: dict, db):
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch(
            "security.access.require_approved_user",
            AsyncMock(return_value=user),
        )
    )
    stack.enter_context(patch("main.postgres_db", db))
    return stack


# =============================================================================
# is_internal_call / require_internal — the helpers themselves
# =============================================================================


class TestIsInternalCall:
    def test_returns_false_when_key_unset(self):
        with patch.object(access_module, "_INTERNAL_KEY", ""):
            req = _make_request({"X-Internal-Key": "anything"})
            assert access_module.is_internal_call(req) is False

    def test_returns_false_when_header_missing(self):
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({})
            assert access_module.is_internal_call(req) is False

    def test_returns_false_when_header_wrong(self):
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({"X-Internal-Key": "wrong"})
            assert access_module.is_internal_call(req) is False

    def test_returns_true_when_header_matches(self):
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({"X-Internal-Key": "secret"})
            assert access_module.is_internal_call(req) is True


class TestRequireInternal:
    @pytest.mark.asyncio
    async def test_raises_401_without_key(self):
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({})
            with pytest.raises(HTTPException) as exc:
                await access_module.require_internal(req)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_401_when_key_unset(self):
        """Fail-closed: an unconfigured cluster denies all internal calls
        loudly rather than silently letting traffic through."""
        with patch.object(access_module, "_INTERNAL_KEY", ""):
            req = _make_request({"X-Internal-Key": "anything"})
            with pytest.raises(HTTPException) as exc:
                await access_module.require_internal(req)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_passes_with_valid_key(self):
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({"X-Internal-Key": "secret"})
            await access_module.require_internal(req)  # no raise


class TestRequireInternalOrJobAccess:
    @pytest.mark.asyncio
    async def test_internal_call_skips_user_auth(self, job_a, fake_db):
        """Agent path: valid X-Internal-Key bypasses the user auth chain."""
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({"X-Internal-Key": "secret"})
            with patch(
                "security.access.require_approved_user",
                AsyncMock(side_effect=AssertionError("user auth called")),
            ):
                caller, job = await access_module.require_internal_or_job_access(
                    req, fake_db, str(job_a["id"])
                )
        assert caller is None
        assert str(job["id"]) == str(job_a["id"])

    @pytest.mark.asyncio
    async def test_internal_call_with_missing_job_404(self, fake_db):
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({"X-Internal-Key": "secret"})
            with pytest.raises(HTTPException) as exc:
                await access_module.require_internal_or_job_access(
                    req, fake_db, "00000000-0000-0000-0000-000000000000"
                )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_user_path_runs_require_job_access(self, user_a, job_a, fake_db):
        """No internal key → falls through to require_job_access. user_a
        is the owner of job_a → call succeeds."""
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({})
            with patch(
                "security.access.require_approved_user",
                AsyncMock(return_value=user_a),
            ):
                caller, job = await access_module.require_internal_or_job_access(
                    req, fake_db, str(job_a["id"])
                )
        assert str(caller["id"]) == str(user_a["id"])
        assert str(job["id"]) == str(job_a["id"])

    @pytest.mark.asyncio
    async def test_user_path_blocks_cross_user(self, user_b, job_a, fake_db):
        """No internal key + cross-user → 403 from require_job_access."""
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            req = _make_request({})
            with patch(
                "security.access.require_approved_user",
                AsyncMock(return_value=user_b),
            ):
                with pytest.raises(HTTPException) as exc:
                    await access_module.require_internal_or_job_access(
                        req, fake_db, str(job_a["id"])
                    )
        assert exc.value.status_code == 403


# =============================================================================
# Endpoint integration — pure-internal endpoints reject without the key
# =============================================================================


class TestPureInternalEndpoints:
    @pytest.mark.asyncio
    async def test_agent_register_without_key_401(self, fake_request):
        from main import AgentRegistration, register_agent

        reg = AgentRegistration(
            config_name="scholar", pod_ip="10.0.0.1", hostname="agent-1"
        )
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await register_agent(fake_request, reg)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_agent_heartbeat_without_key_401(self, fake_request):
        from main import AgentHeartbeat, agent_heartbeat

        hb = AgentHeartbeat(status="ready", current_job_id=None, metrics=None)
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await agent_heartbeat(fake_request, "agent-1", hb)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_complete_job_without_key_401(self, fake_request, job_a):
        from main import JobCompleteRequest, complete_job

        body = JobCompleteRequest(
            should_stop=True, goal_achieved=False, error=None, freeze_data=None
        )
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await complete_job(fake_request, str(job_a["id"]), body)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_subjob_merge_without_key_401(self, fake_request, job_a):
        from main import subjob_merge

        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await subjob_merge(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_agent_release_job_without_key_401(self, fake_request, job_a):
        from main import agent_release_job

        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await agent_release_job(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 401


# =============================================================================
# Endpoint integration — dual-callable endpoints
# =============================================================================


class TestDualCallableEndpoints:
    @pytest.mark.asyncio
    async def test_cancel_job_internal_bypasses_user_auth(
        self, fake_request, job_a, fake_db
    ):
        """Agent path: valid X-Internal-Key + load job → skip user check."""
        from main import cancel_job

        fake_request.headers = {"X-Internal-Key": "secret"}
        # Job_a status is "created" — cancel handler will reach the next
        # check (status != processing) and not error out early.
        fake_db.cancel_job = AsyncMock(return_value=True)
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main.postgres_db", fake_db),
        ):
            # We patch require_approved_user to explode — if the gate ran
            # the user path, this would fire. It must not.
            with patch(
                "security.access.require_approved_user",
                AsyncMock(side_effect=AssertionError("user auth ran for agent call")),
            ):
                # The handler may still fail after the gate (DB shape) — what
                # matters is that the AssertionError never fires.
                try:
                    await cancel_job(fake_request, str(job_a["id"]))
                except AssertionError:
                    raise
                except Exception:
                    pass  # any other error past the gate is fine for this test

    @pytest.mark.asyncio
    async def test_pause_job_user_path_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        """Cockpit path: no key, cross-user → 403 from require_job_access."""
        from main import pause_job

        fake_request.headers = {}
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            _patch_caller_and_db(user_b, fake_db),
        ):
            with pytest.raises(HTTPException) as exc:
                await pause_job(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_create_job_internal_bypasses_user_auth(self, fake_request, fake_db):
        """Agent delegation path: valid key skips approved-user + force-id
        check. Body's user_id should be respected (the agent supplies it
        from the parent job's context)."""
        from main import JobCreate, create_job

        fake_request.headers = {"X-Internal-Key": "secret"}
        body = JobCreate(
            description="delegation child",
            config_name="scholar",
            user_id="11111111-1111-1111-1111-111111111111",
        )
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main.postgres_db", fake_db),
            patch(
                "main.require_approved_user",
                AsyncMock(side_effect=AssertionError("user auth ran")),
            ),
            patch("main._enforce_readiness_gate", AsyncMock(return_value=None)),
        ):
            # Handler may still fail past the gate (project lookup, etc.) —
            # the only assertion is the user-auth dud didn't fire.
            try:
                await create_job(fake_request, body)
            except AssertionError:
                raise
            except Exception:
                pass
        # body.user_id is NOT force-overwritten on the internal path.
        assert body.user_id == "11111111-1111-1111-1111-111111111111"

    @pytest.mark.asyncio
    async def test_create_job_user_path_forces_user_id(
        self, user_a, fake_db, fake_request
    ):
        """Cockpit path: body.user_id is overwritten with caller.id (F2 pattern).
        A malicious body trying to attribute the job to user_b is sanitized."""
        from main import JobCreate, create_job

        fake_request.headers = {}
        body = JobCreate(
            description="hijack attempt",
            config_name="scholar",
            user_id="22222222-2222-2222-2222-222222222222",  # not user_a
        )
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            _patch_caller_and_db(user_a, fake_db),
            patch("main._enforce_readiness_gate", AsyncMock(return_value=None)),
        ):
            try:
                await create_job(fake_request, body)
            except Exception:
                pass
        # Body's user_id must now match the caller (user_a).
        assert str(body.user_id) == str(user_a["id"])
