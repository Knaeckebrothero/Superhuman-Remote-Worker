"""Attention-sleep watchdog: suspend idle ``awaiting_user`` thread workspaces.

Extracted from ``orchestrator.main`` (R1.B10). Owns the Phase 5 attention-sleep
sweeper and its environment-tuned constants. Database access and the thread
retirement authority are injected by application composition — this module
imports no application startup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from orchestrator.services.workspace_suspension import workspace_suspension_service
from shared.thread_presence import promote_expired_stateless_pauses

logger = logging.getLogger(__name__)

# =============================================================================
# Phase 5 — Attention sleep watchdog
# =============================================================================
#
# Suspends thread workspaces (and the bound agent pod) when the agent has
# been in `awaiting_user` for longer than HEADLESS_ATTENTION_SLEEP_MINUTES.
# State machine:
#   active ─→ awaiting_user (agent: natural pause + no WS subscriber)
#   awaiting_user ─→ suspended (this watchdog after TTL)
#   awaiting_user ─→ active (agent: subscriber reattach, clears timer)
#   suspended ─→ active (magic-link wake or REST reattach restores workspace)
#
# Magic-link "extend window" POSTs bump awaiting_user_since forward so the
# watchdog re-arms; threads.extend_count caps the bumps at 4 (4h total).
#
# Today's "tethered" signal is WS-only — Phase 5 v1 ships before the
# cockpit migrates from WS to SSE. SSE-only consumers (MCP, curl) do not
# block suspension; they should rely on magic-link wake to bring the
# session back. When cockpit moves to SSE, this watchdog will need to
# consult the orchestrator's in-process SSE attach registry too.


_ATTENTION_SLEEP_INTERVAL_S: int = int(
    os.environ.get("HEADLESS_ATTENTION_SLEEP_INTERVAL_S", "60")
)
_ATTENTION_SLEEP_MINUTES: int = int(
    os.environ.get("HEADLESS_ATTENTION_SLEEP_MINUTES", "60")
)


async def attention_sleep_sweeper(
    shutdown_event: asyncio.Event,
    *,
    db: Any,
    thread_retirement_operations: Callable[[], Any],
) -> None:
    """Background task: suspend threads stuck in awaiting_user past their TTL.

    Runs every HEADLESS_ATTENTION_SLEEP_INTERVAL_S (default 60s). Each
    qualifying pinned generation enters the same durable retirement funnel as
    owner End, settling to ``suspended`` only after generation-fenced staging
    and exact resource cleanup. Resume stays closed for the entire operation.

    Best-effort: a transient failure (DB unavailable, suspend service
    error) is logged and retried on the next tick.
    """
    interval_s = _ATTENTION_SLEEP_INTERVAL_S
    ttl_minutes = _ATTENTION_SLEEP_MINUTES
    logger.info(
        "Attention-sleep sweeper started (interval=%ds, ttl=%dmin)",
        interval_s,
        ttl_minutes,
    )

    while not shutdown_event.is_set():
        try:
            # A disconnect intentionally leaves a short TTL grace so reloads
            # and multi-tab handoffs never flicker. If a turn reached its
            # natural pause inside that grace, converge it once the queue is
            # durably done and the final client TTL has expired. This is
            # independent of workspace suspension being enabled.
            try:
                promoted = await promote_expired_stateless_pauses(db, limit=50)
                if promoted:
                    logger.info(
                        "presence expiry promoted %d stateless thread(s) "
                        "to awaiting_user",
                        len(promoted),
                    )
            except Exception as exc:
                # Presence convergence is additive. It must never suppress the
                # pre-existing awaiting_user suspension sweep on the same tick.
                logger.warning("presence expiry promotion failed: %s", exc)
            if workspace_suspension_service.is_enabled:
                async with db.acquire() as conn:
                    # Phase 6: per-thread TTL resolution. Priority order is
                    # (1) thread.metadata.config_override.headless overrides,
                    # (2) users.settings.persistent_agent overrides,
                    # (3) the global HEADLESS_ATTENTION_SLEEP_MINUTES default.
                    # ttl <= 0 disables the watchdog for that thread, matching
                    # the cockpit UX of "Never auto-suspend".
                    rows = await conn.fetch(
                        "SELECT t.id, t.status, t.execution_lane, "
                        "       t.runtime_generation, t.agent_id, "
                        "       t.runtime_attach_token "
                        "FROM threads t "
                        "LEFT JOIN users u ON u.id = t.user_id "
                        "WHERE t.status = 'awaiting_user' "
                        "  AND t.execution_lane <> 'stateless' "
                        "  AND t.awaiting_user_since IS NOT NULL "
                        # Officer sessions never sleep via attention-sleep —
                        # their lifecycle belongs to the officer watchdog
                        # (centurion.md §4). Belt-and-suspenders: the agent
                        # side already skips the awaiting_user flip for them.
                        "  AND COALESCE(t.metadata->'config_override'->'officer'"
                        "->>'enabled','false') <> 'true' "
                        "  AND COALESCE("
                        "    NULLIF(t.metadata->'config_override'->'headless'->>'attention_sleep_minutes', '')::int, "
                        "    NULLIF(u.settings->'persistent_agent'->>'headless_attention_sleep_minutes', '')::int, "
                        "    $1::int"
                        "  ) > 0 "
                        "  AND t.awaiting_user_since < now() - make_interval(mins => COALESCE("
                        "    NULLIF(t.metadata->'config_override'->'headless'->>'attention_sleep_minutes', '')::int, "
                        "    NULLIF(u.settings->'persistent_agent'->>'headless_attention_sleep_minutes', '')::int, "
                        "    $1::int"
                        "  )) "
                        "ORDER BY t.awaiting_user_since ASC "
                        "LIMIT 50",
                        int(ttl_minutes),
                    )

                for row in rows:
                    thread_id = str(row["id"])
                    try:
                        result = await thread_retirement_operations().end_thread_flow(
                            thread_id,
                            dict(row),
                            permanent=False,
                            force=False,
                            expected_runtime_generation=str(row["runtime_generation"]),
                            expected_agent_id=(
                                str(row["agent_id"])
                                if row["agent_id"] is not None
                                else None
                            ),
                            expected_attach_token=(
                                str(row["runtime_attach_token"])
                                if row["runtime_attach_token"] is not None
                                else None
                            ),
                            settle_status="suspended",
                        )
                        if result.get("status") == "suspended":
                            logger.info(
                                "attention-sleep: thread %s suspended (was "
                                "awaiting_user >%dm)",
                                thread_id,
                                ttl_minutes,
                            )
                        else:
                            logger.info(
                                "attention-sleep: exact retirement declined "
                                "for thread %s (%s)",
                                thread_id,
                                result.get("status"),
                            )
                    except Exception as e:
                        logger.warning(
                            "attention-sleep: suspend failed for thread %s: %s",
                            thread_id,
                            e,
                        )
        except Exception as e:
            logger.warning("attention-sleep sweep error: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Attention-sleep sweeper stopped")
