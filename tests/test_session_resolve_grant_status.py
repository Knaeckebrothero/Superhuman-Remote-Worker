"""_resolve_session_config records grant violations for the drift collector."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import GrantDenied, _resolve_session_config


THREAD = {"id": "11111111-1111-4111-8111-111111111111", "user_id": "u1"}


@pytest.mark.asyncio
async def test_grant_denied_records_violations_in_status():
    violations = ["shell_tools: tools.shell requires the shell_tools grant"]
    status: dict = {}

    with (
        patch("orchestrator.main._is_experts_db_enabled", return_value=True),
        patch("orchestrator.main._user_experts_enabled", AsyncMock(return_value=True)),
        # Real collaborators hit postgres_db for account-default / skills
        # lookups that are irrelevant to this test (it only cares about the
        # except-block wiring below the grant call). Stub them so the resolve
        # reaches _enforce_dispatch_grants without needing a live DB.
        patch(
            "orchestrator.main._resolve_session_account_defaults",
            AsyncMock(return_value={}),
        ),
        patch(
            "orchestrator.main._gather_in_scope_skills",
            AsyncMock(return_value={}),
        ),
        patch(
            "orchestrator.main._enforce_dispatch_grants",
            AsyncMock(side_effect=GrantDenied(violations)),
        ),
        patch("orchestrator.main.postgres_db"),
    ):
        with pytest.raises(GrantDenied):
            await _resolve_session_config(THREAD, {}, status=status)

    assert status["state"] == "denied"
    assert status["grant_violations"] == violations
