"""Managed assistant repair is conditional on the live row, not a stale read."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.default_experts import (
    MANAGED_SEEDS,
    load_seed_bundle,
    seed_managed_default_experts,
    upgrade_managed_seed,
)


ROOT = Path(__file__).resolve().parents[1]
KEY = "application-default-session-seed"
SPEC = next(spec for spec in MANAGED_SEEDS if spec["managed_key"] == KEY)
OLD_ROSTER = {
    "default": "explorer",
    "roster": {
        name: {"$ref": f"subagents/{name}"}
        for name in ("explorer", "reader", "implementer")
    },
}


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:15") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(
            (ROOT / "src/orchestrator/database/schema_current.sql").read_text()
        )
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, schema_applied):
    store = PostgresDB(connection_string=pg_dsn, min_connections=1, max_connections=4)
    await store.connect()
    try:
        await store.execute("TRUNCATE experts CASCADE")
        yield store
    finally:
        await store.close()


def _bundle():
    return load_seed_bundle(
        ROOT / "config", directory="assistant", expert_type="session"
    )


async def _legacy(db, *, roster=None, seed_version=2):
    bundle = _bundle()
    bundle["config"]["subagents"] = deepcopy(OLD_ROSTER if roster is None else roster)
    bundle["config"]["operator_extra"] = {"enabled": True}
    bundle["prompts"] = {"persona": "Operator-authored persona"}
    bundle["display_name"] = "Operator label"
    row, created = await db.upsert_managed_expert(
        managed_key=KEY, seed_version=seed_version, **bundle
    )
    assert created
    return row


def _config(row):
    return json.loads(row["config"])


@pytest.mark.asyncio
async def test_exact_v2_roster_repairs_once_preserving_other_content_and_pointer(db):
    before = await _legacy(db)
    alternative = _bundle()
    alternative["name"] = "operator-choice"
    selected, _ = await db.upsert_managed_expert(
        managed_key="operator-alternative", seed_version=2, **alternative
    )
    await db.ensure_application_expert_default(
        expert_type="session", expert_id=str(selected["id"])
    )

    await seed_managed_default_experts(db, ROOT / "config")

    after = await db.get_expert_by_managed_key(KEY)
    expected_config = _config(before)
    expected_config["subagents"] = _bundle()["config"]["subagents"]
    assert _config(after) == expected_config
    assert after["seed_version"] == 3
    assert after["version"] == before["version"] + 1
    for key in before.keys() - {"config", "version", "seed_version", "updated_at"}:
        assert after[key] == before[key], key
    pointer = await db.get_application_expert_default("session")
    assert pointer["id"] == selected["id"]
    assert await db.get_expert_by_managed_key("operator-alternative") == selected

    await seed_managed_default_experts(db, ROOT / "config")
    assert await db.get_expert_by_managed_key(KEY) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["default", "entry", "shell"])
async def test_operator_roster_variants_survive_seed_upgrade(db, edit):
    roster = deepcopy(OLD_ROSTER)
    if edit == "default":
        roster["default"] = "reader"
    elif edit == "entry":
        roster["roster"]["implementer"]["description"] = "My custom writer"
    else:
        roster["roster"]["implementer"]["tools"] = {"shell": ["run_command"]}
    before = await _legacy(db, roster=roster)

    await seed_managed_default_experts(db, ROOT / "config")

    after = await db.get_expert_by_managed_key(KEY)
    assert _config(after) == _config(before)
    assert after["prompts"] == before["prompts"]
    assert after["seed_version"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_version", [1, 3, 4])
async def test_repair_requires_exact_seed_version(db, seed_version):
    before = await _legacy(db, seed_version=seed_version)

    updated = await db.upgrade_managed_expert_seed(
        managed_key=KEY,
        seed_version=3,
        config_additions={},
        expected_seed_version=2,
        expected_subagents=OLD_ROSTER,
        replacement_subagents=_bundle()["config"]["subagents"],
    )

    assert updated is None
    assert await db.get_expert_by_managed_key(KEY) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_roster", [True, False])
async def test_waiting_upgrade_rechecks_operator_edit_after_row_lock(db, edit_roster):
    stale_row = await _legacy(db)
    task = None
    async with db.acquire() as editor:
        blocker = await editor.fetchval("SELECT pg_backend_pid()")
        async with editor.transaction():
            if edit_roster:
                await editor.execute(
                    """UPDATE experts SET config = jsonb_set(
                           config, '{subagents,roster,implementer,description}',
                           '"Concurrent operator edit"'::jsonb
                       ), version = version + 1 WHERE managed_key = $1""",
                    KEY,
                )
            else:
                await editor.execute(
                    """UPDATE experts SET config = config ||
                           '{"concurrent_operator_key": "preserve"}'::jsonb,
                           prompts = '{"persona": "Concurrent persona"}'::jsonb,
                           version = version + 1 WHERE managed_key = $1""",
                    KEY,
                )
            operator_row = dict(
                await editor.fetchrow(
                    "SELECT * FROM experts WHERE managed_key = $1", KEY
                )
            )
            task = asyncio.create_task(
                upgrade_managed_seed(db, spec=SPEC, bundle=_bundle(), row=stale_row)
            )
            try:
                # Prove the repair actually waits on the editor's row lock,
                # then commit the operator edit so PostgreSQL must recheck.
                async with asyncio.timeout(10):
                    while not await db.fetchval(
                        """SELECT EXISTS (
                               SELECT 1 FROM pg_stat_activity
                               WHERE $1 = ANY(pg_blocking_pids(pid))
                           )""",
                        blocker,
                    ):
                        await asyncio.sleep(0.01)
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise

    updated = await asyncio.wait_for(task, timeout=10)
    after = await db.get_expert_by_managed_key(KEY)
    if edit_roster:
        assert updated is None
        assert after == operator_row
    else:
        assert updated is not None
        expected = _config(operator_row)
        expected["subagents"] = _bundle()["config"]["subagents"]
        assert _config(after) == expected
        assert after["prompts"] == operator_row["prompts"]
        assert after["seed_version"] == 3
        assert after["version"] == operator_row["version"] + 1
