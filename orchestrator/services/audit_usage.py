"""Materialize LLM token usage from the audit trail → ``usage_events``.

In-process replacement for the LiteLLM spend-log materializer now that the
gateway is gone (``docs/issues/remove_litellm_proxy_and_gateway_concept.md``, P1
Slice 2). Where :func:`~services.litellm_gateway.materialize_llm_usage` pulled
prompt/completion tokens from the proxy's ``/spend/logs``, this reads the same
numbers straight from the auditdb ``llm_requests`` table the agent already writes
for *every* LLM call — worker jobs AND persistent sessions — so metering no
longer depends on routing traffic through a proxy.

Token source per row (extracted server-side as text via ``->>`` so this does not
depend on the read pool carrying a jsonb codec):
- worker rows: ``metrics.token_usage`` — the provider usage block
  (``prompt_tokens``/``completion_tokens``, or ``input_tokens``/``output_tokens``
  on Anthropic-wire).
- session rows (``agent_type='persistent'``): the normalized counts the
  persistent loop stashes in ``metadata.input_tokens`` / ``metadata.output_tokens``
  (streaming providers often leave ``response_metadata.token_usage`` empty).

Cost: none is written here. Two cost-free ``prompt-token`` / ``completion-token``
rows are emitted per call; :meth:`UsageLedger.record_events` snapshots the
$/token rate from ``usage_rates`` (seeded from OpenRouter — see
``openrouter_pricing.py``) exactly as it priced the gateway rows. Reasoning
tokens are a *subset* of the completion total (billed at the completion rate), so
they ride in ``details`` for observability, never as a separate priced row (that
would double-count cost).

Attribution: ``llm_requests.job_id`` is a job id for worker rows and a thread id
for session rows. Resolved against the app-DB ``jobs`` / ``threads`` tables to
``user_id`` / ``project_id`` (soft — a deleted job's tokens still meter, just
unattributed) and carried as ``ref_kind`` / ``ref_id`` for per-job/thread cost.

Cursor: ``llm_requests.id`` is a single-sequence, append-only BIGINT — a gap-free
integer cursor. To dodge the assign-before-commit reorder window (id ``N+1``
visible before id ``N`` commits), the cursor only advances over a *contiguous*
run of rows older than ``min_age_s``; the first too-fresh row stops the tick (its
id, and everything after it, waits for a later tick). Idempotent regardless — the
ledger dedupes on ``(source, source_id, unit, ts)`` with ``source_id`` = the row
id — so a re-scan never double-counts. ``source='audit'`` keeps these rows in a
distinct idempotency namespace from the retired ``source='litellm'`` rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import asyncpg

from services.usage_ledger import UsageEvent, UsageLedger

logger = logging.getLogger(__name__)

# agent_type value the persistent loop writes; discriminates session rows (whose
# job_id is a thread id) from worker rows (whose job_id is a job id).
_SESSION_AGENT_TYPE = "persistent"

_DEFAULT_BATCH = 1000
# Aging window (seconds) a row must clear before its id may advance the cursor —
# see the module docstring's "assign-before-commit" note.
_DEFAULT_MIN_AGE_S = 60.0

# Pull the token counts server-side as TEXT (``->>``), independent of any jsonb
# codec on the read pool. The fallback order per dimension is applied in Python
# (:func:`_first_int`): worker's metrics.token_usage first, then session's
# metadata.*_tokens. metrics is NOT NULL DEFAULT '{}'; metadata may be NULL.
_SELECT_SQL = """
SELECT id, job_id, agent_type, call_type, model, timestamp,
       metrics->'token_usage'->>'prompt_tokens'     AS m_prompt,
       metrics->'token_usage'->>'input_tokens'      AS m_input,
       metrics->'token_usage'->>'completion_tokens' AS m_completion,
       metrics->'token_usage'->>'output_tokens'     AS m_output,
       metrics->'token_usage'->>'reasoning_tokens'  AS m_reasoning,
       metadata->>'input_tokens'                    AS md_input,
       metadata->>'output_tokens'                   AS md_output
FROM llm_requests
WHERE id > $1
ORDER BY id
LIMIT $2
"""


def _first_int(*vals: Any) -> int:
    """First positive integer among ``vals`` (text/None), else 0.

    The ``->>`` extracts arrive as strings ("1234", occasionally "1234.0");
    tolerate both and skip absent / non-numeric / non-positive entries so the
    per-dimension fallback chain moves to the next candidate.
    """
    for v in vals:
        if v is None:
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 0


async def _resolve_owners(
    app_pool: asyncpg.Pool,
    job_ids: Iterable[Any],
    thread_ids: Iterable[Any],
) -> dict[Any, tuple[Any, Any]]:
    """Map ``{id: (user_id, project_id)}`` for the batch's job + thread ids.

    Two batched app-DB reads (jobs, threads). Non-fatal: an attribution failure
    logs and returns what resolved so far — the tokens still meter, just
    unattributed — rather than dropping the tick's usage.
    """
    owners: dict[Any, tuple[Any, Any]] = {}
    job_ids = list(job_ids)
    thread_ids = list(thread_ids)
    try:
        async with app_pool.acquire() as conn:
            if job_ids:
                for r in await conn.fetch(
                    "SELECT id, user_id, project_id FROM jobs "
                    "WHERE id = ANY($1::uuid[])",
                    job_ids,
                ):
                    owners[r["id"]] = (r["user_id"], r["project_id"])
            if thread_ids:
                for r in await conn.fetch(
                    "SELECT id, user_id, project_id FROM threads "
                    "WHERE id = ANY($1::uuid[])",
                    thread_ids,
                ):
                    owners[r["id"]] = (r["user_id"], r["project_id"])
    except Exception:
        logger.warning("usage attribution lookup failed (non-fatal)", exc_info=True)
    return owners


async def materialize_llm_usage_from_audit(
    audit_pool: Optional[asyncpg.Pool],
    app_pool: Optional[asyncpg.Pool],
    ledger: Optional[UsageLedger],
    *,
    since_id: int = 0,
    batch_limit: int = _DEFAULT_BATCH,
    min_age_s: float = _DEFAULT_MIN_AGE_S,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Materialize ``llm_requests`` rows above ``since_id`` into ``usage_events``.

    Emits cost-free ``prompt-token`` / ``completion-token`` rows (the ledger
    prices them from ``usage_rates``), attributed to the row's user/project.
    Advances the cursor only over a contiguous run of rows older than
    ``min_age_s`` (see module docstring). Returns
    ``{"materialized", "cursor", "scanned"}``. Non-fatal: no-ops (cursor
    unchanged) when any pool or the ledger is unavailable.
    """
    if (
        audit_pool is None
        or app_pool is None
        or ledger is None
        or not ledger.is_available
    ):
        return {"materialized": 0, "cursor": since_id, "scanned": 0}

    async with audit_pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_SQL, since_id, batch_limit)
    if not rows:
        return {"materialized": 0, "cursor": since_id, "scanned": 0}

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=min_age_s)

    # Contiguous aged prefix: stop at the first row newer than the cutoff so the
    # cursor never advances past a lower id that may still be mid-commit.
    aged = []
    for r in rows:
        if r["timestamp"] > cutoff:
            break
        aged.append(r)
    if not aged:
        return {"materialized": 0, "cursor": since_id, "scanned": len(rows)}

    job_ids = {
        r["job_id"] for r in aged if (r["agent_type"] or "") != _SESSION_AGENT_TYPE
    }
    thread_ids = {
        r["job_id"] for r in aged if (r["agent_type"] or "") == _SESSION_AGENT_TYPE
    }
    owners = await _resolve_owners(app_pool, job_ids, thread_ids)

    events: list[UsageEvent] = []
    new_cursor = since_id
    for r in aged:
        new_cursor = r["id"]  # advance over every aged row, priced or not
        prompt = _first_int(r["m_prompt"], r["m_input"], r["md_input"])
        completion = _first_int(r["m_completion"], r["m_output"], r["md_output"])
        if not prompt and not completion:
            continue  # health check / audio / errored call — nothing to meter
        is_session = (r["agent_type"] or "") == _SESSION_AGENT_TYPE
        user_id, project_id = owners.get(r["job_id"], (None, None))
        reasoning = _first_int(r["m_reasoning"])
        details: dict[str, Any] = {
            "model": str(r["model"]),
            "llm_request_id": r["id"],
            "agent_type": r["agent_type"],
            "call_type": r["call_type"],
        }
        if reasoning:
            details["reasoning_tokens"] = reasoning
        common = dict(
            category="llm",
            resource=str(r["model"]),
            source="audit",
            source_id=str(r["id"]),
            ts=r["timestamp"],
            user_id=user_id,
            project_id=project_id,
            ref_kind="thread" if is_session else "job",
            ref_id=str(r["job_id"]),
            details=details,
        )
        if prompt:
            events.append(UsageEvent(quantity=prompt, unit="prompt-token", **common))
        if completion:
            events.append(
                UsageEvent(quantity=completion, unit="completion-token", **common)
            )

    inserted = await ledger.record_events(events)
    if inserted:
        logger.info(
            "LLM usage (audit): materialized %d ledger row(s) from %d audit row(s)",
            inserted,
            len(aged),
        )
    return {"materialized": inserted, "cursor": new_cursor, "scanned": len(rows)}


__all__ = ["materialize_llm_usage_from_audit"]
