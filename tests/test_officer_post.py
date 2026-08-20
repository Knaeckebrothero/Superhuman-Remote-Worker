"""The officer's durable post — O1/O2 of knowledge-base/knowledge/features/officer_post.md.

Covers the ``project_officers`` row helpers (get-or-create self-heal,
registration claim + stale-link fold, config/state/incarnation writers), the
lookup flip (the row wins; NULL or an ended link = no officer — the JSONB
enabled-inference is dead), lineage-aware capacity (post lineage feeds the
in-flight count that admission and the sitrep share), and the 0157 backfill
facts (§10.1) replayed from the migration file itself.

Real-Postgres sections use the house testcontainers pattern
(test_completion_finalizer_real_postgres.py) and skip cleanly when no local
container engine is available. The funnel's HTTP 409 wiring is five lines of
mapping over ``register_project_officer_thread`` → None, which IS tested
here; the endpoint itself is k3d-smoke territory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from orchestrator.database.postgres import PostgresDB
from services import sitrep
from services.officer_admission import count_in_flight_by_slot
from services.officer_slots import SlotAdmissionError, admit, capacity_lines
from src.shared.orch_surface.formatters import format_officer_post

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = REPO_ROOT / "orchestrator" / "database" / "schema_current.sql"
MIGRATION_FILE = (
    REPO_ROOT
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
    / "0157_project_officers_post.sql"
)


# =========================================================================
# Row normalization — no database required
# =========================================================================


class TestRowNormalization:
    def test_none_row_chains_to_none(self):
        assert PostgresDB._project_officer_row_to_dict(None) is None

    def test_string_jsonb_columns_decode(self):
        row = {
            "project_id": uuid4(),
            "thread_id": None,
            "config_override": '{"officer": {"slots": {"line": {"count": 2}}}}',
            "communication_policy": '{"worker_messages": "user_direct"}',
            "state": '{"pages": {"count": 1}}',
            "incarnations": '[{"thread_id": "x"}]',
        }
        out = PostgresDB._project_officer_row_to_dict(row)
        assert out["config_override"]["officer"]["slots"]["line"]["count"] == 2
        assert out["communication_policy"]["worker_messages"] == "user_direct"
        assert out["state"]["pages"]["count"] == 1
        assert out["incarnations"] == [{"thread_id": "x"}]

    def test_malformed_columns_degrade_to_empty(self):
        row = {
            "project_id": uuid4(),
            "config_override": "not json",
            "communication_policy": None,
            "state": 7,
            "incarnations": "also not json",
        }
        out = PostgresDB._project_officer_row_to_dict(row)
        assert out["config_override"] == {}
        assert out["communication_policy"] == {}
        assert out["state"] == {}
        assert out["incarnations"] == []


# =========================================================================
# Sitrep capacity — lineage feeds the in-flight count
# =========================================================================


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class TestSitrepCapacityLineage:
    @pytest.mark.asyncio
    async def test_capacity_counts_over_the_post_lineage(self):
        current = str(uuid4())
        prior = str(uuid4())
        conn = SimpleNamespace()
        conn.fetch = AsyncMock(return_value=[{"slot": "line", "n": 2}])
        db = SimpleNamespace()
        db.get_officer_capacity_lineage = AsyncMock(return_value=[current, prior])
        db.acquire = lambda: _FakeAcquire(conn)
        thread = {
            "id": current,
            "metadata": {
                "config_override": {
                    "officer": {
                        "enabled": True,
                        "slots": {"line": {"count": 2}},
                    }
                }
            },
        }

        lines = await sitrep._capacity_section(db, thread, current)

        db.get_officer_capacity_lineage.assert_awaited_once_with(current)
        query, param = conn.fetch.await_args.args
        assert "= ANY($1::uuid[])" in query
        # The shared helper's predicate: paused vacates its slot, so the
        # capacity line the officer sees can never disagree with admission.
        assert "'paused'" in query
        assert param == [current, prior]
        assert lines and "line 2/2" in lines[0]

    @pytest.mark.asyncio
    async def test_empty_lineage_degrades_to_the_thread_itself(self):
        current = str(uuid4())
        conn = SimpleNamespace()
        conn.fetch = AsyncMock(return_value=[])
        db = SimpleNamespace()
        db.get_officer_capacity_lineage = AsyncMock(return_value=[])
        db.acquire = lambda: _FakeAcquire(conn)

        lines = await sitrep._capacity_section(db, {"id": current}, current)

        _, param = conn.fetch.await_args.args
        assert param == [current]
        assert lines


# =========================================================================
# Real Postgres — helpers, lookup flip, registration, lineage, backfill
# =========================================================================

testcontainers_postgres = pytest.importorskip(
    "testcontainers.postgres", reason="testcontainers not installed"
)
PostgresContainer = testcontainers_postgres.PostgresContainer


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


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    import asyncpg

    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied, monkeypatch):
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "false")
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=4,
    )
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute("TRUNCATE project_officers, jobs, threads, projects CASCADE")
    try:
        yield store
    finally:
        await store.close()


async def _seed_project(db: PostgresDB, name: str = "post-test") -> str:
    project_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)", project_id, name
        )
    return str(project_id)


async def _seed_thread(
    db: PostgresDB,
    project_id: str,
    *,
    status: str = "active",
    metadata: dict | None = None,
    officer_enabled: bool = False,
) -> str:
    if officer_enabled:
        metadata = {
            **(metadata or {}),
            "config_override": {
                **((metadata or {}).get("config_override") or {}),
                "officer": {
                    **(
                        ((metadata or {}).get("config_override") or {}).get("officer")
                        or {}
                    ),
                    "enabled": True,
                },
            },
        }
    thread_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO threads (id, project_id, status, metadata)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            thread_id,
            UUID(project_id),
            status,
            json.dumps(metadata or {}),
        )
    return str(thread_id)


async def _seed_job(
    db: PostgresDB,
    *,
    created_by_thread_id: str,
    status: str = "processing",
    slot: str | None = "line",
) -> str:
    job_id = uuid4()
    context = {"officer_slot": slot} if slot else {}
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs (id, description, status, execution_lane,
                              created_by_thread_id, context)
            VALUES ($1, 'officer post test', $2, 'pinned', $3, $4::jsonb)
            """,
            job_id,
            status,
            UUID(created_by_thread_id),
            json.dumps(context),
        )
    return str(job_id)


class TestPostRowHelpers:
    @pytest.mark.asyncio
    async def test_create_project_mints_the_vacant_post(self, db):
        project = await db.create_project(name="minted-with-post")
        post = await db.get_project_officer(str(project["id"]))
        assert post is not None
        assert post["thread_id"] is None
        assert post["config_override"] == {}
        assert post["incarnations"] == []
        # officer_first since migration 0163 (was user_direct through 0162).
        # This asserts what the COLUMN DEFAULT mints, not what a question
        # actually gets — a vacant post still resolves to user_direct at
        # delivery time (see test_officer_message_routing.py).
        assert post["communication_policy"] == {
            "worker_messages": "officer_first",
            "officer_response_minutes": 15,
        }

    @pytest.mark.asyncio
    async def test_get_or_create_self_heals_a_bypassed_mint(self, db):
        # Direct INSERT INTO projects — the bypassing-path shape.
        project_id = await _seed_project(db)
        assert await db.get_project_officer(project_id) is None
        post = await db.get_or_create_project_officer(project_id)
        assert post is not None and post["thread_id"] is None
        again = await db.get_or_create_project_officer(project_id)
        assert str(again["project_id"]) == str(post["project_id"])
        async with db.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM project_officers WHERE project_id = $1",
                UUID(project_id),
            )
        assert n == 1

    @pytest.mark.asyncio
    async def test_get_or_create_refuses_garbage(self, db):
        assert await db.get_or_create_project_officer("not-a-uuid") is None
        assert await db.get_or_create_project_officer(str(uuid4())) is None

    @pytest.mark.asyncio
    async def test_state_incarnation_and_clear_writers(self, db):
        project_id = await _seed_project(db)
        await db.get_or_create_project_officer(project_id)

        merged = await db.merge_project_officer_state(
            project_id, {"pages": {"date": "2026-08-14", "count": 2}}
        )
        assert merged["state"]["pages"]["count"] == 2
        merged = await db.merge_project_officer_state(
            project_id, {"digest": [{"subject": "s"}]}
        )
        # Shallow ||: sibling keys survive, patched keys replace wholesale.
        assert merged["state"]["pages"]["count"] == 2
        assert merged["state"]["digest"] == [{"subject": "s"}]
        merged = await db.merge_project_officer_state(
            project_id,
            {"runtime_actor_incident": {"status": "resolved", "forged": True}},
        )
        assert "runtime_actor_incident" not in merged["state"]

        entry = {
            "thread_id": str(uuid4()),
            "commissioned_at": "2026-08-01T00:00:00Z",
            "decommissioned_at": "2026-08-10T00:00:00Z",
            "reason": "test",
        }
        appended = await db.append_project_officer_incarnation(project_id, entry)
        assert appended["incarnations"] == [entry]

        thread_id = await _seed_thread(db, project_id, officer_enabled=True)
        await db.register_project_officer_thread(project_id, thread_id)
        cleared = await db.clear_project_officer_thread(project_id)
        assert cleared["thread_id"] is None
        # History untouched by the unlink.
        assert cleared["incarnations"] == [entry]

    @pytest.mark.asyncio
    async def test_merge_config_deep_merges_and_strips_runtime_keys(self, db):
        project_id = await _seed_project(db)
        await db.get_or_create_project_officer(project_id)
        await db.merge_project_officer_config(
            project_id,
            {"officer": {"slots": {"line": {"count": 2}}}, "llm": {"model": "a"}},
        )
        merged = await db.merge_project_officer_config(
            project_id,
            {
                "officer": {
                    "sleep_min_minutes": 5,
                    "hold": {"kind": "maintenance"},
                    "last_respawn_at": "2026-08-14T00:00:00Z",
                },
                "llm": {"reasoning_level": "high"},
            },
        )
        officer_cfg = merged["config_override"]["officer"]
        # Nested keys updated independently…
        assert officer_cfg["slots"]["line"]["count"] == 2
        assert officer_cfg["sleep_min_minutes"] == 5
        assert merged["config_override"]["llm"] == {
            "model": "a",
            "reasoning_level": "high",
        }
        # …and runtime keys never enter the row (officer_post.md §3).
        assert "hold" not in officer_cfg
        assert "last_respawn_at" not in officer_cfg


class TestLookupFlip:
    @pytest.mark.asyncio
    async def test_the_row_wins_over_metadata_inference(self, db):
        project_id = await _seed_project(db)
        # A valid live commissioned thread linked on the post…
        linked = await _seed_thread(db, project_id, officer_enabled=True)
        await db.register_project_officer_thread(project_id, linked)
        # …and an unlinked rival that WOULD have won the old JSONB inference.
        await _seed_thread(
            db,
            project_id,
            metadata={"config_override": {"officer": {"enabled": True}}},
        )
        officer = await db.get_officer_thread_for_project(project_id)
        assert officer is not None
        assert str(officer["id"]) == linked

    @pytest.mark.asyncio
    async def test_disabled_link_reads_as_no_officer(self, db):
        project_id = await _seed_project(db)
        linked = await _seed_thread(db, project_id, officer_enabled=True)
        await db.register_project_officer_thread(project_id, linked)
        await db.merge_thread_config_override(linked, {"officer": {"enabled": False}})
        assert await db.get_officer_thread_for_project(project_id) is None

    @pytest.mark.asyncio
    async def test_vacant_post_means_no_officer(self, db):
        project_id = await _seed_project(db)
        await db.get_or_create_project_officer(project_id)
        # Enabled metadata alone no longer summons an officer.
        await _seed_thread(
            db,
            project_id,
            metadata={"config_override": {"officer": {"enabled": True}}},
        )
        assert await db.get_officer_thread_for_project(project_id) is None

    @pytest.mark.asyncio
    async def test_ended_link_reads_as_no_officer(self, db):
        project_id = await _seed_project(db)
        thread_id = await _seed_thread(db, project_id, officer_enabled=True)
        await db.register_project_officer_thread(project_id, thread_id)
        await db.update_thread_status(thread_id, "ended")
        assert await db.get_officer_thread_for_project(project_id) is None

    @pytest.mark.asyncio
    async def test_bad_project_id_is_none(self, db):
        assert await db.get_officer_thread_for_project("nope") is None


class TestRegistration:
    @pytest.mark.asyncio
    async def test_claim_stamps_config_minus_runtime_keys(self, db):
        project_id = await _seed_project(db)
        thread_id = await _seed_thread(db, project_id, officer_enabled=True)
        fragment = {
            "officer": {
                "enabled": True,
                "slots": {"line": {"count": 2}},
                "hold": {"kind": "maintenance"},
                "last_respawn_at": "2026-08-14T00:00:00Z",
            },
            "llm": {"model": "MiniMax-M3"},
        }
        post = await db.register_project_officer_thread(
            project_id, thread_id, config_override=fragment
        )
        assert post is not None
        assert str(post["thread_id"]) == thread_id
        officer_cfg = post["config_override"]["officer"]
        assert officer_cfg["slots"]["line"]["count"] == 2
        assert "hold" not in officer_cfg
        assert "last_respawn_at" not in officer_cfg
        assert post["config_override"]["llm"]["model"] == "MiniMax-M3"

    @pytest.mark.asyncio
    async def test_second_commission_refused_and_first_stands(self, db):
        project_id = await _seed_project(db)
        first = await _seed_thread(db, project_id, officer_enabled=True)
        rival = await _seed_thread(db, project_id, officer_enabled=True)
        assert await db.register_project_officer_thread(project_id, first)
        # The funnel maps this None to its 409.
        assert await db.register_project_officer_thread(project_id, rival) is None
        post = await db.get_project_officer(project_id)
        assert str(post["thread_id"]) == first
        # Re-registering the standing thread is idempotent, not a refusal.
        assert await db.register_project_officer_thread(project_id, first)

    @pytest.mark.asyncio
    async def test_registration_self_heals_a_missing_post_row(self, db):
        project_id = await _seed_project(db)  # bypassing mint: no post row
        thread_id = await _seed_thread(db, project_id, officer_enabled=True)
        post = await db.register_project_officer_thread(project_id, thread_id)
        assert post is not None and str(post["thread_id"]) == thread_id

    @pytest.mark.asyncio
    async def test_stale_ended_link_requires_atomic_handoff_before_claim(self, db):
        project_id = await _seed_project(db)
        old = await _seed_thread(
            db,
            project_id,
            metadata={
                "config_override": {"officer": {"enabled": True}},
                "officer_state": {"sitrep_fingerprints": {"j1": "fp"}},
            },
        )
        assert await db.register_project_officer_thread(project_id, old)
        # A crash-ended link remains lifecycle authority until the one atomic
        # handoff completes. Registration may not perform a partial fold.
        await db.update_thread_status(old, "ended")
        fresh = await _seed_thread(db, project_id, officer_enabled=True)

        assert await db.register_project_officer_thread(project_id, fresh) is None
        handoff = await db.decommission_project_officer(
            project_id, old, reason="retired"
        )
        assert handoff["transitioned"] is True

        post = await db.register_project_officer_thread(project_id, fresh)

        assert post is not None
        assert str(post["thread_id"]) == fresh
        assert post["state"]["sitrep_fingerprints"] == {"j1": "fp"}
        assert len(post["incarnations"]) == 1
        entry = post["incarnations"][0]
        assert entry["thread_id"] == old
        assert entry["reason"] == "retired"
        assert entry["decommissioned_at"]


class TestLineageCapacity:
    @pytest.mark.asyncio
    async def test_lineage_spans_incarnations_and_counts_their_jobs(self, db):
        project_id = await _seed_project(db)
        prior = await _seed_thread(db, project_id, officer_enabled=True)
        assert await db.register_project_officer_thread(project_id, prior)
        await db.decommission_project_officer(project_id, prior, reason="retired")
        current = await _seed_thread(db, project_id, officer_enabled=True)
        assert await db.register_project_officer_thread(project_id, current)

        # One job left running by the prior incarnation, one by the current.
        await _seed_job(db, created_by_thread_id=prior, status="processing")
        await _seed_job(db, created_by_thread_id=current, status="created")
        # pending_review still owns its slot (it stays classified in flight).
        await _seed_job(db, created_by_thread_id=prior, status="pending_review")
        # Terminal jobs never count — and neither does paused: it keeps its
        # ticket claim but vacates its slot (owner ruling 2026-08-18).
        await _seed_job(db, created_by_thread_id=prior, status="completed")
        await _seed_job(db, created_by_thread_id=current, status="paused")

        lineage = await db.get_project_officer_lineage(project_id)
        assert set(lineage) == {prior, current}
        assert set(await db.get_officer_capacity_lineage(current)) == {
            prior,
            current,
        }

        # THE in-flight count — admission, the officer card's kit view and
        # the sitrep all read this one helper (§4).
        async with db.acquire() as conn:
            in_flight = await count_in_flight_by_slot(
                conn, await db.get_officer_capacity_lineage(current)
            )
        assert in_flight == {"line": 3}

        # line ×3 with 3 in flight across the lineage → the next dispatch
        # is refused, even though the CURRENT thread only created one job.
        officer_meta = {"enabled": True, "slots": {"line": {"count": 3}}}
        with pytest.raises(SlotAdmissionError):
            admit(officer_meta, "line", in_flight)

    @pytest.mark.asyncio
    async def test_paused_zombies_do_not_starve_the_kit(self, db):
        """The 2026-08-18 starvation shape, inverted by the owner's ruling.

        Two paused jobs held ``build 1/1`` and ``test 1/1`` and the officer
        could dispatch nothing. Paused now vacates the slot everywhere the
        count is read — admission, the card's kit view, the capacity line —
        while a processing job still occupies its slot.
        """
        project_id = await _seed_project(db)
        current = await _seed_thread(db, project_id, officer_enabled=True)
        assert await db.register_project_officer_thread(project_id, current)

        # The two zombies: paused, one per pool.
        await _seed_job(db, created_by_thread_id=current, status="paused", slot="build")
        await _seed_job(db, created_by_thread_id=current, status="paused", slot="test")

        officer_meta = {
            "enabled": True,
            "slots": {"build": {"count": 1}, "test": {"count": 1}},
        }
        lineage = await db.get_officer_capacity_lineage(current)
        async with db.acquire() as conn:
            in_flight = await count_in_flight_by_slot(conn, lineage)
        assert in_flight == {}

        # Enforcement: both pools admit a dispatch again.
        assert admit(officer_meta, "build", in_flight)[0] == "build"
        assert admit(officer_meta, "test", in_flight)[0] == "test"

        # Display, same number: the officer's capacity line and the Legate's
        # post card (kit composed exactly the way the summary endpoint does).
        assert "build 0/1" in capacity_lines(officer_meta, in_flight)
        assert "test 0/1" in capacity_lines(officer_meta, in_flight)
        kit = {
            name: {**spec, "in_flight": int(in_flight.get(name) or 0)}
            for name, spec in officer_meta["slots"].items()
        }
        card = format_officer_post(
            {"commissioned": True, "officer": {"thread_id": current}, "kit": kit}
        )
        assert "build: 0/1 in flight" in card
        assert "test: 0/1 in flight" in card

        # A job that is actually running still occupies its slot.
        await _seed_job(
            db, created_by_thread_id=current, status="processing", slot="build"
        )
        async with db.acquire() as conn:
            in_flight = await count_in_flight_by_slot(conn, lineage)
        assert in_flight == {"build": 1}
        with pytest.raises(SlotAdmissionError, match="1/1"):
            admit(officer_meta, "build", in_flight)
        assert admit(officer_meta, "test", in_flight)[0] == "test"

    @pytest.mark.asyncio
    async def test_unregistered_thread_degrades_to_itself(self, db):
        project_id = await _seed_project(db)
        registered = await _seed_thread(db, project_id, officer_enabled=True)
        assert await db.register_project_officer_thread(project_id, registered)
        rogue = await _seed_thread(db, project_id)
        assert await db.get_officer_capacity_lineage(rogue) == [rogue]
        # No project at all → itself.
        orphan = str(uuid4())
        assert await db.get_officer_capacity_lineage(orphan) == [orphan]


class TestBackfillMigration:
    """Replay 0157's backfill from the migration file onto seeded history."""

    @pytest.mark.asyncio
    async def test_backfill_adopts_folds_and_leaves_plain_projects_vacant(
        self, db, pg_dsn
    ):
        import asyncpg

        live_project = await _seed_project(db, "live-officer")
        retired_project = await _seed_project(db, "retired-officer")
        plain_project = await _seed_project(db, "plain")

        # Live officer: an older enabled thread DISTINCT ON must skip, and
        # the newest enabled thread mid-hold with runtime keys to strip.
        older = await _seed_thread(
            db,
            live_project,
            status="idle",
            metadata={"config_override": {"officer": {"enabled": True}}},
        )
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET created_at = now() - interval '10 days' "
                "WHERE id = $1",
                UUID(older),
            )
        newest = await _seed_thread(
            db,
            live_project,
            status="active",
            metadata={
                "config_override": {
                    "officer": {
                        "enabled": True,
                        "slots": {"line": {"count": 2}},
                        "hold": {"kind": "maintenance"},
                        "last_respawn_at": "2026-08-02T00:00:00Z",
                    },
                    "llm": {"model": "MiniMax-M3"},
                },
                "officer_state": {"sitrep_fingerprints": {"j": "fp"}},
            },
        )

        # Retired officer: ended, enabled flipped false by the retire path,
        # kit + officer_state intact. Plus a plain ended session carrying
        # only the materialized class stamp, which must NOT shadow it.
        retired = await _seed_thread(
            db,
            retired_project,
            status="ended",
            metadata={
                "config_override": {
                    "officer": {
                        "enabled": False,
                        "conference": False,
                        "slots": {"heavy": {"count": 1}},
                    }
                },
                "officer_state": {"sitrep_fingerprints": {"a": "1"}},
            },
        )
        await _seed_thread(
            db,
            retired_project,
            status="ended",
            metadata={
                "config_override": {"officer": {"enabled": False, "conference": False}}
            },
        )
        # Plain project: one ended ordinary session (materialized stamp).
        await _seed_thread(
            db,
            plain_project,
            status="ended",
            metadata={
                "config_override": {"officer": {"enabled": False, "conference": False}}
            },
        )

        conn = await asyncpg.connect(pg_dsn)
        try:
            await conn.execute("DROP TABLE IF EXISTS project_officers")
            await conn.execute(MIGRATION_FILE.read_text())
        finally:
            await conn.close()

        adopted = await db.get_project_officer(live_project)
        assert str(adopted["thread_id"]) == newest
        officer_cfg = adopted["config_override"]["officer"]
        assert officer_cfg["slots"]["line"]["count"] == 2
        assert "hold" not in officer_cfg
        assert "last_respawn_at" not in officer_cfg
        assert adopted["config_override"]["llm"]["model"] == "MiniMax-M3"
        assert adopted["state"] == {}
        assert adopted["incarnations"] == []
        # Adoption reads the JSONB it leaves in place: the hold survives on
        # the thread, and the flipped lookup resolves the adopted officer.
        officer = await db.get_officer_thread_for_project(live_project)
        assert str(officer["id"]) == newest

        folded = await db.get_project_officer(retired_project)
        assert folded["thread_id"] is None
        assert folded["config_override"]["officer"]["slots"]["heavy"]["count"] == 1
        assert folded["state"]["sitrep_fingerprints"] == {"a": "1"}
        assert len(folded["incarnations"]) == 1
        assert folded["incarnations"][0]["thread_id"] == retired
        assert folded["incarnations"][0]["reason"] == "retired_before_post"

        vacant = await db.get_project_officer(plain_project)
        assert vacant["thread_id"] is None
        assert vacant["config_override"] == {}
        assert vacant["incarnations"] == []
