"""0158: repair of the tool-group marker damage 0156 left in stored policies.

0156 minted explicit ``job_control: []`` / ``job_inspection: []`` members (and
left ``orchestrator: []`` behind after moving every name out of it). The
legacy session path reads ``== []`` as a hard-disable marker while an ABSENT
key gets that group's defaults appended, so migrated rows silently lost their
job tools. 0158 repairs forward; applied 0156 is never edited.

Static checks always run; the functional replay uses a throwaway Postgres
container (same pattern as ``test_officer_post.py``) and skips cleanly when
none is available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "src/orchestrator/database/migrations/app/0158_repair_unified_tool_group_markers.sql"
)


# =========================================================================
# Static contract of the migration file
# =========================================================================


def test_migration_follows_house_conventions() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith(
        "-- migration:     0158_repair_unified_tool_group_markers.sql"
    )
    for field in ("-- description:", "-- depends-on:", "-- expected:", "-- locks:"):
        assert field in text
    assert "-- depends-on:    0157_project_officers_post.sql" in text
    assert "-- transactional: yes" in text
    assert text.count("BEGIN;") == 1
    assert text.count("COMMIT;") == 1
    assert "SET LOCAL lock_timeout" in text
    assert "SET LOCAL statement_timeout" in text
    # Repair-forward only: 0158 must not rewrite or supersede applied 0156.
    assert "pg_temp." in text


def test_migration_covers_every_store_0156_rewrote() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "experts",
        "project_experts",
        "projects",
        "automations",
        "config_overrides",
        "threads",
        "jobs",
    ):
        assert f"UPDATE {table}" in text
    assert "resolved_config" in text  # the embedded agent config too


def test_migration_reasons_through_both_f3_variants() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert "get_session_context row" in text
    assert "all-job-names row" in text
    # The legitimacy carve-out for a pre-existing orchestrator opt-out.
    assert "opt-out" in text


# =========================================================================
# Functional replay against a real Postgres
# =========================================================================

testcontainers_postgres = pytest.importorskip(
    "testcontainers.postgres", reason="testcontainers not installed"
)
PostgresContainer = testcontainers_postgres.PostgresContainer

_MINIMAL_DDL = """
CREATE TABLE experts (id serial PRIMARY KEY, config JSONB);
CREATE TABLE project_experts (id serial PRIMARY KEY, config_override JSONB);
CREATE TABLE projects (id serial PRIMARY KEY, default_config_override JSONB);
CREATE TABLE automations (id serial PRIMARY KEY, config_override JSONB);
CREATE TABLE config_overrides (id serial PRIMARY KEY, value_json JSONB);
CREATE TABLE threads (id serial PRIMARY KEY, metadata JSONB);
CREATE TABLE jobs (id serial PRIMARY KEY, config_override JSONB, resolved_config JSONB);
"""

# --- Post-0156 shapes ------------------------------------------------------

# F3 variant one: {orchestrator: ["get_session_context"]} grew minted empty
# job groups. Repair deletes both artifacts and keeps orchestrator.
VARIANT_APP_TOOLS = {
    "tools": {
        "orchestrator": ["get_session_context"],
        "job_control": [],
        "job_inspection": [],
    }
}
VARIANT_APP_TOOLS_REPAIRED = {"tools": {"orchestrator": ["get_session_context"]}}

# F3 variant two: a row that held ONLY job names; 0156 moved them all out,
# leaving orchestrator: [] residue. Repair deletes the residue (the non-empty
# job groups are the movement proof) and keeps the renamed grants.
VARIANT_ALL_JOB_NAMES = {
    "tools": {
        "orchestrator": [],
        "job_control": ["create_job", "cancel_job"],
        "job_inspection": ["list_jobs", "get_job"],
    }
}
VARIANT_ALL_JOB_NAMES_REPAIRED = {
    "tools": {
        "job_control": ["create_job", "cancel_job"],
        "job_inspection": ["list_jobs", "get_job"],
    }
}

# A pre-existing orchestrator opt-out (both job groups empty after 0156's
# minting): the artifacts go, the opt-out survives.
VARIANT_OPTOUT = {
    "tools": {"orchestrator": [], "job_control": [], "job_inspection": []}
}
VARIANT_OPTOUT_REPAIRED = {"tools": {"orchestrator": []}}

# Untouchables.
VARIANT_BOOLEAN = {"tools": {"orchestrator": True}}  # 0156 skipped; 0158 must too
VARIANT_GENUINE = {"tools": {"job_control": ["create_job"], "workspace": ["read_file"]}}
VARIANT_NO_TOOLS = {"llm": {"model": "gpt-5.6"}}


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        container = PostgresContainer("postgres:15")
        container.start()
    except Exception as exc:  # docker/podman not available on this runner
        pytest.skip(f"local Postgres container unavailable: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def conn(pg_dsn):
    asyncpg = pytest.importorskip("asyncpg")
    connection = await asyncpg.connect(pg_dsn)
    await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await connection.execute(_MINIMAL_DDL)
    try:
        yield connection
    finally:
        await connection.close()


async def _fetch_json(conn, query: str):
    value = await conn.fetchval(query)
    return json.loads(value) if value is not None else None


@pytest.mark.asyncio
async def test_repair_restores_pre_0156_effective_semantics(conn) -> None:
    seed = json.dumps
    await conn.execute(
        "INSERT INTO threads (metadata) VALUES ($1), ($2), ($3), ($4), ($5)",
        seed({"config_override": VARIANT_APP_TOOLS}),
        seed({"config_override": VARIANT_ALL_JOB_NAMES}),
        seed({"config_override": VARIANT_OPTOUT}),
        seed({"config_override": VARIANT_BOOLEAN}),
        seed({"config_override": VARIANT_GENUINE}),
    )
    await conn.execute(
        "INSERT INTO jobs (config_override, resolved_config) VALUES ($1, $2)",
        seed(VARIANT_APP_TOOLS),
        seed({"agent": VARIANT_OPTOUT, "prompts": {"system": "keep me"}}),
    )
    await conn.execute(
        "INSERT INTO experts (config) VALUES ($1)", seed(VARIANT_NO_TOOLS)
    )
    await conn.execute(
        "INSERT INTO projects (default_config_override) VALUES ($1)",
        seed(VARIANT_ALL_JOB_NAMES),
    )

    await conn.execute(MIGRATION.read_text(encoding="utf-8"))

    threads = [
        json.loads(row["metadata"])["config_override"]
        for row in await conn.fetch("SELECT metadata FROM threads ORDER BY id")
    ]
    assert threads[0] == VARIANT_APP_TOOLS_REPAIRED
    assert threads[1] == VARIANT_ALL_JOB_NAMES_REPAIRED
    assert threads[2] == VARIANT_OPTOUT_REPAIRED
    assert threads[3] == VARIANT_BOOLEAN  # boolean rows stay code-side (FIX-3b)
    assert threads[4] == VARIANT_GENUINE

    job = await conn.fetchrow("SELECT config_override, resolved_config FROM jobs")
    assert json.loads(job["config_override"]) == VARIANT_APP_TOOLS_REPAIRED
    resolved = json.loads(job["resolved_config"])
    assert resolved["agent"] == VARIANT_OPTOUT_REPAIRED
    assert resolved["prompts"] == {"system": "keep me"}  # non-tool fields intact

    assert await _fetch_json(conn, "SELECT config FROM experts") == VARIANT_NO_TOOLS
    assert (
        await _fetch_json(conn, "SELECT default_config_override FROM projects")
        == VARIANT_ALL_JOB_NAMES_REPAIRED
    )


@pytest.mark.asyncio
async def test_repair_is_idempotent(conn) -> None:
    await conn.execute(
        "INSERT INTO threads (metadata) VALUES ($1)",
        json.dumps({"config_override": VARIANT_APP_TOOLS}),
    )
    script = MIGRATION.read_text(encoding="utf-8")
    await conn.execute(script)
    first = await _fetch_json(conn, "SELECT metadata FROM threads")
    # pg_temp functions persist for the session; a rerun must find nothing to
    # change (repaired rows no longer match) and redefine cleanly.
    await conn.execute("DISCARD TEMP")
    await conn.execute(script)
    second = await _fetch_json(conn, "SELECT metadata FROM threads")
    assert first == second
    assert second["config_override"] == VARIANT_APP_TOOLS_REPAIRED
