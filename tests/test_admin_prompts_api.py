"""Tests for the admin prompt-override API (v1).

Mirrors tests/test_admin_providers_api.py: lightweight route-registration +
Pydantic validation, plus a mocked-pool CRUD check. The ``orchestrator.main``
import is heavy; CI (Py3.12) is the gate for the route/model tests.
"""

import os

os.environ.setdefault("VECTOR_DB_URL", "postgresql://localhost/test")

import pytest


@pytest.mark.asyncio
async def test_upsert_prompt_override_uses_on_conflict():
    from unittest.mock import AsyncMock

    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)  # bypass real pool init
    db.fetchrow = AsyncMock(return_value={
        "id": "abc", "family": "gemma", "kind": "prompts",
        "name": "persona", "content": "C",
    })

    row = await db.upsert_prompt_override(
        family="gemma", kind="prompts", name="persona",
        content="C", content_format="text", notes=None, user_id=None,
    )

    sql = db.fetchrow.call_args.args[0]
    assert "INSERT INTO prompt_overrides" in sql
    assert "ON CONFLICT" in sql and "DO UPDATE" in sql
    assert row["content"] == "C"


# ---------------------------------------------------------------------------
# Route registration + model validation (import main; CI-gated — see header).
# main is imported inside the test bodies so this file still collects locally
# where main's heavy deps (aiosmtplib, ...) are absent.
# ---------------------------------------------------------------------------

def _import_main():
    import sys
    from pathlib import Path

    _orch = Path(__file__).parent.parent / "orchestrator"
    if str(_orch) not in sys.path:
        sys.path.insert(0, str(_orch))
    import main  # noqa: E402

    return main


def _registered_routes(app) -> set:
    out = set()
    for route in app.routes:
        for m in (getattr(route, "methods", None) or set()):
            out.add((m, getattr(route, "path", "")))
    return out


OVERRIDE_ROUTES = {
    ("GET", "/api/admin/prompts/overrides"),
    ("POST", "/api/admin/prompts/overrides"),
    ("GET", "/api/admin/prompts/overrides/{override_id}"),
    ("PUT", "/api/admin/prompts/overrides/{override_id}"),
    ("DELETE", "/api/admin/prompts/overrides/{override_id}"),
}


def test_prompt_override_routes_registered():
    registered = _registered_routes(_import_main().app)
    missing = [r for r in OVERRIDE_ROUTES if r not in registered]
    assert not missing, f"missing routes: {missing}"


def test_prompt_override_create_model_validates():
    PromptOverrideCreate = _import_main().PromptOverrideCreate

    ok = PromptOverrideCreate(family="gemma", kind="prompts", name="persona", content="x")
    assert ok.content_format == "text"  # default
    with pytest.raises(Exception):
        PromptOverrideCreate(family=None, kind="bogus", name="persona", content="x")


CATALOG_ROUTES = {
    ("GET", "/api/admin/prompts/catalog"),
    ("GET", "/api/admin/prompts/bundled/{family}/{kind}/{name}"),
}


def test_prompt_catalog_and_bundled_routes_registered():
    registered = _registered_routes(_import_main().app)
    missing = [r for r in CATALOG_ROUTES if r not in registered]
    assert not missing, f"missing routes: {missing}"


def test_prompt_catalog_yaml_has_core_keys():
    # Reads the shipped catalog directly (no main import) so it runs locally.
    from pathlib import Path

    import yaml

    path = Path(__file__).parent.parent / "config" / "prompts" / "catalog.yaml"
    entries = yaml.safe_load(path.read_text())
    keys = {(e["kind"], e["name"]) for e in entries}
    assert {("prompts", "persona"), ("prompts", "systemprompt")}.issubset(keys)
