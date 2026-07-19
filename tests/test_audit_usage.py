"""Unit tests for the audit-trail → usage_events LLM materializer.

Covers token extraction from both homes (worker ``metrics.token_usage`` vs
session ``metadata.*_tokens``), the Anthropic-wire input/output fallback,
user/project attribution (jobs vs threads), the reasoning-not-double-counted
rule, and the timestamp-cursor + contiguous-aging guard — all against fake
asyncpg pools and a capturing ledger (no network / DB).

The cursor is a ``timestamp`` (not ``id``): ``llm_requests.id`` is not monotonic
with insertion time once the audit sequence has been reset, so these tests order
and advance on ``timestamp`` and keep ``id`` only as the unique dedupe key.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.services.audit_usage import (
    _first_int,
    materialize_llm_usage_from_audit,
)

USER = uuid.uuid4()
PROJ = uuid.uuid4()
JOB = uuid.uuid4()
THREAD = uuid.uuid4()

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 1, 12, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 1, 12, 2, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 1, 12, 5, tzinfo=timezone.utc)  # 5 min after T0


def _row(
    id,
    *,
    job_id=JOB,
    agent_type="scholar",
    call_type="main",
    model="minimax-m3",
    ts=T0,
    m_prompt=None,
    m_input=None,
    m_completion=None,
    m_output=None,
    m_reasoning=None,
    m_cached=None,
    m_cached_norm=None,
    um_input=None,
    um_output=None,
    um_reasoning=None,
    md_input=None,
    md_output=None,
    md_cached=None,
):
    """Build one ``llm_requests`` result row (as the SELECT's ``->>`` text cols)."""
    return {
        "id": id,
        "job_id": job_id,
        "agent_type": agent_type,
        "call_type": call_type,
        "model": model,
        "timestamp": ts,
        "m_prompt": m_prompt,
        "m_input": m_input,
        "m_completion": m_completion,
        "m_output": m_output,
        "m_reasoning": m_reasoning,
        "m_cached": m_cached,
        "m_cached_norm": m_cached_norm,
        "um_input": um_input,
        "um_output": um_output,
        "um_reasoning": um_reasoning,
        "md_input": md_input,
        "md_output": md_output,
        "md_cached": md_cached,
    }


# --- fake asyncpg pools + ledger --------------------------------------------


class AuditConn:
    def __init__(self, rows):
        self.rows = list(rows)

    async def fetch(self, _sql, since_ts, limit):
        # Mirror: WHERE timestamp > $1 ORDER BY timestamp, id LIMIT $2
        out = [r for r in self.rows if r["timestamp"] > since_ts]
        out.sort(key=lambda r: (r["timestamp"], r["id"]))
        return out[:limit]


class AppConn:
    """Answers the jobs / threads owner lookups from in-memory maps."""

    def __init__(self, jobs=None, threads=None):
        self.jobs = jobs or {}
        self.threads = threads or {}

    async def fetch(self, sql, ids):
        table = self.jobs if "FROM jobs" in sql else self.threads
        return [
            {"id": i, "user_id": table[i][0], "project_id": table[i][1]}
            for i in ids
            if i in table
        ]


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class FakeLedger:
    def __init__(self, available=True):
        self._available = available
        self.events = []

    @property
    def is_available(self):
        return self._available

    async def record_events(self, events):
        self.events.extend(events)
        return len(events)


# --- _first_int -------------------------------------------------------------


class TestFirstInt:
    def test_first_positive_wins(self):
        assert _first_int("100", "200") == 100

    def test_float_string_and_fallthrough(self):
        # "0" is skipped (not positive) → next candidate; "1234.0" tolerated.
        assert _first_int("0", "1234.0") == 1234

    def test_none_and_garbage_and_negative(self):
        assert _first_int(None, "abc", "-5") == 0
        assert _first_int() == 0


# --- materialize_llm_usage_from_audit ---------------------------------------


class TestMaterialize:
    @pytest.mark.asyncio
    async def test_worker_row_emits_both_token_dimensions(self):
        audit = FakePool(AuditConn([_row(1, ts=T0, m_prompt="100", m_completion="40")]))
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, since_ts=None, min_age_s=0, now=NOW
        )
        assert res == {"materialized": 2, "cursor": T0, "scanned": 1}
        by_unit = {e.unit: e for e in ledger.events}
        assert by_unit["prompt-token"].quantity == 100
        assert by_unit["completion-token"].quantity == 40
        e = by_unit["prompt-token"]
        assert e.category == "llm" and e.resource == "minimax-m3"
        assert e.source == "audit" and e.source_id == "1"
        assert e.ref_kind == "job" and e.ref_id == str(JOB)
        assert e.user_id == USER and e.project_id == PROJ
        assert e.rate_usd is None and e.cost_usd is None  # ledger prices it

    @pytest.mark.asyncio
    async def test_session_row_uses_metadata_tokens_and_thread_attr(self):
        row = _row(
            5,
            job_id=THREAD,
            agent_type="persistent",
            md_input="200",
            md_output="80",
        )
        audit = FakePool(AuditConn([row]))
        app = FakePool(AppConn(threads={THREAD: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 2
        by_unit = {e.unit: e for e in ledger.events}
        assert by_unit["prompt-token"].quantity == 200
        assert by_unit["completion-token"].quantity == 80
        assert by_unit["prompt-token"].ref_kind == "thread"
        assert by_unit["prompt-token"].ref_id == str(THREAD)
        assert by_unit["prompt-token"].user_id == USER

    @pytest.mark.asyncio
    async def test_anthropic_wire_input_output_fallback(self):
        # metrics.token_usage carries input_tokens/output_tokens (no prompt/completion).
        audit = FakePool(AuditConn([_row(2, m_input="70", m_output="30")]))
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        await materialize_llm_usage_from_audit(audit, app, ledger, min_age_s=0, now=NOW)
        by_unit = {e.unit: e.quantity for e in ledger.events}
        assert by_unit == {"prompt-token": 70, "completion-token": 30}

    @pytest.mark.asyncio
    async def test_cached_prompt_tokens_split_from_uncached_prompt(self):
        audit = FakePool(
            AuditConn([_row(9, m_prompt="1000", m_cached="750", m_completion="40")])
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 3
        by_unit = {e.unit: e for e in ledger.events}
        assert by_unit["prompt-token"].quantity == 250
        assert by_unit["cached-prompt-token"].quantity == 750
        assert by_unit["completion-token"].quantity == 40
        assert by_unit["cached-prompt-token"].details["cached_prompt_tokens"] == 750

    @pytest.mark.asyncio
    async def test_cached_prompt_tokens_clamp_at_prompt_total(self):
        audit = FakePool(AuditConn([_row(10, m_prompt="100", m_cached="150")]))
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 1
        assert [(e.unit, e.quantity) for e in ledger.events] == [
            ("cached-prompt-token", 100)
        ]

    @pytest.mark.asyncio
    async def test_cached_from_normalized_usage_metadata_responses_api(self):
        # Codex / gpt-5.x on the Responses API: response_metadata carries no
        # token_usage, so prompt+cached come from LangChain's normalized
        # metrics.usage_metadata (m_input + m_cached_norm), never m_cached.
        audit = FakePool(
            AuditConn(
                [
                    _row(
                        11,
                        model="gpt-5.5",
                        m_input="1000",
                        m_output="40",
                        m_cached_norm="600",
                    )
                ]
            )
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 3
        by_unit = {e.unit: e.quantity for e in ledger.events}
        assert by_unit["prompt-token"] == 400
        assert by_unit["cached-prompt-token"] == 600
        assert by_unit["completion-token"] == 40

    @pytest.mark.asyncio
    async def test_codex_worker_row_tokens_only_in_usage_metadata(self):
        # Real codex/gpt-5.x WORKER-job shape (job c3fc9128, 07-18): the
        # Responses API leaves metrics.token_usage = {} and metadata NULL —
        # every count lives ONLY in LangChain-normalized metrics.usage_metadata
        # (input_tokens / output_tokens / output_token_details.reasoning, with
        # cached already covered by m_cached_norm). Before the um_* fallbacks
        # these rows resolved to 0/0 and were silently dropped as "nothing to
        # meter" — no codex job ever reached the ledger.
        audit = FakePool(
            AuditConn(
                [
                    _row(
                        13,
                        model="gpt-5.6-sol",
                        um_input="223567",
                        um_output="838",
                        um_reasoning="376",
                        m_cached_norm="7680",
                    )
                ]
            )
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 3
        by_unit = {e.unit: e for e in ledger.events}
        assert by_unit["prompt-token"].quantity == 215887  # 223567 - 7680 cached
        assert by_unit["cached-prompt-token"].quantity == 7680
        assert by_unit["completion-token"].quantity == 838
        assert by_unit["prompt-token"].details["reasoning_tokens"] == 376
        assert by_unit["prompt-token"].ref_kind == "job"

    @pytest.mark.asyncio
    async def test_cached_from_session_metadata(self):
        # Persistent session turn: tokens live in metadata (md_*), including the
        # newly-threaded md_cached.
        row = _row(
            12,
            job_id=THREAD,
            agent_type="persistent",
            model="gpt-5.5",
            md_input="500",
            md_output="30",
            md_cached="350",
        )
        audit = FakePool(AuditConn([row]))
        app = FakePool(AppConn(threads={THREAD: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 3
        by_unit = {e.unit: e.quantity for e in ledger.events}
        assert by_unit["prompt-token"] == 150
        assert by_unit["cached-prompt-token"] == 350
        assert by_unit["completion-token"] == 30

    @pytest.mark.asyncio
    async def test_reasoning_tokens_in_details_not_a_third_row(self):
        audit = FakePool(
            AuditConn([_row(3, m_prompt="100", m_completion="60", m_reasoning="25")])
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        # Reasoning is a subset of completion — only 2 priced rows, never a 3rd.
        assert res["materialized"] == 2
        assert all(e.unit != "reasoning-token" for e in ledger.events)
        assert ledger.events[0].details["reasoning_tokens"] == 25

    @pytest.mark.asyncio
    async def test_zero_token_row_skipped_but_cursor_advances(self):
        audit = FakePool(
            AuditConn(
                [
                    _row(7, ts=T0, m_prompt="0", m_completion="0"),  # nothing to meter
                    _row(8, ts=T1, m_prompt="10", m_completion="5"),
                ]
            )
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["cursor"] == T1  # advanced past the empty row's timestamp
        assert res["materialized"] == 2  # only row 8's two dimensions

    @pytest.mark.asyncio
    async def test_fresh_row_halts_contiguous_advance(self):
        fresh = NOW - timedelta(seconds=30)  # newer than cutoff (min_age 60)
        # id deliberately non-monotonic with ts to prove the cursor is ts-keyed.
        audit = FakePool(
            AuditConn(
                [
                    _row(999, ts=T0, m_prompt="10", m_completion="5"),  # aged
                    _row(2, ts=fresh, m_prompt="10", m_completion="5"),  # fresh → halt
                    _row(1, ts=NOW, m_prompt="10", m_completion="5"),  # fresher
                ]
            )
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=60, now=NOW
        )
        assert res["cursor"] == T0  # stopped at the first fresh row
        assert res["materialized"] == 2  # only the aged row metered
        assert res["scanned"] == 3

    @pytest.mark.asyncio
    async def test_attribution_miss_still_meters_unattributed(self):
        audit = FakePool(AuditConn([_row(4, m_prompt="10", m_completion="5")]))
        app = FakePool(AppConn(jobs={}))  # JOB absent → no owner
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, min_age_s=0, now=NOW
        )
        assert res["materialized"] == 2
        e = ledger.events[0]
        assert e.user_id is None and e.project_id is None
        assert e.ref_id == str(JOB)  # ref still recorded

    @pytest.mark.asyncio
    async def test_since_ts_filters_processed_rows(self):
        audit = FakePool(
            AuditConn(
                [
                    _row(1, ts=T0, m_prompt="9", m_completion="9"),
                    _row(2, ts=T1, m_prompt="10", m_completion="5"),
                ]
            )
        )
        app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        ledger = FakeLedger()
        res = await materialize_llm_usage_from_audit(
            audit, app, ledger, since_ts=T0, min_age_s=0, now=NOW
        )
        assert res["scanned"] == 1  # T0 row is at/below the cursor
        assert res["cursor"] == T1
        assert res["materialized"] == 2

    @pytest.mark.asyncio
    async def test_no_rows_keeps_cursor(self):
        audit = FakePool(AuditConn([]))
        app = FakePool(AppConn())
        res = await materialize_llm_usage_from_audit(
            audit, app, FakeLedger(), since_ts=T2, min_age_s=0, now=NOW
        )
        assert res == {"materialized": 0, "cursor": T2, "scanned": 0}

    @pytest.mark.asyncio
    async def test_unavailable_pools_or_ledger_are_noops(self):
        good_audit = FakePool(AuditConn([_row(1, m_prompt="1", m_completion="1")]))
        good_app = FakePool(AppConn(jobs={JOB: (USER, PROJ)}))
        noop = {"materialized": 0, "cursor": T2, "scanned": 0}
        assert (
            await materialize_llm_usage_from_audit(
                None, good_app, FakeLedger(), since_ts=T2
            )
            == noop
        )
        assert (
            await materialize_llm_usage_from_audit(
                good_audit, None, FakeLedger(), since_ts=T2
            )
            == noop
        )
        assert (
            await materialize_llm_usage_from_audit(
                good_audit, good_app, FakeLedger(available=False), since_ts=T2
            )
            == noop
        )
