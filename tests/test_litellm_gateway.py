"""Unit tests for the LiteLLM gateway catalog-sync (orchestrator side).

Covers the pure reconcile logic + the catalog→desired-model mapping without a
live gateway: a mock postgres_db supplies endpoint models, a mock LiteLLMClient
records the admin calls. See docs/features/usage_monitoring_and_rate_limiting.md.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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
    _parse_quota_policy,
    _parse_rate_policy,
    _parse_spend_ts,
    _project_quota,
    _rev_for_endpoint,
    _srw_id_from,
    _team_id_for_project,
    _team_limits_for_project,
    _user_limits,
    build_desired_models,
    compute_fleet_key,
    compute_project_quota_status,
    compute_scoped_key,
    ensure_fleet_key,
    ensure_scoped_key,
    get_fleet_key,
    materialize_llm_usage,
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


# --------------------------------------------------------------------------
# Slice 3 — longer-window quota (read path + policy)
# --------------------------------------------------------------------------


class TestParseQuotaPolicy:
    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv("LITELLM_QUOTA", raising=False)
        assert _parse_quota_policy() == {}

    def test_valid_json(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_QUOTA", '{"projects": {"default": {"requests_per_day": 5}}}'
        )
        assert _parse_quota_policy() == {
            "projects": {"default": {"requests_per_day": 5}}
        }

    def test_malformed_is_empty(self, monkeypatch):
        monkeypatch.setenv("LITELLM_QUOTA", "nope{")
        assert _parse_quota_policy() == {}

    def test_non_object_is_empty(self, monkeypatch):
        monkeypatch.setenv("LITELLM_QUOTA", "[1, 2]")
        assert _parse_quota_policy() == {}


class TestProjectQuota:
    _P = {
        "projects": {
            "default": {"requests_per_day": 100},
            "p-vip": {"requests_per_day": 10, "tokens_per_day": 5000},
        }
    }

    def test_default_applies(self):
        assert _project_quota(self._P, "p-x") == {"requests_per_day": 100}

    def test_override(self):
        assert _project_quota(self._P, "p-vip") == {
            "requests_per_day": 10,
            "tokens_per_day": 5000,
        }

    def test_zero_means_no_cap(self):
        assert (
            _project_quota({"projects": {"default": {"requests_per_day": 0}}}, "p")
            == {}
        )

    def test_empty_policy(self):
        assert _project_quota({}, "p") == {}


class TestGetTeamDailyUsage:
    @pytest.mark.asyncio
    async def test_sums_successful_requests_and_tokens(self):
        # api_requests includes our own gateway 429s — the quota must count only
        # successful (upstream-hitting) requests, so we sum successful_requests.
        client = gw_mod.LiteLLMClient("http://gw", "sk-master")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "results": [
                    {
                        "metrics": {
                            "successful_requests": 3,
                            "total_tokens": 50,
                            "api_requests": 18,  # includes 429s — must be ignored
                        }
                    },
                    {"metrics": {"successful_requests": 2, "total_tokens": 20}},
                ]
            }
        )
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=resp)
        out = await client.get_team_daily_usage("srw-proj-p1", day="2026-06-22")
        assert out == {"requests": 5, "tokens": 70}
        # queried the non-enterprise activity endpoint, scoped to the team + day.
        # NOTE: the filter param is team_ids (plural) — the singular team_id is
        # silently ignored and returns the global all-team total (verified live).
        _, kwargs = client._client.get.call_args
        assert kwargs["params"] == {
            "team_ids": "srw-proj-p1",
            "start_date": "2026-06-22",
            "end_date": "2026-06-22",
        }
        await client.aclose()

    @pytest.mark.asyncio
    async def test_empty_results_is_zero(self):
        client = gw_mod.LiteLLMClient("http://gw", "sk-master")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"results": []})
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=resp)
        assert await client.get_team_daily_usage("t", day="d") == {
            "requests": 0,
            "tokens": 0,
        }
        await client.aclose()


class TestComputeProjectQuotaStatus:
    @pytest.mark.asyncio
    async def test_no_policy_returns_empty(self, monkeypatch):
        monkeypatch.delenv("LITELLM_QUOTA", raising=False)
        client = AsyncMock()
        assert await compute_project_quota_status(client, ["p1"], day="d") == {}
        client.get_team_daily_usage.assert_not_awaited()  # no reads when inert

    @pytest.mark.asyncio
    async def test_over_when_requests_exceed(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_QUOTA", '{"projects": {"default": {"requests_per_day": 10}}}'
        )
        client = AsyncMock()
        client.get_team_daily_usage.return_value = {"requests": 12, "tokens": 0}
        out = await compute_project_quota_status(client, ["p1"], day="2026-06-22")
        assert out["p1"]["over"] is True
        client.get_team_daily_usage.assert_awaited_once_with(
            "srw-proj-p1", day="2026-06-22"
        )

    @pytest.mark.asyncio
    async def test_under_when_below(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_QUOTA", '{"projects": {"default": {"requests_per_day": 10}}}'
        )
        client = AsyncMock()
        client.get_team_daily_usage.return_value = {"requests": 3, "tokens": 0}
        out = await compute_project_quota_status(client, ["p1"], day="d")
        assert out["p1"]["over"] is False

    @pytest.mark.asyncio
    async def test_token_axis_also_trips(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_QUOTA", '{"projects": {"default": {"tokens_per_day": 100}}}'
        )
        client = AsyncMock()
        client.get_team_daily_usage.return_value = {"requests": 1, "tokens": 200}
        out = await compute_project_quota_status(client, ["p1"], day="d")
        assert out["p1"]["over"] is True

    @pytest.mark.asyncio
    async def test_project_without_quota_omitted(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_QUOTA", '{"projects": {"p1": {"requests_per_day": 5}}}'
        )
        client = AsyncMock()
        client.get_team_daily_usage.return_value = {"requests": 9, "tokens": 0}
        out = await compute_project_quota_status(client, ["p1", "p2"], day="d")
        assert "p1" in out and "p2" not in out  # p2 has no entry + no default

    @pytest.mark.asyncio
    async def test_read_failure_skips_project(self, monkeypatch):
        monkeypatch.setenv(
            "LITELLM_QUOTA", '{"projects": {"default": {"requests_per_day": 5}}}'
        )
        client = AsyncMock()
        client.get_team_daily_usage.side_effect = RuntimeError("gateway down")
        # a read blip must skip, never mass-freeze
        assert await compute_project_quota_status(client, ["p1"], day="d") == {}


class _CaptureLedger:
    """Minimal UsageLedger stand-in: captures events + simulates dedupe."""

    def __init__(self):
        self.events = []
        self.is_available = True
        self._seen = set()

    async def record_events(self, events):
        new = 0
        for e in events:
            key = (e.source, e.source_id, e.unit, e.ts)
            if key in self._seen:
                continue
            self._seen.add(key)
            self.events.append(e)
            new += 1
        return new


class TestSrwIdFrom:
    def test_valid_uuid_extracted(self):
        u = str(uuid4())
        assert _srw_id_from(f"srw-user-{u}", "srw-user-") == u
        assert _srw_id_from(f"srw-proj-{u}", "srw-proj-") == u

    def test_non_uuid_and_unprefixed_rejected(self):
        # Test ids, the fleet key's blank attribution, and raw uuids without our
        # prefix must NOT attribute (they'd poison the ledger's uuid columns).
        assert _srw_id_from("srw-user-ktest-user", "srw-user-") is None
        assert _srw_id_from("default_user_id", "srw-user-") is None
        assert _srw_id_from("e95f0254-5ad3-48ae-8824-6acce14ee3a7", "srw-proj-") is None
        assert _srw_id_from("", "srw-user-") is None
        assert _srw_id_from(None, "srw-user-") is None


class TestParseSpendTs:
    def test_iso_z_parsed_utc(self):
        d = _parse_spend_ts("2026-06-22T06:36:47.993000Z")
        assert d is not None and d.tzinfo is not None

    def test_bad_inputs_none(self):
        assert _parse_spend_ts("not-a-date") is None
        assert _parse_spend_ts(None) is None
        assert _parse_spend_ts("") is None


class TestMaterializeLlmUsage:
    @pytest.mark.asyncio
    async def test_attribution_and_quantities(self):
        uid, pid = str(uuid4()), str(uuid4())
        rows = [
            {  # scoped-key traffic → attributed to the SRW user + project
                "request_id": "r1",
                "model_group": "gemma",
                "prompt_tokens": 17,
                "completion_tokens": 2,
                "startTime": "2026-06-22T06:36:47.993000Z",
                "user": f"srw-user-{uid}",
                "team_id": f"srw-proj-{pid}",
            },
            {  # fleet / unscoped traffic → recorded but unattributed
                "request_id": "r2",
                "model_group": "gemma",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "startTime": "2026-06-22T06:37:00.000000Z",
                "user": "default_user_id",
                "team_id": "",
            },
        ]
        client = AsyncMock()
        client.get_spend_logs.return_value = rows
        ledger = _CaptureLedger()
        res = await materialize_llm_usage(client, ledger)
        assert res["materialized"] == 4  # 2 rows × (prompt + completion)
        by = {(e.source_id, e.unit): e for e in ledger.events}
        assert by[("r1", "prompt-token")].quantity == 17
        assert by[("r1", "prompt-token")].user_id == uid
        assert by[("r1", "prompt-token")].project_id == pid
        assert by[("r1", "completion-token")].quantity == 2
        assert by[("r2", "prompt-token")].user_id is None
        assert by[("r2", "prompt-token")].project_id is None
        assert all(
            e.category == "llm" and e.resource == "gemma" and e.source == "litellm"
            for e in ledger.events
        )

    @pytest.mark.asyncio
    async def test_skips_zero_token_no_model_and_bad_rows(self):
        rows = [
            {
                "request_id": "z",
                "model_group": "g",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "startTime": "2026-06-22T06:36:47Z",
            },
            {
                "request_id": "n",
                "model_group": "",
                "model": "",
                "prompt_tokens": 5,
                "completion_tokens": 5,
                "startTime": "2026-06-22T06:36:47Z",
            },
            {
                "request_id": "",
                "model_group": "g",
                "prompt_tokens": 5,
                "completion_tokens": 0,
                "startTime": "2026-06-22T06:36:47Z",
            },
            {
                "model_group": "g",
                "prompt_tokens": 5,
                "completion_tokens": 0,
                "startTime": "bad-ts",
            },
        ]
        client = AsyncMock()
        client.get_spend_logs.return_value = rows
        ledger = _CaptureLedger()
        res = await materialize_llm_usage(client, ledger)
        assert res["materialized"] == 0
        # A row with only prompt tokens emits one event (not two).
        client.get_spend_logs.return_value = [
            {
                "request_id": "p",
                "model_group": "g",
                "prompt_tokens": 3,
                "completion_tokens": 0,
                "startTime": "2026-06-22T06:36:47Z",
            }
        ]
        assert (await materialize_llm_usage(client, _CaptureLedger()))[
            "materialized"
        ] == 1

    @pytest.mark.asyncio
    async def test_cursor_filters_already_seen(self):
        rows = [
            {
                "request_id": "a",
                "model_group": "g",
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "startTime": "2026-06-22T06:00:00Z",
            },
            {
                "request_id": "b",
                "model_group": "g",
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "startTime": "2026-06-22T07:00:00Z",
            },
        ]
        client = AsyncMock()
        client.get_spend_logs.return_value = rows
        res1 = await materialize_llm_usage(client, _CaptureLedger())
        assert res1["materialized"] == 2
        # A fresh ledger + the cursor → both rows are <= cursor, ledger never sees
        # them (proves the cursor filters, independent of ledger dedupe).
        fresh = _CaptureLedger()
        res2 = await materialize_llm_usage(client, fresh, since=res1["cursor"])
        assert res2["materialized"] == 0
        assert fresh.events == []

    @pytest.mark.asyncio
    async def test_no_ledger_noop(self):
        client = AsyncMock()
        res = await materialize_llm_usage(client, None)
        assert res == {"materialized": 0, "cursor": None, "scanned": 0}
        client.get_spend_logs.assert_not_called()
