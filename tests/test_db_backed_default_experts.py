"""Managed seed and authoritative root-expert selection contracts."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.default_experts import (
    load_seed_bundle,
    resolve_root_expert,
    seed_managed_default_experts,
)
from src.core.loader import canonical_config_name, resolve_config_path


ROOT = Path(__file__).resolve().parents[1]


class SelectionDB:
    def __init__(self):
        self.application = {"id": "app", "expert_type": "worker", "owner_id": None}
        self.personal = {"id": "mine", "expert_type": "worker", "owner_id": "u1"}
        self.project = None
        self.explicit = {"id": "explicit", "expert_type": "worker", "owner_id": "u1"}
        self.link = None
        self.global_grants = []

    async def list_grants_for_scopes(self, *, user_id, project_ids):
        return {"user": [], "project": [], "global": self.global_grants}

    async def get_expert_visible_by_id(self, expert_id, **_kwargs):
        return self.explicit if expert_id == self.explicit["id"] else None

    async def get_project_expert_link(self, **_kwargs):
        return self.link

    async def get_project_default_expert(self, **_kwargs):
        return self.project

    async def get_user_expert_default(self, **_kwargs):
        return self.personal

    async def get_application_expert_default(self, _expert_type):
        return self.application


@pytest.mark.asyncio
async def test_selection_precedence_and_project_override():
    db = SelectionDB()
    db.project = {
        "id": "project",
        "expert_type": "worker",
        "owner_id": "other",
        "config_override": {"llm": {"temperature": 0.2}},
    }
    selected = await resolve_root_expert(
        db, expert_type="worker", user_id="u1", project_id="p1"
    )
    assert selected.source == "project"
    assert selected.expert["id"] == "project"
    assert selected.project_override == {"llm": {"temperature": 0.2}}

    db.link = {"config_override": {"tools": {"shell": []}}}
    explicit = await resolve_root_expert(
        db,
        expert_type="worker",
        user_id="u1",
        project_id="p1",
        explicit_expert_id="explicit",
    )
    assert explicit.source == "explicit"
    assert explicit.project_override == {"tools": {"shell": []}}


@pytest.mark.asyncio
async def test_personal_default_is_dormant_when_grant_is_revoked():
    db = SelectionDB()
    chosen = await resolve_root_expert(db, expert_type="worker", user_id="u1")
    assert chosen.source == "user"

    db.global_grants = [{"key": "personal_default_experts", "value_json": False}]
    chosen = await resolve_root_expert(db, expert_type="worker", user_id="u1")
    assert chosen.source == "application"


def test_legacy_base_names_resolve_to_canonical_files():
    assert canonical_config_name("defaults") == "worker_base"
    assert canonical_config_name("persistent_defaults") == "session_base"
    worker, _ = resolve_config_path("defaults")
    session, _ = resolve_config_path("persistent_defaults")
    assert Path(worker).name == "worker_base.yaml"
    assert Path(session).name == "session_base.yaml"


def test_managed_seed_bundles_are_raw_typed_overlays():
    worker = load_seed_bundle(
        ROOT / "config", directory="general-worker", expert_type="worker"
    )
    session = load_seed_bundle(
        ROOT / "config", directory="assistant", expert_type="session"
    )
    assert worker["name"] == "general-worker"
    assert session["name"] == "assistant"
    assert "$extends" not in worker["config"]
    assert "$extends" not in session["config"]
    assert session["prompts"]["persona"].strip()


class SeedDB:
    def __init__(self):
        self.rows = {}
        self.defaults = {}

    async def upsert_managed_expert(self, *, managed_key, **bundle):
        if managed_key in self.rows:
            return self.rows[managed_key], False
        row = {"id": f"id-{len(self.rows)}", "managed_key": managed_key, **bundle}
        self.rows[managed_key] = row
        return row, True

    async def ensure_application_expert_default(self, *, expert_type, expert_id):
        self.defaults.setdefault(expert_type, expert_id)
        return {"expert_type": expert_type, "expert_id": self.defaults[expert_type]}


@pytest.mark.asyncio
async def test_managed_seed_is_idempotent_and_insert_only():
    db = SeedDB()
    first = await seed_managed_default_experts(db, ROOT / "config")
    db.rows["application-default-session-seed"]["display_name"] = "Operator Assistant"
    second = await seed_managed_default_experts(db, ROOT / "config")
    assert first == second
    assert (
        db.rows["application-default-session-seed"]["display_name"]
        == "Operator Assistant"
    )
    assert set(db.defaults) == {"worker", "session"}


def test_default_expert_migration_shape():
    migration_dir = ROOT / "orchestrator/database/migrations/app"
    sql = "\n".join(
        path.read_text() for path in sorted(migration_dir.glob("006[4-8]_*.sql"))
    )
    assert "application_expert_defaults" in sql
    assert "user_expert_defaults" in sql
    assert "expert_default_audit" in sql
    assert "managed_key" in sql
    assert "FOREIGN KEY (expert_id, expert_type)" in sql
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in sql
    assert "BIGINT GENERATED BY DEFAULT AS IDENTITY" in sql


def test_default_assistant_runtime_control_groups_are_really_off():
    """The resolved policy must reach the runtime gates, not stop at YAML."""
    from orchestrator import main as orchestrator_main
    from orchestrator.services.config_resolver import resolve_config
    from src.api.persistent_session import (
        _agent_catalog_enabled,
        _canvas_enabled,
        _fleet_management_enabled,
        _workflows_enabled,
    )
    from src.core.loader import load_config_from_resolved

    assistant = load_seed_bundle(
        ROOT / "config", directory="assistant", expert_type="session"
    )
    capture: dict = {}
    blob = resolve_config(
        base_config_name="session_base",
        expert_row=assistant,
        expert_type="session",
        capture=capture,
    )
    blob["agent"].update(
        orchestrator_main._session_tool_group_disabled_markers(
            capture["merged_fragment"]
        )
    )
    hydrated = load_config_from_resolved(blob)

    assert _fleet_management_enabled(hydrated) is False
    assert _agent_catalog_enabled(hydrated) is False
    assert _workflows_enabled(hydrated) is False
    assert _canvas_enabled(hydrated) is True


@pytest.mark.asyncio
async def test_account_reasoning_is_a_floor_below_the_expert(monkeypatch):
    from orchestrator import main as orchestrator_main
    from orchestrator.services.config_resolver import resolve_config

    monkeypatch.setattr(
        orchestrator_main.postgres_db,
        "get_user_settings",
        AsyncMock(
            return_value={
                "default_model": "account-model",
                "default_reasoning_level": "low",
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator_main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )
    account_floor = await orchestrator_main._resolve_default_models("user-1")

    assert account_floor["llm"] == {
        "model": "account-model",
        "reasoning_level": "low",
    }

    expert = {
        "expert_type": "session",
        "name": "reasoning-expert",
        "config": {"llm": {"reasoning_level": "high"}},
        "prompts": {},
    }
    resolved = resolve_config(
        base_config_name="session_base",
        base_defaults=account_floor,
        expert_row=expert,
        expert_type="session",
    )
    assert resolved["agent"]["llm"]["model"] == "account-model"
    assert resolved["agent"]["llm"]["reasoning_level"] == "high"
