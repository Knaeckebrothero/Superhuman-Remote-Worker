"""Unit tests for DB-backed expert write-CRUD (restored Slice-1 surface).

These guard the request models + the save-time hard-deny gate + the bundle
mapping. Full endpoint integration is verified live on k3d (see the plan's
Task 10) because the auth dependency + asyncpg store are not hermetically
mockable here. Local env may be noisy (Py3.14, missing optional deps); CI
(Py3.12) is the authoritative gate.
"""

import pytest
from fastapi import HTTPException

from orchestrator.main import (
    ExpertCreate,
    ExpertUpdate,
    _db_expert_to_bundle_src,
    _validate_expert_fragment,
)

# --- T1: request models + save-time hard-deny gate ---


def test_expert_create_rejects_bad_slug():
    with pytest.raises(Exception):
        ExpertCreate(name="Bad Name", display_name="X", expert_type="worker")


def test_expert_create_rejects_bad_type():
    with pytest.raises(Exception):
        ExpertCreate(name="ok", display_name="X", expert_type="orchestrator")


def test_expert_create_rejects_bad_color():
    with pytest.raises(Exception):
        ExpertCreate(name="ok", display_name="X", expert_type="worker", color="red")


def test_expert_create_minimal_ok():
    e = ExpertCreate(name="my-helper", display_name="My Helper", expert_type="session")
    assert e.config == {} and e.prompts == {} and e.color == "#6B7280"
    assert e.icon == "smart_toy"


def test_validate_fragment_blocks_credentials():
    with pytest.raises(HTTPException) as ei:
        _validate_expert_fragment({"llm": {"api_key": "secret"}})
    assert ei.value.status_code == 422


def test_validate_fragment_blocks_connections():
    with pytest.raises(HTTPException) as ei:
        _validate_expert_fragment({"connections": {"db": "x"}})
    assert ei.value.status_code == 422


def test_validate_fragment_allows_clean_config():
    # Should not raise.
    _validate_expert_fragment(
        {"llm": {"model": "gemma-4-moe"}, "tools": {"shell": True}}
    )


# --- T2: update contract (immutable name/type; unset fields dropped) ---


def test_update_excludes_immutable_fields():
    assert "name" not in ExpertUpdate.model_fields
    assert "expert_type" not in ExpertUpdate.model_fields


def test_update_payload_drops_unset_fields():
    body = ExpertUpdate(display_name="New Name")
    assert body.model_dump(exclude_unset=True) == {"display_name": "New Name"}


# --- T3: bundle-source mapping (JSONB str-tolerant) ---


def test_db_row_to_bundle_src_shape():
    row = {
        "name": "scholar",
        "display_name": "Scholar",
        "expert_type": "worker",
        "description": None,
        "icon": "school",
        "color": "#111111",
        "tags": ["research"],
        "config": {"llm": {"model": "x"}},
        "prompts": {"persona": "p"},
    }
    src = _db_expert_to_bundle_src(row)
    assert src["name"] == "scholar"
    assert src["expert_type"] == "worker"
    assert src["config"] == {"llm": {"model": "x"}}
    assert src["prompts"] == {"persona": "p"}


def test_db_row_to_bundle_src_parses_json_strings():
    row = {
        "name": "x",
        "display_name": "X",
        "expert_type": "session",
        "icon": "smart_toy",
        "color": "#6B7280",
        "config": '{"tools": {"shell": false}}',
        "prompts": '{"persona": "hi"}',
    }
    src = _db_expert_to_bundle_src(row)
    assert src["config"] == {"tools": {"shell": False}}
    assert src["prompts"] == {"persona": "hi"}
