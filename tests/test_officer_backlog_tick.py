"""Officer auto-pull tick — B3 of docs/features/officer_backlog_pools.md.

The tick spends money and creates work unattended, so the tests that matter
here are the ones about NOT dispatching. In rough order of how expensive the
bug would be:

* A terminal job still holds its ticket (one-shot claims). The original design
  released the claim on terminal, which re-dispatched every completed ticket a
  minute later and re-burned every failing one at breaker cadence, forever.
* Capacity and claim predicates agree, and both count paused/pending-review
  jobs. A paused executor that stopped occupying its slot lets a second
  executor start on the story it is halfway through.
* Honest failure reports never open a breaker; only job failures do.
* A `ready` tag with no `ready_at` fails closed.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.officer_backlog import (
    STALE_CLAIM_HOURS,
    auto_pull_enabled,
    breaker_is_open,
    eligible_tickets,
    evaluate_breaker,
    pools_from_meta,
    stale_claims,
    tick_officer,
)
from services.work_categories import EXECUTOR, RESEARCHER

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _row(note_id, *, tags, ready_at=None, title="T", note_type="feature"):
    return {
        "note_id": note_id,
        "note_type": note_type,
        "title": title,
        "priority": 1,
        "tags": list(tags),
        "ready_at": ready_at,
    }


# =============================================================================
# Configuration gates — the tick is dormant unless deliberately switched on
# =============================================================================


class TestAutoPullGate:
    def test_ships_off(self):
        # §13.1: a century whose officer has not triaged a backlog must not
        # start pulling whatever happens to be tagged.
        assert auto_pull_enabled({}) is False
        assert auto_pull_enabled({"auto_pull": False}) is False

    def test_accepts_the_shapes_json_round_trips_produce(self):
        for value in (True, "true", "True", 1):
            assert auto_pull_enabled({"auto_pull": value}) is True

    def test_only_categorized_slots_are_pools(self):
        # A slot without a category keeps today's behaviour exactly:
        # officer-directed capacity the tick never touches.
        pools = pools_from_meta(
            {
                "slots": {
                    "researchers": {"count": 2, "category": "researcher"},
                    "adhoc": {"count": 1},
                }
            }
        )
        assert set(pools) == {"researchers"}

    def test_no_roster_means_no_pools(self):
        assert pools_from_meta({}) == {}
        assert pools_from_meta({"max_concurrent_workers": 3}) == {}


# =============================================================================
# One-shot claims — the bug that would have burned the most money
# =============================================================================


class TestEligibility:
    def test_an_armed_unclaimed_ticket_is_eligible(self):
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        ready, notes = eligible_tickets(rows, {}, NOW)
        assert [t["note_id"] for t in ready] == ["feature-a"]
        assert notes == []

    def test_a_terminal_job_still_holds_its_ticket(self):
        # THE one-shot rule. Disposition is asynchronous and officer-owned, so
        # a completed job holds its claim until he reviews and re-arms.
        rows = [
            _row(
                "feature-a",
                tags=["ready", "category:researcher"],
                ready_at=NOW - timedelta(hours=3),
            )
        ]
        claims = {"feature-a": NOW - timedelta(hours=2)}  # dispatched after arming
        ready, notes = eligible_tickets(rows, claims, NOW)
        assert ready == []
        assert "claimed at" in notes[0]

    def test_re_arming_after_the_claim_makes_it_eligible_again(self):
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        claims = {"feature-a": NOW - timedelta(hours=2)}  # armed since
        ready, _ = eligible_tickets(rows, claims, NOW)
        assert [t["note_id"] for t in ready] == ["feature-a"]

    def test_a_claim_exactly_at_the_arming_instant_counts_as_claimed(self):
        # Ties go to "claimed": dispatching twice costs a job, refusing costs
        # one tick of latency.
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        ready, _ = eligible_tickets(rows, {"feature-a": NOW}, NOW)
        assert ready == []

    def test_ready_tag_without_ready_at_fails_closed(self):
        # The post-vault-rebuild case. A dispatch authorization with no
        # timestamp is not an authorization.
        rows = [_row("feature-a", tags=["ready", "category:researcher"])]
        ready, notes = eligible_tickets(rows, {}, NOW)
        assert ready == []
        assert "no ready_at" in notes[0]

    def test_an_ambiguous_ticket_is_reported_not_guessed(self):
        rows = [
            _row(
                "feature-a",
                tags=["ready", "category:researcher", "category:executor"],
                ready_at=NOW,
            )
        ]
        ready, notes = eligible_tickets(rows, {}, NOW)
        assert ready == []
        assert "multiple category" in notes[0]

    def test_a_typo_in_the_expert_pin_never_reaches_agent_boot(self):
        # It would look like a job failure there and chain-trip the breaker on
        # a spelling mistake.
        rows = [
            _row(
                "feature-a",
                tags=["ready", "category:researcher", "expert:scholr"],
                ready_at=NOW,
            )
        ]
        ready, notes = eligible_tickets(rows, {}, NOW)
        assert ready == []
        assert "unknown expert" in notes[0]

    def test_ordering_is_preserved_so_priority_still_leads(self):
        rows = [
            _row("high", tags=["ready", "category:researcher"], ready_at=NOW),
            _row("low", tags=["ready", "category:researcher"], ready_at=NOW),
        ]
        ready, _ = eligible_tickets(rows, {}, NOW)
        assert [t["note_id"] for t in ready] == ["high", "low"]


# =============================================================================
# Circuit breaker — honesty must not be punished
# =============================================================================


def _terminal(job_id, ticket, status):
    return {"id": job_id, "ticket_note_id": ticket, "status": status}


class TestBreaker:
    def test_two_failures_on_distinct_tickets_trip_it(self):
        history = [_terminal("j2", "t2", "failed"), _terminal("j1", "t1", "failed")]
        tripped = evaluate_breaker(history, {}, "line")
        assert tripped and tripped["tripped_on_job"] == "j2"
        assert tripped["tickets"] == ["t2", "t1"]

    def test_honest_negative_completions_never_trip_it(self):
        # A worker reporting goal_achieved=false completes SUCCESSFULLY. If
        # two of those opened a breaker, the fleet would learn to stop filing
        # honest negatives — the exact incentive this design fights.
        history = [
            _terminal("j2", "t2", "completed"),
            _terminal("j1", "t1", "completed"),
        ]
        assert evaluate_breaker(history, {}, "line") is None

    def test_a_success_between_two_failures_breaks_the_chain(self):
        history = [
            _terminal("j3", "t3", "failed"),
            _terminal("j2", "t2", "completed"),
            _terminal("j1", "t1", "failed"),
        ]
        assert evaluate_breaker(history, {}, "line") is None

    def test_two_failures_on_the_SAME_ticket_are_one_incident(self):
        history = [_terminal("j2", "t1", "failed"), _terminal("j1", "t1", "failed")]
        assert evaluate_breaker(history, {}, "line") is None

    def test_it_does_not_re_trip_on_the_failure_it_already_tripped_on(self):
        # Derived-from-history alone would re-open the breaker every 30 minutes
        # forever once those two rows stop changing.
        history = [_terminal("j2", "t2", "failed"), _terminal("j1", "t1", "failed")]
        state = {"backlog_breakers": {"line": {"tripped_on_job": "j2"}}}
        assert evaluate_breaker(history, state, "line") is None

    def test_a_newer_failure_after_the_window_trips_again(self):
        history = [_terminal("j3", "t3", "failed"), _terminal("j2", "t2", "failed")]
        state = {"backlog_breakers": {"line": {"tripped_on_job": "j2"}}}
        tripped = evaluate_breaker(history, state, "line")
        assert tripped and tripped["tripped_on_job"] == "j3"

    def test_too_little_history_never_trips(self):
        assert evaluate_breaker([_terminal("j1", "t1", "failed")], {}, "line") is None
        assert evaluate_breaker([], {}, "line") is None

    def test_open_is_per_pool_and_expires(self):
        state = {
            "backlog_breakers": {
                "line": {"until": (NOW + timedelta(minutes=5)).isoformat()},
                "heavy": {"until": (NOW - timedelta(minutes=5)).isoformat()},
            }
        }
        assert breaker_is_open(state, "line", NOW) is True
        assert breaker_is_open(state, "heavy", NOW) is False
        # A burning research pool must not stop the executor from shipping.
        assert breaker_is_open(state, "executors", NOW) is False


# =============================================================================
# Stale claims — surfaced, never auto-released
# =============================================================================


class TestStaleClaims:
    def test_a_claim_that_has_not_moved_is_surfaced_oldest_first(self):
        claims = [
            {
                "id": "j1",
                "ticket_note_id": "t1",
                "officer_slot": "line",
                "status": "paused",
                "updated_at": NOW - timedelta(hours=STALE_CLAIM_HOURS + 2),
            },
            {
                "id": "j2",
                "ticket_note_id": "t2",
                "officer_slot": "line",
                "status": "pending_review",
                "updated_at": NOW - timedelta(hours=STALE_CLAIM_HOURS + 10),
            },
        ]
        out = stale_claims(claims, NOW)
        assert [c["job_id"] for c in out] == ["j2", "j1"]
        assert out[0]["age_hours"] >= STALE_CLAIM_HOURS

    def test_a_fresh_claim_is_not_stale(self):
        claims = [
            {
                "id": "j1",
                "ticket_note_id": "t1",
                "status": "processing",
                "updated_at": NOW - timedelta(minutes=30),
            }
        ]
        assert stale_claims(claims, NOW) == []


# =============================================================================
# The tick end to end, against a doubled DB
# =============================================================================


def _officer_row(**over):
    row = {
        "id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "status": "active",
        "metadata": {
            "config_override": {
                "officer": {
                    "enabled": True,
                    "auto_pull": True,
                    "slots": {
                        "researchers": {"count": 1, "category": RESEARCHER},
                    },
                }
            }
        },
    }
    row.update(over)
    return row


def _db(*, claims=None, slot_claims=None, locked_claim_at=None):
    """A doubled PostgresDB.

    ``locked_claim_at`` is what the in-transaction re-read of the claim ledger
    returns — the racing-replica case. None means "still unclaimed".
    """
    db = AsyncMock()
    db.get_officer_capacity_lineage.return_value = [
        "11111111-1111-1111-1111-111111111111"
    ]
    db.newest_ticket_claims.return_value = claims or {}
    db.list_officer_slot_claims.return_value = slot_claims or []
    db.merge_thread_officer_state.return_value = True

    created = {}

    async def _create_job(**kwargs):
        created.update(kwargs)
        return {"id": str(uuid.uuid4()), **kwargs}

    db.create_job.side_effect = _create_job
    db.created = created

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=locked_claim_at)
    conn.fetch = AsyncMock(return_value=[])
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    db.acquire = MagicMock(return_value=acq)
    return db


def _vector_db(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[rows, []])
    conn.fetchrow = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    vector_db = MagicMock()
    vector_db.acquire = MagicMock(return_value=acq)
    return vector_db


class TestTickOfficer:
    @pytest.mark.asyncio
    async def test_dispatches_an_armed_ticket_with_the_category_contract(self):
        db = _db()
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(
            db, _vector_db(rows), _officer_row(), now=NOW, notify=None
        )

        assert counts["dispatched"] == 1
        created = db.created
        assert created["context"]["ticket_note_id"] == "feature-a"
        assert created["context"]["work_category"] == RESEARCHER
        assert created["context"]["officer_slot"] == "researchers"
        # The contract rides the kickoff, never `instructions` — that parameter
        # replaces the rendered instructions.md template wholesale.
        assert (
            "ANSWER, not a product increment" in created["context"]["kickoff_message"]
        )
        assert "instructions" not in created
        # No expert pin on the ticket -> the category default.
        assert created["config_name"] == "scholar"
        # The INSERT shares the caller's transaction, or the advisory lock is
        # gone before the row lands.
        assert created["conn"] is not None

    @pytest.mark.asyncio
    async def test_the_autonomy_exemption_rides_config_not_context(self):
        # Both grant PEPs read config_override. In context it would be inert,
        # and the completion would park in the pending_review dead zone holding
        # its claim.
        db = _db()
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        await tick_officer(db, _vector_db(rows), _officer_row(), now=NOW)
        assert db.created["config_override"]["autonomy"] == "full"
        assert "autonomy" not in db.created["context"]

    @pytest.mark.asyncio
    async def test_runner_kind_is_a_value_the_check_constraint_accepts(self):
        # jobs_runner_kind_check allows user | lifecycle | service. `lifecycle`
        # is also the class whose grants carry the full autonomy ceiling the
        # stamp above depends on — a mocked DB would never have caught either.
        from pathlib import Path

        db = _db()
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        await tick_officer(db, _vector_db(rows), _officer_row(), now=NOW)
        assert db.created["runner_kind"] == "lifecycle"

        schema = Path("orchestrator/database/schema_current.sql").read_text()
        constraint = next(
            line for line in schema.splitlines() if "jobs_runner_kind_check" in line
        )
        assert f"'{db.created['runner_kind']}'::text" in constraint

    @pytest.mark.asyncio
    async def test_a_claim_landing_under_the_lock_is_a_quiet_skip(self):
        # Two replicas ticking together is normal contention, not an incident.
        # The unique index would refuse the second INSERT anyway; re-reading the
        # ledger under the lock turns a stack trace into a log line.
        db = _db(locked_claim_at=NOW)
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), _officer_row(), now=NOW)
        assert counts["dispatched"] == 0
        assert counts["skipped"] == 1
        db.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_claim_under_the_lock_does_not_block_a_re_armed_ticket(self):
        # The claim predates this arming, so the officer has re-readied since.
        db = _db(locked_claim_at=NOW - timedelta(hours=2))
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), _officer_row(), now=NOW)
        assert counts["dispatched"] == 1

    @pytest.mark.asyncio
    async def test_a_refused_grant_skips_the_pool_without_tripping_the_breaker(self):
        # The officer's kit exceeding what the owner may be granted is a real
        # refusal, but nothing ran — it is not a job failure.
        db = _db()

        async def _grants(config_override, *, user_id, project_ids):
            raise RuntimeError("GrantDenied: model not granted")

        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(
            db,
            _vector_db(rows),
            _officer_row(user_id=str(uuid.uuid4())),
            now=NOW,
            enforce_grants=_grants,
        )
        assert counts["dispatched"] == 0
        assert counts["skipped"] == 1
        assert counts["breakers_opened"] == 0

    @pytest.mark.asyncio
    async def test_an_expert_pin_overrides_the_category_default(self):
        db = _db()
        rows = [
            _row(
                "feature-a",
                tags=["ready", "category:researcher", "expert:designer"],
                ready_at=NOW,
            )
        ]
        await tick_officer(db, _vector_db(rows), _officer_row(), now=NOW)
        assert db.created["config_name"] == "designer"

    @pytest.mark.asyncio
    async def test_auto_pull_off_dispatches_nothing(self):
        db = _db()
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["auto_pull"] = False
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), row, now=NOW)
        assert counts["dispatched"] == 0
        db.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_held_officer_dispatches_nothing(self):
        # Conference fence: the meeting may be revising the direction this
        # dispatch would act on.
        db = _db()
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["hold"] = True
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), row, now=NOW)
        assert counts["dispatched"] == 0
        db.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_open_breaker_skips_only_its_own_pool(self):
        db = _db()
        row = _officer_row()
        row["metadata"]["officer_state"] = {
            "backlog_breakers": {
                "researchers": {"until": (NOW + timedelta(minutes=5)).isoformat()}
            }
        }
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), row, now=NOW)
        assert counts["dispatched"] == 0
        assert counts["skipped"] == 1

    @pytest.mark.asyncio
    async def test_a_kb_outage_skips_cleanly_without_touching_the_breaker(self):
        db = _db()
        vector_db = MagicMock()
        vector_db.acquire = MagicMock(side_effect=RuntimeError("pgvector down"))
        counts = await tick_officer(db, vector_db, _officer_row(), now=NOW)
        assert counts["dispatched"] == 0
        assert counts["breakers_opened"] == 0

    @pytest.mark.asyncio
    async def test_a_floor_breach_wakes_the_officer_once_per_window(self):
        db = _db()
        woken = []

        async def _notify(_db, project_id, *, source, dedup_key, payload=None):
            woken.append((source, dedup_key, payload))
            return True

        # 1-slot pool, zero ready tickets -> below its floor.
        counts = await tick_officer(
            db, _vector_db([]), _officer_row(), now=NOW, notify=_notify
        )
        assert counts["wakes"] == 1
        assert woken[0][0] == "backlog_floor_breach"
        assert woken[0][2]["floor"] == 1 and woken[0][2]["ready"] == 0

    @pytest.mark.asyncio
    async def test_the_floor_wake_is_debounced(self):
        db = _db()
        woken = []

        async def _notify(_db, project_id, *, source, dedup_key, payload=None):
            woken.append(source)
            return True

        row = _officer_row()
        row["metadata"]["officer_state"] = {
            "backlog_floor_wakes": {
                "researchers": (NOW - timedelta(hours=1)).isoformat()
            }
        }
        counts = await tick_officer(db, _vector_db([]), row, now=NOW, notify=_notify)
        assert counts["wakes"] == 0
        assert woken == []

    @pytest.mark.asyncio
    async def test_grants_are_enforced_against_the_post_owner(self):
        # The internal spawn path bypasses the endpoint's PEP, so a slot that
        # pins a VM backend must still be checked explicitly.
        db = _db()
        seen = {}

        async def _grants(config_override, *, user_id, project_ids):
            seen["user_id"] = user_id
            seen["project_ids"] = project_ids

        row = _officer_row(user_id="00000000-0000-0000-0000-0000000000aa")
        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        await tick_officer(db, _vector_db(rows), row, now=NOW, enforce_grants=_grants)
        assert seen["user_id"] == "00000000-0000-0000-0000-0000000000aa"
        assert seen["project_ids"] == [row["project_id"]]

    @pytest.mark.asyncio
    async def test_a_failed_repo_provision_seals_the_job_and_does_not_count(self):
        db = _db()

        async def _provision(_job):
            raise RuntimeError("gitea down")

        rows = [_row("feature-a", tags=["ready", "category:researcher"], ready_at=NOW)]
        counts = await tick_officer(
            db, _vector_db(rows), _officer_row(), now=NOW, provision_repo=_provision
        )
        assert counts["dispatched"] == 0
        assert counts["skipped"] == 1
        db.update_job_status.assert_awaited()

    @pytest.mark.asyncio
    async def test_state_is_persisted_to_officer_state_not_config(self):
        # Live counters belong on the runtime plane; config_override.officer
        # flows into config resolution on every dispatch.
        db = _db()
        await tick_officer(db, _vector_db([]), _officer_row(), now=NOW)
        db.merge_thread_officer_state.assert_awaited()
        patch = db.merge_thread_officer_state.await_args.args[1]
        assert "backlog_stale_claims" in patch


class TestExecutorSerialization:
    @pytest.mark.asyncio
    async def test_a_live_executor_blocks_the_next_one(self):
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["slots"] = {
            "executors": {"count": 2, "category": EXECUTOR}
        }
        db = _db(
            slot_claims=[
                {
                    "id": str(uuid.uuid4()),
                    "status": "processing",
                    "work_category": EXECUTOR,
                    "ticket_note_id": "feature-old",
                    "updated_at": NOW,
                    "created_at": NOW,
                }
            ]
        )
        rows = [_row("feature-a", tags=["ready", "category:executor"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), row, now=NOW)
        assert counts["dispatched"] == 0
        db.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_PAUSED_executor_still_holds_the_singleton(self):
        # The predicate that matters: if a paused job released its slot, its
        # redispatch would race a second executor into the same story.
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["slots"] = {
            "executors": {"count": 2, "category": EXECUTOR}
        }
        db = _db(
            slot_claims=[
                {
                    "id": str(uuid.uuid4()),
                    "status": "paused",
                    "work_category": EXECUTOR,
                    "ticket_note_id": "feature-old",
                    "updated_at": NOW,
                    "created_at": NOW,
                }
            ]
        )
        rows = [_row("feature-a", tags=["ready", "category:executor"], ready_at=NOW)]
        counts = await tick_officer(db, _vector_db(rows), row, now=NOW)
        assert counts["dispatched"] == 0

    @pytest.mark.asyncio
    async def test_an_undispositioned_previous_ticket_blocks_the_next_executor(self):
        # Terminal is not the same as reviewed. The deliverable gate checks
        # that files exist, never what is in them, so without this an executor
        # chain builds on unreviewed work and review debt compounds straight
        # into the deliverable.
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["slots"] = {
            "executors": {"count": 1, "category": EXECUTOR}
        }
        finished = {
            "id": str(uuid.uuid4()),
            "status": "completed",
            "work_category": EXECUTOR,
            "ticket_note_id": "feature-old",
            "created_at": NOW - timedelta(hours=2),
            "updated_at": NOW - timedelta(hours=1),
        }
        db = _db()

        async def _claims(_lineage, *, slot=None, include_terminal=False, limit=20):
            return [finished] if include_terminal else []

        db.list_officer_slot_claims.side_effect = _claims

        # The old ticket is still active and was NOT re-armed after dispatch.
        vector_db = _vector_db(
            [_row("feature-a", tags=["ready", "category:executor"], ready_at=NOW)]
        )
        old_ticket = {
            "note_id": "feature-old",
            "status": "active",
            "ready_at": NOW - timedelta(hours=3),
            "tags": ["category:executor"],
        }
        vector_db.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value=old_ticket
        )

        counts = await tick_officer(db, vector_db, row, now=NOW)
        assert counts["dispatched"] == 0
        db.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_closed_previous_ticket_releases_the_executor_lane(self):
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["slots"] = {
            "executors": {"count": 1, "category": EXECUTOR}
        }
        finished = {
            "id": str(uuid.uuid4()),
            "status": "completed",
            "work_category": EXECUTOR,
            "ticket_note_id": "feature-old",
            "created_at": NOW - timedelta(hours=2),
            "updated_at": NOW - timedelta(hours=1),
        }
        db = _db()

        async def _claims(_lineage, *, slot=None, include_terminal=False, limit=20):
            return [finished] if include_terminal else []

        db.list_officer_slot_claims.side_effect = _claims

        vector_db = _vector_db(
            [_row("feature-a", tags=["ready", "category:executor"], ready_at=NOW)]
        )
        vector_db.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={"note_id": "feature-old", "status": "resolved"}
        )

        counts = await tick_officer(db, vector_db, row, now=NOW)
        assert counts["dispatched"] == 1

    @pytest.mark.asyncio
    async def test_a_re_readied_previous_ticket_also_releases_the_lane(self):
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["slots"] = {
            "executors": {"count": 1, "category": EXECUTOR}
        }
        finished = {
            "id": str(uuid.uuid4()),
            "status": "completed",
            "work_category": EXECUTOR,
            "ticket_note_id": "feature-old",
            "created_at": NOW - timedelta(hours=2),
            "updated_at": NOW - timedelta(hours=1),
        }
        db = _db()

        async def _claims(_lineage, *, slot=None, include_terminal=False, limit=20):
            return [finished] if include_terminal else []

        db.list_officer_slot_claims.side_effect = _claims

        vector_db = _vector_db(
            [_row("feature-a", tags=["ready", "category:executor"], ready_at=NOW)]
        )
        vector_db.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "note_id": "feature-old",
                "status": "active",
                # Re-armed AFTER the previous job was created = reviewed.
                "ready_at": NOW - timedelta(minutes=30),
            }
        )

        counts = await tick_officer(db, vector_db, row, now=NOW)
        assert counts["dispatched"] == 1

    @pytest.mark.asyncio
    async def test_parallel_safe_exempts_a_ticket_from_the_singleton(self):
        row = _officer_row()
        row["metadata"]["config_override"]["officer"]["slots"] = {
            "executors": {"count": 2, "category": EXECUTOR}
        }
        db = _db(
            slot_claims=[
                {
                    "id": str(uuid.uuid4()),
                    "status": "processing",
                    "work_category": EXECUTOR,
                    "ticket_note_id": "feature-old",
                    "updated_at": NOW,
                    "created_at": NOW,
                }
            ]
        )
        rows = [
            _row(
                "feature-a",
                tags=["ready", "category:executor", "parallel-safe"],
                ready_at=NOW,
            )
        ]
        counts = await tick_officer(db, _vector_db(rows), row, now=NOW)
        assert counts["dispatched"] == 1
