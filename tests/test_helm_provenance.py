"""Contracts for the Helm provenance helper and app migrations 0243/0244.

Migration tests follow the repo idiom (read the file text, assert on DDL
shape — see test_experts_migration.py); the runtime behaviour of the columns
is covered by the seeder and admin-API tests.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

import pytest

from orchestrator.database.postgres import PostgresDB
from orchestrator.seed.llm_config import SEEDED_FROM_TAG
from shared.helm_provenance import (
    HELM_SEED_BREADCRUMB,
    MANIFEST_SECTIONS,
    SOURCES,
    empty_manifest,
    is_managed,
    model_identity,
    provenance_from_breadcrumb,
    value_hash,
)

MIGRATIONS = (
    Path(__file__).resolve().parents[1] / "src/orchestrator/database/migrations/app"
)
ADD = MIGRATIONS / "0243_helm_provenance_columns.sql"
VALIDATE = MIGRATIONS / "0244_validate_helm_provenance_checks.sql"
TABLES = ("system_api_keys", "llm_endpoints", "models", "system_settings")


class TestBreadcrumbRule:
    def test_seed_tag_is_the_shared_constant(self):
        assert SEEDED_FROM_TAG == HELM_SEED_BREADCRUMB

    def test_helm_seed_breadcrumb_means_helm(self):
        assert provenance_from_breadcrumb("helm:llm.seed") == "helm"
        assert provenance_from_breadcrumb("helm:llm.seed.apiKeys[openai]") == "helm"

    def test_other_breadcrumbs_mean_image_default(self):
        for crumb in (
            "env:TAVILY_API_KEY",
            "helm:searxng",
            "helm:crawl4ai",
            "helm:openrouter-defaults",
            "migration:user_llm_endpoint_models",
        ):
            assert provenance_from_breadcrumb(crumb) == "default", crumb

    def test_no_breadcrumb_means_ui(self):
        assert provenance_from_breadcrumb(None) == "ui"
        assert provenance_from_breadcrumb("") == "ui"

    def test_every_derived_value_is_a_valid_source(self):
        for crumb in (None, "helm:llm.seed", "env:X"):
            assert provenance_from_breadcrumb(crumb) in SOURCES


class TestValueHash:
    def test_key_order_does_not_matter(self):
        assert value_hash({"a": 1, "b": [1, 2]}) == value_hash({"b": [1, 2], "a": 1})

    def test_different_values_differ(self):
        assert value_hash({"base_url": "http://a"}) != value_hash(
            {"base_url": "http://b"}
        )

    def test_scalars_hash(self):
        assert len(value_hash("sk-abc")) == 64


class TestManifest:
    def test_sections_and_identity_shape(self):
        assert set(empty_manifest()) == set(MANIFEST_SECTIONS)
        assert model_identity("system", "openai", "gpt-5-mini") == "openai/gpt-5-mini"
        assert (
            model_identity("endpoint", "MiniMax", "MiniMax-M3")
            == "endpoint:MiniMax/MiniMax-M3"
        )

    def test_is_managed(self):
        manifest = {"systemApiKeys": ["openai"], "defaults": ["chat"]}
        assert is_managed(manifest, "systemApiKeys", "openai")
        assert not is_managed(manifest, "systemApiKeys", "anthropic")
        assert not is_managed(manifest, "models", "openai/gpt-5-mini")
        assert not is_managed(None, "defaults", "chat")
        assert not is_managed({"defaults": "chat"}, "defaults", "chat")


class TestMigrationShape:
    def test_files_exist(self):
        assert ADD.is_file()
        assert VALIDATE.is_file()

    def test_columns_added_to_every_provenance_table(self):
        sql = ADD.read_text()
        for table in TABLES:
            assert f"ALTER TABLE {table}" in sql, table
            assert f"ADD CONSTRAINT {table}_source_check" in sql, table
        assert sql.count(
            "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ui'"
        ) == len(TABLES)
        assert sql.count("ADD COLUMN IF NOT EXISTS helm_value_hash TEXT") == len(TABLES)
        assert sql.count(
            "ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ) == len(TABLES)

    def test_checks_are_not_valid_then_validated(self):
        sql = ADD.read_text()
        assert sql.count(
            "CHECK (source IN ('default', 'helm', 'ui')) NOT VALID"
        ) == len(TABLES)
        validate = VALIDATE.read_text()
        for table in TABLES:
            assert (
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {table}_source_check;"
                in validate
            )

    def test_backfill_mirrors_the_breadcrumb_rule(self):
        sql = ADD.read_text()
        # seeded_from tables: helm tag -> helm, other breadcrumb -> default
        assert sql.count("WHEN seeded_from LIKE 'helm:llm.seed%' THEN 'helm'") == 2
        assert sql.count("WHEN seeded_from IS NOT NULL THEN 'default'") == 2
        # llm_endpoints has no breadcrumb: derive from its catalog rows, and
        # from the transport marker for the subscription proxy
        assert "bool_or(seeded_from LIKE 'helm:llm.seed%') THEN 'helm'" in sql
        assert "bool_or(seeded_from IS NOT NULL) THEN 'default'" in sql
        assert "transport_kind = 'subscription-proxy'" in sql
        # system_settings only has updated_by
        assert "WHEN updated_by LIKE 'helm:%' THEN 'helm'" in sql
        assert "WHEN updated_by IS NULL THEN 'default'" in sql


# ---------------------------------------------------------------------------
# Placeholder/argument parity for the write paths that gained provenance
# columns. Every other test mocks these methods, which is exactly how a
# "server expects 14 arguments, 12 were passed" reached a live database.
# ---------------------------------------------------------------------------


class _RecordingConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "provider": "openai",
            "label": "x",
            "base_url": "http://x",
            "key": "k",
            "value": "{}",
            "model_id": "m",
            "capabilities": ["chat"],
            "provider_kind": "system",
            "provider_ref": "openai",
            "display_label": "m",
            "family": "f",
            "params_json": None,
        }

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


def _db_with_recording_conn(monkeypatch):
    db = PostgresDB.__new__(PostgresDB)
    conn = _RecordingConn()

    @contextlib.asynccontextmanager
    async def _acquire():
        yield conn

    monkeypatch.setattr(db, "acquire", _acquire, raising=False)
    monkeypatch.setattr(
        "orchestrator.database.postgres.encrypt", lambda v: f"v1:{v}", raising=False
    )
    monkeypatch.setattr(
        "orchestrator.database.postgres._encrypt_optional",
        lambda v: None if v is None else f"v1:{v}",
        raising=False,
    )
    return db, conn


def _placeholder_count(sql: str) -> int:
    return max((int(n) for n in re.findall(r"\$(\d+)", sql)), default=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.upsert_system_api_key(
            provider="openai",
            api_key="sk-x",
            key_prefix="sk-x",
            seeded_from="helm:llm.seed",
            helm_value_hash="h",
        ),
        lambda db: db.create_system_llm_endpoint(
            label="E",
            base_url="http://e",
            api_key="k",
            key_prefix="k",
            source="helm",
            helm_value_hash="h",
        ),
        lambda db: db.create_model(
            provider_kind="system",
            provider_ref="openai",
            model_id="m",
            display_label="m",
            capabilities=["chat"],
            family="f",
            seeded_from="helm:llm.seed",
            on_conflict_do_nothing=True,
            source="helm",
            helm_value_hash="h",
        ),
        lambda db: db.upsert_system_setting(
            "helm.reconcile",
            {"a": 1},
            updated_by="helm:llm.seed",
            source="helm",
            helm_value_hash="h",
        ),
        lambda db: db.update_system_llm_endpoint(
            endpoint_id="00000000-0000-0000-0000-000000000001",
            base_url="http://n",
            api_key="k2",
            key_prefix="k2",
            source="helm",
            helm_value_hash="h2",
        ),
        lambda db: db.update_model(
            "00000000-0000-0000-0000-000000000001",
            display_label="n",
            source="ui",
            helm_value_hash="h3",
        ),
    ],
    ids=[
        "upsert_key",
        "create_endpoint",
        "create_model",
        "upsert_setting",
        "update_endpoint",
        "update_model",
    ],
)
async def test_write_paths_bind_every_placeholder(monkeypatch, call):
    db, conn = _db_with_recording_conn(monkeypatch)
    await call(db)
    assert conn.calls, "no SQL was issued"
    for sql, args in conn.calls:
        assert _placeholder_count(sql) == len(args), (
            f"{_placeholder_count(sql)} placeholders vs {len(args)} args in:\n{sql}"
        )
        assert "source" in sql and "helm_value_hash" in sql
