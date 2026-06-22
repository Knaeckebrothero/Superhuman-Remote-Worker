"""Unit tests for the LiteLLM gateway catalog-sync (orchestrator side).

Covers the pure reconcile logic + the catalog→desired-model mapping without a
live gateway: a mock postgres_db supplies endpoint models, a mock LiteLLMClient
records the admin calls. See docs/features/usage_monitoring_and_rate_limiting.md.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import orchestrator.services.litellm_gateway as gw_mod
from orchestrator.services.litellm_gateway import (
    FLEET_KEY_ALIAS,
    OWNED_ID_PREFIX,
    _build_fleet_limits,
    _category_models,
    _internal_user_id_for,
    _needs_replace,
    _parse_backstop,
    _parse_rate_policy,
    _rev_for_endpoint,
    _team_id_for_project,
    _team_limits_for_project,
    _user_limits,
    build_desired_models,
    compute_fleet_key,
    compute_scoped_key,
    ensure_fleet_key,
    ensure_scoped_key,
    get_fleet_key,
    sync_catalog_to_gateway,
)

_EP_ID = "f475b8e1-6839-4e54-a366-f1dfa692e4d4"
_UPDATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_REV = int(_UPDATED.timestamp())


def _endpoint(base_url="https://ai.h4ll.app/v1", api_key="sk-home", updated=_UPDATED):
    return {
        "id": _EP_ID,
        "base_url": base_url,
        "api_key": api_key,
        "updated_at": updated,
    }


def _model_row(catalog_id, model_id, endpoint_id=_EP_ID):
    return {"id": catalog_id, "provider_ref": endpoint_id, "model_id": model_id}


def _mock_db(model_rows, endpoint=None):
    db = AsyncMock()
    db.list_models.return_value = model_rows
    db.get_user_llm_endpoint.return_value = (
        endpoint if endpoint is not None else _endpoint()
    )
    return db


class TestRevForEndpoint:
    def test_uses_updated_at_timestamp(self):
        assert _rev_for_endpoint(_endpoint()) == _REV

    def test_missing_updated_at_is_zero(self):
        assert _rev_for_endpoint({"base_url": "x"}) == 0

    def test_bad_updated_at_is_zero(self):
        assert _rev_for_endpoint({"updated_at": "not-a-datetime"}) == 0


class TestBuildDesiredModels:
    @pytest.mark.asyncio
    async def test_endpoint_model_maps_to_openai_deployment(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        desired = await build_desired_models(db)

        # Slice 1 only asks for endpoint-kind, enabled models.
        db.list_models.assert_awaited_once_with(
            provider_kind="endpoint", enabled_only=True
        )
        assert list(desired) == [f"{OWNED_ID_PREFIX}cat-1"]
        spec = desired[f"{OWNED_ID_PREFIX}cat-1"]
        assert spec["model_name"] == "gemma-4-moe-strix"
        # openai/ prefix → OpenAI-compatible upstream; model_name stays the bare id.
        assert spec["litellm_params"]["model"] == "openai/gemma-4-moe-strix"
        assert spec["litellm_params"]["api_base"] == "https://ai.h4ll.app/v1"
        assert spec["litellm_params"]["api_key"] == "sk-home"
        assert spec["model_info"]["id"] == f"{OWNED_ID_PREFIX}cat-1"
        assert spec["model_info"]["srw_rev"] == _REV
        assert spec["model_info"]["srw_managed"] is True

    @pytest.mark.asyncio
    async def test_endpoint_resolved_once_for_many_models(self):
        db = _mock_db(
            [
                _model_row("cat-1", "gemma-4-moe-strix"),
                _model_row("cat-2", "qwen3-embedding-8b-strix"),
                _model_row("cat-3", "whisper-large-v3-strix"),
            ]
        )
        desired = await build_desired_models(db)
        assert len(desired) == 3
        # All share one endpoint → resolved + decrypted exactly once.
        db.get_user_llm_endpoint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keyless_endpoint_omits_api_key(self):
        db = _mock_db(
            [_model_row("cat-1", "local-model")], endpoint=_endpoint(api_key=None)
        )
        desired = await build_desired_models(db)
        assert "api_key" not in desired[f"{OWNED_ID_PREFIX}cat-1"]["litellm_params"]

    @pytest.mark.asyncio
    async def test_endpoint_without_base_url_skipped(self):
        db = _mock_db(
            [_model_row("cat-1", "broken")], endpoint=_endpoint(base_url=None)
        )
        assert await build_desired_models(db) == {}

    @pytest.mark.asyncio
    async def test_no_models_returns_empty(self):
        db = _mock_db([])
        assert await build_desired_models(db) == {}


class TestNeedsReplace:
    def _spec(self, model="openai/m", api_base="https://a/v1", rev=_REV):
        return {
            "litellm_params": {"model": model, "api_base": api_base},
            "model_info": {"srw_rev": rev},
        }

    def test_identical_no_replace(self):
        assert _needs_replace(self._spec(), self._spec()) is False

    def test_api_base_change_triggers_replace(self):
        assert (
            _needs_replace(self._spec(api_base="https://old/v1"), self._spec()) is True
        )

    def test_model_change_triggers_replace(self):
        assert _needs_replace(self._spec(model="openai/old"), self._spec()) is True

    def test_rev_change_triggers_replace(self):
        # Key rotation bumps updated_at → rev → replace, even though the masked
        # api_key never surfaces on read-back.
        assert _needs_replace(self._spec(rev=1), self._spec(rev=2)) is True

    def test_missing_current_rev_does_not_thrash(self):
        current = {"litellm_params": {"model": "openai/m", "api_base": "https://a/v1"}}
        assert _needs_replace(current, self._spec()) is False


class TestSyncCatalogToGateway:
    @pytest.mark.asyncio
    async def test_adds_missing_models(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        client = AsyncMock()
        client.list_managed_models.return_value = {}

        counts = await sync_catalog_to_gateway(db, client)

        client.add_model.assert_awaited_once()
        client.delete_model.assert_not_awaited()
        assert counts == {"added": 1, "replaced": 0, "deleted": 0, "managed": 1}

    @pytest.mark.asyncio
    async def test_deletes_stale_owned_models(self):
        db = _mock_db([])  # catalog now empty
        client = AsyncMock()
        client.list_managed_models.return_value = {
            f"{OWNED_ID_PREFIX}gone": {
                "model_name": "old",
                "litellm_params": {},
                "model_info": {"id": f"{OWNED_ID_PREFIX}gone"},
            }
        }

        counts = await sync_catalog_to_gateway(db, client)

        client.delete_model.assert_awaited_once_with(f"{OWNED_ID_PREFIX}gone")
        client.add_model.assert_not_awaited()
        assert counts["deleted"] == 1

    @pytest.mark.asyncio
    async def test_replaces_drifted_model(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        client = AsyncMock()
        # Same id present but pointing at a stale base_url → delete + re-add.
        client.list_managed_models.return_value = {
            f"{OWNED_ID_PREFIX}cat-1": {
                "model_name": "gemma-4-moe-strix",
                "litellm_params": {
                    "model": "openai/gemma-4-moe-strix",
                    "api_base": "https://OLD.example/v1",
                },
                "model_info": {"id": f"{OWNED_ID_PREFIX}cat-1", "srw_rev": _REV},
            }
        }

        counts = await sync_catalog_to_gateway(db, client)

        client.delete_model.assert_awaited_once_with(f"{OWNED_ID_PREFIX}cat-1")
        client.add_model.assert_awaited_once()
        assert counts == {"added": 0, "replaced": 1, "deleted": 0, "managed": 1}

    @pytest.mark.asyncio
    async def test_noop_when_in_sync(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        client = AsyncMock()
        client.list_managed_models.return_value = {
            f"{OWNED_ID_PREFIX}cat-1": {
                "model_name": "gemma-4-moe-strix",
                "litellm_params": {
                    "model": "openai/gemma-4-moe-strix",
                    "api_base": "https://ai.h4ll.app/v1",
                },
                "model_info": {"id": f"{OWNED_ID_PREFIX}cat-1", "srw_rev": _REV},
            }
        }

        counts = await sync_catalog_to_gateway(db, client)

        client.add_model.assert_not_awaited()
        client.delete_model.assert_not_awaited()
        assert counts == {"added": 0, "replaced": 0, "deleted": 0, "managed": 1}

    @pytest.mark.asyncio
    async def test_leaves_unowned_models_untouched(self):
        db = _mock_db([])
        client = AsyncMock()
        # A model an operator added by hand (no srw- prefix) — list_managed_models
        # filters it out, so the reconcile never sees or deletes it.
        client.list_managed_models.return_value = {}

        counts = await sync_catalog_to_gateway(db, client)

        client.delete_model.assert_not_awaited()
        assert counts["deleted"] == 0


class TestComputeFleetKey:
    def test_deterministic(self):
        assert compute_fleet_key("sk-master") == compute_fleet_key("sk-master")

    def test_depends_on_master_key(self):
        assert compute_fleet_key("sk-a") != compute_fleet_key("sk-b")

    def test_prefix(self):
        assert compute_fleet_key("sk-x").startswith("sk-srw-fleet-")


class TestParseBackstop:
    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv("LITELLM_BACKSTOP", raising=False)
        assert _parse_backstop() == {}

    def test_valid_json(self, monkeypatch):
        monkeypatch.setenv("LITELLM_BACKSTOP", '{"gemma": {"rpm": 5}}')
        assert _parse_backstop() == {"gemma": {"rpm": 5}}

    def test_malformed_is_empty(self, monkeypatch):
        monkeypatch.setenv("LITELLM_BACKSTOP", "not-json{")
        assert _parse_backstop() == {}

    def test_non_object_is_empty(self, monkeypatch):
        monkeypatch.setenv("LITELLM_BACKSTOP", "[1, 2]")
        assert _parse_backstop() == {}


class TestBuildFleetLimits:
    def test_per_model_rpm(self):
        assert _build_fleet_limits(["a", "b"], {"a": {"rpm": 5}}) == {
            "model_rpm_limit": {"a": 5}
        }

    def test_wildcard_default_fans_out(self):
        # '*' is the category→model_names expansion in miniature.
        out = _build_fleet_limits(["a", "b"], {"*": {"rpm": 10}, "a": {"rpm": 5}})
        assert out["model_rpm_limit"] == {"a": 5, "b": 10}

    def test_tpm(self):
        assert _build_fleet_limits(["a"], {"a": {"tpm": 100}}) == {
            "model_tpm_limit": {"a": 100}
        }

    def test_no_config_is_empty(self):
        assert _build_fleet_limits(["a"], {}) == {}


class TestEnsureFleetKey:
    @pytest.fixture(autouse=True)
    def _reset_globals(self, monkeypatch):
        monkeypatch.delenv("LITELLM_BACKSTOP", raising=False)
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master")
        gw_mod._fleet_key_ready = False
        gw_mod._fleet_spec_hash = None
        yield
        gw_mod._fleet_key_ready = False
        gw_mod._fleet_spec_hash = None

    @pytest.mark.asyncio
    async def test_mints_scoped_key_and_marks_ready(self, monkeypatch):
        monkeypatch.setenv("LITELLM_BACKSTOP", '{"gemma-4-moe-strix": {"rpm": 5}}')
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        assert get_fleet_key() is None  # gated until ensure runs

        await ensure_fleet_key(client, db, "sk-master")

        client.upsert_key.assert_awaited_once()
        _, kwargs = client.upsert_key.call_args
        assert kwargs["alias"] == FLEET_KEY_ALIAS
        assert kwargs["spec"]["models"] == ["gemma-4-moe-strix"]
        assert kwargs["spec"]["model_rpm_limit"] == {"gemma-4-moe-strix": 5}
        # routing can now switch agents onto it
        assert get_fleet_key() == compute_fleet_key("sk-master")

    @pytest.mark.asyncio
    async def test_idempotent_when_unchanged(self):
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        await ensure_fleet_key(client, db, "sk-master")
        await ensure_fleet_key(client, db, "sk-master")
        client.upsert_key.assert_awaited_once()  # second call is a no-op

    @pytest.mark.asyncio
    async def test_rewrites_when_limits_change(self, monkeypatch):
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        await ensure_fleet_key(client, db, "sk-master")
        monkeypatch.setenv("LITELLM_BACKSTOP", '{"gemma-4-moe-strix": {"rpm": 9}}')
        await ensure_fleet_key(client, db, "sk-master")
        assert client.upsert_key.await_count == 2

    @pytest.mark.asyncio
    async def test_no_models_skips(self):
        db = _mock_db([])
        client = AsyncMock()
        await ensure_fleet_key(client, db, "sk-master")
        client.upsert_key.assert_not_awaited()
        assert get_fleet_key() is None

    @pytest.mark.asyncio
    async def test_no_backstop_still_sends_empty_limit_dicts(self):
        # No LITELLM_BACKSTOP → the fleet key still mints (agents off the master
        # key), and the empty limit dicts must be present so a *later* removal of
        # a backstop propagates on /key/update (omitted fields keep old values).
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        await ensure_fleet_key(client, db, "sk-master")
        _, kwargs = client.upsert_key.call_args
        assert kwargs["spec"]["model_rpm_limit"] == {}
        assert kwargs["spec"]["model_tpm_limit"] == {}


# --------------------------------------------------------------------------
# Slice 2b — per-user / per-project scoped keys
# --------------------------------------------------------------------------


class TestComputeScopedKey:
    def test_deterministic(self):
        assert compute_scoped_key("sk-master", "u1", "p1") == compute_scoped_key(
            "sk-master", "u1", "p1"
        )

    def test_depends_on_master_key(self):
        assert compute_scoped_key("sk-a", "u1", "p1") != compute_scoped_key(
            "sk-b", "u1", "p1"
        )

    def test_depends_on_user_and_project(self):
        base = compute_scoped_key("sk-master", "u1", "p1")
        assert compute_scoped_key("sk-master", "u2", "p1") != base
        assert compute_scoped_key("sk-master", "u1", "p2") != base

    def test_prefix_distinct_from_fleet(self):
        k = compute_scoped_key("sk-x", "u1", "p1")
        assert k.startswith("sk-srw-")
        # must NOT collide with the fleet key's namespace
        assert not k.startswith("sk-srw-fleet-")


class TestScopedIdHelpers:
    def test_team_id(self):
        assert _team_id_for_project("p1") == "srw-proj-p1"

    def test_internal_user_id(self):
        assert _internal_user_id_for("u1") == "srw-user-u1"


class TestParseRatePolicy:
    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv("LITELLM_RATE_POLICY", raising=False)
        assert _parse_rate_policy() == {}

    def test_valid_json(self, monkeypatch):
        monkeypatch.setenv("LITELLM_RATE_POLICY", '{"users": {"default": {"rpm": 5}}}')
        assert _parse_rate_policy() == {"users": {"default": {"rpm": 5}}}

    def test_malformed_is_empty(self, monkeypatch):
        monkeypatch.setenv("LITELLM_RATE_POLICY", "nope{")
        assert _parse_rate_policy() == {}

    def test_non_object_is_empty(self, monkeypatch):
        monkeypatch.setenv("LITELLM_RATE_POLICY", "[1, 2]")
        assert _parse_rate_policy() == {}


class TestCategoryModels:
    _POLICY = {"categories": {"large": ["a", "b"], "small": ["c"]}}

    def test_named_category_filters_to_registered(self):
        assert _category_models(self._POLICY, "large", ["a", "b", "c"]) == ["a", "b"]

    def test_wildcard_returns_all_registered(self):
        assert _category_models(self._POLICY, "*", ["a", "b"]) == ["a", "b"]

    def test_unknown_model_skipped_with_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            out = _category_models(
                {"categories": {"large": ["a", "ghost"]}}, "large", ["a"]
            )
        # the validation guard drops the unregistered model + surfaces it (gap 1:
        # LiteLLM would silently skip a mismatched model_name → unthrottled)
        assert out == ["a"]
        assert "ghost" in caplog.text

    def test_unknown_category_is_empty(self):
        assert _category_models(self._POLICY, "missing", ["a"]) == []


class TestTeamLimitsForProject:
    _POLICY = {
        "categories": {"large": ["gemma-4-moe-strix"]},
        "projects": {
            "default": {"large": {"rpm": 30}},
            "p-special": {"*": {"rpm": 10, "tpm": 1000}},
        },
    }

    def test_default_applies_when_no_override(self):
        out = _team_limits_for_project(self._POLICY, "p-unknown", ["gemma-4-moe-strix"])
        assert out == {"model_rpm_limit": {"gemma-4-moe-strix": 30}}

    def test_per_project_override_and_wildcard(self):
        out = _team_limits_for_project(
            self._POLICY, "p-special", ["gemma-4-moe-strix", "other"]
        )
        assert out["model_rpm_limit"] == {"gemma-4-moe-strix": 10, "other": 10}
        assert out["model_tpm_limit"] == {"gemma-4-moe-strix": 1000, "other": 1000}

    def test_empty_policy_is_empty(self):
        assert _team_limits_for_project({}, "p1", ["a"]) == {}


class TestUserLimits:
    _POLICY = {"users": {"default": {"rpm": 120}, "u-vip": {"rpm": 500, "tpm": 9999}}}

    def test_default(self):
        assert _user_limits(self._POLICY, "u-x") == {"rpm_limit": 120}

    def test_override(self):
        assert _user_limits(self._POLICY, "u-vip") == {
            "rpm_limit": 500,
            "tpm_limit": 9999,
        }

    def test_empty(self):
        assert _user_limits({}, "u1") == {}


class TestEnsureScopedKey:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.delenv("LITELLM_RATE_POLICY", raising=False)
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master")
        gw_mod._scoped_ensured.clear()
        yield
        gw_mod._scoped_ensured.clear()

    @pytest.mark.asyncio
    async def test_none_without_user_or_project(self):
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        assert (
            await ensure_scoped_key(
                client, db, "sk-master", user_id=None, project_id="p1"
            )
            is None
        )
        assert (
            await ensure_scoped_key(
                client, db, "sk-master", user_id="u1", project_id=None
            )
            is None
        )
        client.upsert_team.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_models_returns_none(self):
        db = _mock_db([])
        client = AsyncMock()
        assert (
            await ensure_scoped_key(
                client, db, "sk-master", user_id="u1", project_id="p1"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_upserts_team_user_key_and_returns_key(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_RATE_POLICY",
            '{"categories": {"large": ["gemma-4-moe-strix"]}, '
            '"projects": {"default": {"large": {"rpm": 7}}}, '
            '"users": {"default": {"rpm": 50}}}',
        )
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()

        key = await ensure_scoped_key(
            client, db, "sk-master", user_id="u1", project_id="p1"
        )
        assert key == compute_scoped_key("sk-master", "u1", "p1")

        # team carries the per-model project limit (aggregates across the project)
        _, t_kwargs = client.upsert_team.call_args
        assert t_kwargs["spec"]["model_rpm_limit"] == {"gemma-4-moe-strix": 7}
        # internal user carries the flat per-user limit
        _, u_kwargs = client.upsert_internal_user.call_args
        assert u_kwargs["spec"]["rpm_limit"] == 50
        # the key binds to both (no limits of its own — the objects enforce)
        _, k_kwargs = client.upsert_scoped_key.call_args
        assert k_kwargs["team_id"] == "srw-proj-p1"
        assert k_kwargs["user_id"] == "srw-user-u1"

    @pytest.mark.asyncio
    async def test_idempotent_when_unchanged(self):
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        await ensure_scoped_key(client, db, "sk-master", user_id="u1", project_id="p1")
        await ensure_scoped_key(client, db, "sk-master", user_id="u1", project_id="p1")
        client.upsert_team.assert_awaited_once()  # hash-gate skips the second

    @pytest.mark.asyncio
    async def test_rewrites_when_policy_changes(self, monkeypatch):
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        await ensure_scoped_key(client, db, "sk-master", user_id="u1", project_id="p1")
        monkeypatch.setenv(
            "LITELLM_RATE_POLICY", '{"users": {"default": {"rpm": 999}}}'
        )
        await ensure_scoped_key(client, db, "sk-master", user_id="u1", project_id="p1")
        assert client.upsert_team.await_count == 2

    @pytest.mark.asyncio
    async def test_no_policy_still_mints_off_master(self):
        # No policy → no per-entity limits, but the scoped key still mints
        # (attribution + off the admin master key); team/user upserted with empty
        # limits so a later policy addition propagates (omitted-field caveat).
        db = _mock_db([_model_row("c1", "gemma-4-moe-strix")])
        client = AsyncMock()
        key = await ensure_scoped_key(
            client, db, "sk-master", user_id="u1", project_id="p1"
        )
        assert key == compute_scoped_key("sk-master", "u1", "p1")
        _, t_kwargs = client.upsert_team.call_args
        assert t_kwargs["spec"]["model_rpm_limit"] == {}
