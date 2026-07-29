"""Mode-specific virtual framework-default expert details."""

from unittest.mock import AsyncMock

import pytest

import orchestrator.main as orchestrator_main
from orchestrator.main import _load_expert_detail


@pytest.mark.asyncio
async def test_session_defaults_use_persistent_base():
    detail = await _load_expert_detail("defaults", defaults_type="session")

    config = detail["config"]
    assert config["agent_id"] == "session_base"
    assert config["llm"]["max_retries"] == 3
    assert config["tools"]["communication"] == []
    assert config["tools"]["delegation"] == []


@pytest.mark.asyncio
async def test_unspecified_defaults_type_remains_worker_for_compatibility():
    detail = await _load_expert_detail("defaults")

    config = detail["config"]
    assert config["agent_id"] == "worker_base"
    assert config["llm"]["max_retries"] == 0


@pytest.mark.asyncio
async def test_db_expert_detail_includes_base_tools_and_settings_matrix(monkeypatch):
    expert_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
    monkeypatch.setattr(
        orchestrator_main.postgres_db,
        "get_expert_by_id",
        AsyncMock(
            return_value={
                "id": expert_id,
                "display_name": "DB Worker",
                "description": "",
                "icon": "smart_toy",
                "color": "#6B7280",
                "tags": [],
                "expert_type": "worker",
                "is_global": False,
                "managed_key": None,
                "config": {"tools": {"shell": []}},
                "prompts": {},
            }
        ),
    )

    detail = await _load_expert_detail(expert_id)

    assert detail["defaults_tools"]["workspace"]
    assert detail["defaults_tools"]["shell"] == []
    assert "gpt-5.6" in detail["settings_matrix"]


class TestAccountDefaultsLayer:
    """`include_account_defaults` must reproduce `resolve_config`'s precedence.

    The create forms render this config; when it disagrees with what
    create/dispatch resolves, controls keyed off a resolved value are wrong.
    The concrete regression: session detail reported `workspace.backend` as
    session_base's `sandbox` while `create_thread` resolved the account's
    `virtual`, so the New Session datasource picker kept clone-based repository
    connectors selected and every create 400'd on the lite-backend rule.
    """

    @pytest.fixture
    def account_user(self, monkeypatch):
        """A user whose account pins a model but no session workspace tier."""
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(return_value={"default_model": "account-pinned-model"}),
        )
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "resolve_default_for_capability",
            AsyncMock(return_value=None),
        )
        return "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_session_detail_off_by_default_reports_base_backend(self):
        detail = await _load_expert_detail("defaults", defaults_type="session")

        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_session_detail_reports_the_backend_create_will_resolve(
        self, account_user
    ):
        detail = await _load_expert_detail(
            "defaults",
            defaults_type="session",
            user_id=account_user,
            include_account_defaults=True,
        )

        # The account default, not session_base's `sandbox` — this is the value
        # the picker's lite-tier gate has to agree with.
        assert detail["config"]["workspace"]["backend"] == "virtual"
        assert detail["config"]["llm"]["model"] == "account-pinned-model"

    @pytest.mark.asyncio
    async def test_saved_session_tier_preference_wins_over_platform_default(
        self, monkeypatch, account_user
    ):
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(
                return_value={"persistent_agent": {"workspace_backend": "sandbox"}}
            ),
        )

        detail = await _load_expert_detail(
            "defaults",
            defaults_type="session",
            user_id=account_user,
            include_account_defaults=True,
        )

        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_worker_detail_gets_the_model_floor_but_no_session_tier(
        self, account_user
    ):
        detail = await _load_expert_detail(
            "defaults", user_id=account_user, include_account_defaults=True
        )

        assert detail["config"]["llm"]["model"] == "account-pinned-model"
        # Jobs have no account workspace layer at dispatch, so the worker base's
        # own backend must survive — feeding sessions' `virtual` here would make
        # the New Job form lie in the opposite direction.
        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_expert_fragment_still_beats_the_account_layer(
        self, monkeypatch, account_user
    ):
        expert_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_expert_by_id",
            AsyncMock(
                return_value={
                    "id": expert_id,
                    "display_name": "Pinned Session",
                    "description": "",
                    "icon": "smart_toy",
                    "color": "#6B7280",
                    "tags": [],
                    "expert_type": "session",
                    "is_global": False,
                    "managed_key": None,
                    "config": {"workspace": {"backend": "sandbox"}},
                    "prompts": {},
                }
            ),
        )

        detail = await _load_expert_detail(
            expert_id, user_id=account_user, include_account_defaults=True
        )

        # base -> account -> expert: the expert is the most specific layer here.
        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_anonymous_caller_has_no_account_layer(self):
        detail = await _load_expert_detail(
            "defaults", defaults_type="session", include_account_defaults=True
        )

        assert detail["config"]["workspace"]["backend"] == "sandbox"
