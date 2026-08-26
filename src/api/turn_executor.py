"""Stateless turn executor — the M3 claim loop (stateless_agents.md §5.1–§5.3).

One asyncio loop per pod: claim a ``session_turn`` unit from the shared
``run_queue`` (``src/shared/run_queue``), replay the thread through the
EXISTING persistent_app attach/restore machinery, drive exactly ONE turn by
injecting the oldest unanswered ``thread_messages`` row into the running
loop's user queue, and complete the unit with the consumed watermark. This
module is a DRIVER over ``persistent_app`` — it owns no session runtime of
its own (doc decision 4: maximum reuse of the pool attach/detach path).

Flow per claim (§5.1/§5.3.1):

1.  **Skip-if-answered** — ``consumed_seq >= input_seq`` at claim time means a
    predecessor answered but died before completing; complete immediately,
    never re-invoke the LLM.
2.  **Heartbeat** — an independent task renews the lease every
    ``HEARTBEAT_INTERVAL_SECONDS``; never an astream hook (a 10-minute tool
    call must not starve renewal). A failed renewal marks the shared
    :class:`~src.api.lease_context.LeaseHandle` lost.
3.  **Claim bundle** — resolved config + attach payload delivered only
    against proof of the current lease (§5.6;
    ``GET /internal/units/{unit_id}/claim-bundle?lease_token=N``).
4.  **Attach with affinity** — same thread + same attach fingerprint reuses
    the live session; anything else detaches, scrubs process residue
    (§5.6 scrub-on-claim), and attaches fresh.
5.  **Inject** — the oldest unanswered ``role='human'`` row (LangChain role
    vocabulary — implementation log M0b) goes onto the loop's user queue with
    its DB row id, so every later persist upserts onto the existing row.
6.  **Complete** — after the turn-complete hook fires (and the turn-end cloud
    push, if any, is awaited under the lease — §5.3.5 option (i)),
    ``complete_unit`` records the consumed watermark; 'queued' means more
    input already arrived and the unit re-enters the queue.

Torn-turn invariant (§5.2, stated where the doc requires it): sessions
rebuild from ``thread_messages`` + the consumed watermark alone; a
checkpoint-ahead-of-messages tear is converged by the next claim's rebuild,
and skip-if-answered prevents the double-answer.

Greppable log contract (M6 fault-injection greps for these):

* ``run_queue claim``      — a lease was obtained
* ``run_queue complete``   — a turn (or skip) completed a unit
* ``run_queue release``    — a claim was voluntarily released (error path)
* ``lease lost``           — heartbeat/fence/completion found the lease gone
* ``fence rejected``       — a fenced persist/flush was refused
  (emitted by ``postgres_db`` and the journal writer)

S1 acceptance — zero in-process claim state: between loop iterations the ONLY
state this executor carries is (a) the soft-affinity hint
(``_prefer_unit_id`` + the attach fingerprint of the cached session) and
(b) the attached-session cache inside persistent_app itself. Neither is
load-bearing: a pod restart forgets both and the next claim rebuilds
everything from Postgres (thread_messages + run_queue watermarks).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from uuid import UUID

from .lease_context import LeaseHandle, LeaseLostError, current_lease
from .models import JobStartRequest
from .orchestrator_client import ClaimBundleError, CompletionNonTerminalReportError
from ..shared.job_freeze_types import (
    AUTO_CONTINUE_FREEZE_TYPES,
    FREEZE_TYPE_BATCH_BOUNDARY,
)
from ..shared.run_queue import (
    HEARTBEAT_INTERVAL_SECONDS,
    UNIT_KIND_SESSION_TURN,
    ClaimedUnit,
    claim_unit,
    close_interrupt_admission,
    complete_unit,
    heartbeat_unit,
    open_interrupt_admission,
    release_unit,
)
from ..shared.session_retirement import acknowledge_session_claim_quiesced
from ..shared.worker_queue import (
    WorkerClaim,
    WorkerCompletionAcceptance,
    WorkerRenewal,
    claim_worker_batch,
    complete_worker_batch,
    get_worker_completion_acceptance,
    renew_worker_batch,
    release_worker_batch,
    rotate_worker_batch,
)

logger = logging.getLogger(__name__)

_COMPLETION_REPORT_PAYLOAD_FIELDS = (
    "should_stop",
    "goal_achieved",
    "error",
    "freeze_data",
)

# --- Tunables (env-overridable where deployment cares) -----------------------

IDLE_POLL_SECONDS = 0.5
IDLE_POLL_BACKOFF_SECONDS = 2.0
IDLE_POLLS_BEFORE_BACKOFF = 30
POLL_JITTER = 0.2  # ±20%
# How long a pod keeps an idle thread's session attached, hoping for the next
# turn (§5.3.4). The win it buys is the whole attach (measured 7.4s on k3d);
# the cost is one process's worth of session state — LLM clients, workspace
# backend, knowledge stores — held for a thread nobody is talking to. Well
# under the reaper's steal horizon, so an expired warm cache never masks a
# lease problem.
WARM_SESSION_IDLE_TTL_SECONDS = 300.0
CLOUD_PUSH_WAIT_SECONDS = 60.0  # §5.3.5 option (i): the lease covers the push
TURN_ABORT_GRACE_SECONDS = 15.0  # polite-unwind budget after an interrupt
COMPLETE_RETRY_ATTEMPTS = 3
PENDING_ROWS_LIMIT = 50
WORKER_FINALIZATION_POLL_SECONDS = 1.0

_AUDIT_WRITER_UNSET = object()

_WORKER_PRESERVE_SHELL_STATUSES = frozenset(
    {"paused", "pending_review", "reviewing", "waiting", "waiting_for_reply"}
)
_WORKER_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
_WORKER_UNFINISHED_COMMAND_STATES = frozenset({"pending", "finalizing"})
_WORKER_FINALIZED_COMMAND_STATES = frozenset({"done", "superseded", "force_resolved"})


def _enabled_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Oldest-unanswered query (§5.1 watermarks + M0b role vocabulary): LangChain
# roles ('human'/'ai'); rewound rows are dead timelines and must not be
# answered. ``seq > COALESCE(consumed_seq, -1)`` — a NULL consumed watermark
# means nothing was ever answered, so the oldest human row qualifies.
_PENDING_INPUT_SQL = """
    SELECT message.id, message.seq, message.content, message.turn_number,
           message.role, delivery.delivery_id
    FROM thread_messages AS message
    LEFT JOIN thread_input_deliveries AS delivery
      ON delivery.message_id = message.id
     AND delivery.thread_id = message.thread_id
    WHERE message.thread_id = $1
      AND message.rewound_at IS NULL
      AND (
          (message.role = 'human' AND message.seq > $2)
          OR
          (
              message.role = 'event'
              AND delivery.execution_lane = 'stateless'
              AND delivery.state IN ('persisted', 'queued', 'deferred')
          )
      )
    ORDER BY seq ASC
    LIMIT $3
"""

_PENDING_EVENT_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1
      FROM thread_input_deliveries AS delivery
      JOIN thread_messages AS message ON message.id = delivery.message_id
     WHERE delivery.thread_id = $1
       AND delivery.execution_lane = 'stateless'
       AND delivery.state IN ('persisted', 'queued', 'deferred')
       AND message.rewound_at IS NULL
)
"""


def _pa():
    """The persistent_app module, imported lazily (import-cycle guard)."""
    import src.api.persistent_app as pa

    return pa


def _message_text(msg: Any) -> str:
    """Best-effort text of a message's content (list content is flattened
    exactly like ``_serialize_message_row`` does at persist time, so a
    restored row's stored content compares equal)."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return content if isinstance(content, str) else str(content or "")


def attach_fingerprint(attach: Dict[str, Any]) -> str:
    """Stable, cheap fingerprint of a claim bundle's attach object (§5.3.4).

    sha256 over canonical JSON — any change in resolved config / datasources /
    project scope forces a re-attach; identical bundles reuse the live
    session. ``default=str`` keeps exotic values (UUIDs, datetimes) stable
    rather than raising.

    Two classes of delivery-only values are excluded before hashing:

    * ``resolved_config.resolved_at`` is a wall-clock stamp minted by every
      resolver call and consumed by nothing.
    * ``interactive.permission_mode`` / ``narration_mode`` are first-class
      control-inbox scalars. A cold attach must receive them for crash/handoff
      convergence, but a warm owner applies their ordered pending request in
      place. Hashing them forced a full detach/attach before that drain (9–11s
      measured on k3d for a scalar whose journal write took ~10ms).

    Every other config-content change (model, prompts, tools, datasources and
    other interactive settings) still changes the hash and forces the attach
    it should.
    """

    def _without_control_scalars(config: Any) -> Any:
        if not isinstance(config, dict):
            return config
        interactive = config.get("interactive")
        if not isinstance(interactive, dict):
            return config
        filtered = {
            key: value
            for key, value in interactive.items()
            if key not in {"permission_mode", "narration_mode"}
        }
        result = dict(config)
        if filtered:
            result["interactive"] = filtered
        else:
            result.pop("interactive", None)
        return result

    rc = attach.get("resolved_config")
    override = attach.get("config_override")
    if isinstance(rc, dict):
        rc = dict(rc)
        rc.pop("resolved_at", None)
        agent = rc.get("agent")
        if isinstance(agent, dict):
            rc["agent"] = _without_control_scalars(agent)
        attach = {
            **attach,
            "resolved_config": rc,
        }
    if isinstance(override, dict):
        attach = {
            **attach,
            "config_override": _without_control_scalars(override),
        }
    canonical = json.dumps(attach, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_diff_paths(
    old: Any, new: Any, *, max_depth: int = 3, max_paths: int = 20
) -> List[str]:
    """Dotted paths (KEYS ONLY — never values; bundles hold secrets) where two
    attach payloads differ. Diagnostic for affinity misses: answers *which*
    part of the bundle was volatile without ever logging credential material.
    """
    paths: List[str] = []

    def _canon(v: Any) -> str:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)

    def _walk(a: Any, b: Any, prefix: str, depth: int) -> None:
        if len(paths) >= max_paths:
            return
        if isinstance(a, dict) and isinstance(b, dict) and depth < max_depth:
            for key in sorted(set(a) | set(b)):
                pa_, pb_ = a.get(key), b.get(key)
                if _canon(pa_) != _canon(pb_):
                    _walk(
                        pa_, pb_, f"{prefix}.{key}" if prefix else str(key), depth + 1
                    )
        elif isinstance(a, list) and isinstance(b, list) and depth < max_depth:
            if len(a) != len(b):
                paths.append(f"{prefix}[len {len(a)}->{len(b)}]")
                return
            for i, (ia, ib) in enumerate(zip(a, b)):
                if _canon(ia) != _canon(ib):
                    _walk(ia, ib, f"{prefix}[{i}]", depth + 1)
        else:
            paths.append(prefix or "<root>")

    _walk(old, new, "", 0)
    return paths


def strip_restored_pending_humans(
    messages: List[Any], pending_rows: List[Dict[str, Any]]
) -> int:
    """Drop restored copies of not-yet-consumed human rows from the tail.

    ``_restore_session_messages`` loads ALL live rows — including pending
    unanswered ones (rows whose ``seq`` is past the consumed watermark).
    Those re-enter properly via the loop injection (id-based upsert makes the
    DB write idempotent); without this strip the turn would see the pending
    message twice (once as restored history, once as the injected input).

    Matching strategy: **by message id when the restored message carries the
    DB row id** (future-proof — today's restore deliberately mints fresh
    uuid4 ids and does not even select the id column, so id matches never
    occur), **else by exact content equality matched tail-to-tail** (the
    pending rows are the newest rows, so their restored copies are the last
    messages; both sequences are seq-ordered and compared from the end).

    Stops at the first trailing message that is not a matching HumanMessage:
    a non-trailing human is history and stays; an unanswered ``role='event'``
    row (never enqueued by the orchestrator, so never in ``pending_rows``)
    legitimately remains in context as history — matching today's documented
    behavior for accepted-but-unconsumed notices.

    Mutates ``messages`` in place; returns the number of messages removed.
    """
    if not messages or not pending_rows:
        return 0
    pending_ids = {
        str(row["id"]): row for row in pending_rows if row.get("id") is not None
    }
    # Event deliveries are deliberately excluded by transcript restore until
    # provider admission. Keep only rows that restore actually loaded, or an
    # event after a human row would stop the tail matcher and duplicate the
    # human on a fresh attach.
    remaining = [row for row in pending_rows if row.get("role", "human") == "human"]
    removed = 0
    while messages and remaining:
        msg = messages[-1]
        if getattr(msg, "type", None) != "human":
            break
        msg_id = getattr(msg, "id", None)
        row = pending_ids.get(str(msg_id)) if msg_id is not None else None
        if row is not None and row in remaining:
            remaining.remove(row)
        elif _message_text(msg) == (remaining[-1].get("content") or ""):
            remaining.pop()
        else:
            break
        messages.pop()
        removed += 1
    return removed


class StatelessTurnExecutor:
    """The M3 claim loop. One instance per stateless pod (see module docstring)."""

    def __init__(
        self,
        *,
        pod_name: Optional[str] = None,
        pod_uid: Optional[str] = None,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
        idle_backoff_seconds: float = IDLE_POLL_BACKOFF_SECONDS,
        idle_polls_before_backoff: int = IDLE_POLLS_BEFORE_BACKOFF,
        jitter: float = POLL_JITTER,
        cloud_push_wait_seconds: float = CLOUD_PUSH_WAIT_SECONDS,
        abort_grace_seconds: float = TURN_ABORT_GRACE_SECONDS,
        warm_session_idle_ttl_seconds: float = WARM_SESSION_IDLE_TTL_SECONDS,
        worker_enabled: Optional[bool] = None,
        completion_commands_enabled: Optional[bool] = None,
        audit_writer: Any = _AUDIT_WRITER_UNSET,
    ) -> None:
        self._pod_name = (
            pod_name or os.getenv("POD_NAME") or socket.gethostname() or "agent"
        )
        self._pod_uid = str(pod_uid or os.getenv("POD_UID") or "").strip()
        self._idle_poll_seconds = idle_poll_seconds
        self._idle_backoff_seconds = idle_backoff_seconds
        self._idle_polls_before_backoff = idle_polls_before_backoff
        self._jitter = jitter
        self._cloud_push_wait_seconds = cloud_push_wait_seconds
        self._abort_grace_seconds = abort_grace_seconds
        self._warm_session_idle_ttl = warm_session_idle_ttl_seconds
        self._warm_since: Optional[float] = None
        self._worker_enabled = (
            _enabled_env("STATELESS_EXECUTOR", False)
            if worker_enabled is None
            else bool(worker_enabled)
        )
        self._completion_commands_enabled = (
            _enabled_env("COMPLETION_COMMANDS_ENABLED", False)
            if completion_commands_enabled is None
            else bool(completion_commands_enabled)
        )
        self._worker_preempted = asyncio.Event()
        self._worker_preempt_status: Optional[str] = None
        self._worker_terminal_report_generation: tuple[str, int] | None = None
        self._worker_completion_accepted_generation: tuple[str, int] | None = None
        self._claim_audit_unavailable_logged = False
        if audit_writer is _AUDIT_WRITER_UNSET:
            try:
                # Reuse the process-wide archiver's SyncAuditWriter. Creating a
                # second writer would mean a second private event loop/pool;
                # resolving it once here also keeps writer construction out of
                # the per-claim path.
                from ..core.archiver import get_archiver

                archiver = get_archiver()
                self._claim_audit_writer = (
                    getattr(archiver, "_writer", None) if archiver is not None else None
                )
            except Exception:
                logger.warning(
                    "worker claim timing audit initialization failed; "
                    "claims will continue without timing rows",
                    exc_info=True,
                )
                self._claim_audit_writer = None
                self._claim_audit_unavailable_logged = True
        else:
            self._claim_audit_writer = audit_writer

        # S1 acceptance (zero in-process claim state): everything below is
        # either the soft-affinity hint or plumbing. Correctness never
        # depends on any of it surviving a restart.
        self._prefer_unit_id: Optional[Any] = None  # last thread served
        self._attached_fingerprint: Optional[str] = None
        # Previous attach payload, kept ONLY for affinity-miss path diffing
        # (fingerprint_diff_paths — key paths, never values). Same process
        # that holds the live session's credentials, so no new exposure.
        self._attached_bundle: Optional[Dict[str, Any]] = None
        self._lease = LeaseHandle()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        # Full-settle close evidence survives cancellation into the claim's
        # finally block. Cleanup must never downgrade an atomic
        # close+checkpoint retry into a gate-only close.
        self._pending_settled_close: tuple[str, int, int, int] | None = None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    @property
    def _db(self):
        """The agent's app-DB pool wrapper (PostgresDB) — the same pool
        ``_session.postgres_conn`` uses. PostgresDB exposes fetch/fetchrow/
        fetchval with asyncpg-compatible signatures, which is all the
        run_queue query functions need."""
        pa = _pa()
        agent = pa._agent
        db = getattr(agent, "postgres_conn", None) if agent is not None else None
        if db is None:
            raise RuntimeError(
                "stateless executor requires the agent's Postgres pool "
                "(UniversalAgent.initialize must run first, with "
                "connections.postgres enabled)"
            )
        return db

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("stateless executor already running")
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run(), name="stateless-turn-executor")

    def request_stop(self) -> None:
        """SIGTERM/preStop: stop claiming (mid-turn work finishes first)."""
        self._stop.set()

    async def stop(self, timeout: Optional[float] = None) -> None:
        """Stop claiming, let a mid-flight turn finish (bounded), then return.

        The turn keeps heartbeating while it finishes, so the lease covers the
        whole grace window. On timeout, escalate: politely interrupt the turn,
        then cancel the loop task as the last resort (the abandoned lease
        expires and the reaper requeues the unit — bounded, logged).
        """
        if timeout is None:
            timeout = float(os.getenv("STATELESS_SHUTDOWN_TIMEOUT_S", "120"))
        self.request_stop()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "stateless executor did not finish within %.0fs — "
                "interrupting the in-flight turn",
                timeout,
            )
            self._abort_turn_politely(_pa())
            try:
                await asyncio.wait_for(asyncio.shield(task), self._abort_grace_seconds)
            except asyncio.TimeoutError:
                logger.error(
                    "stateless executor still running after interrupt — "
                    "cancelling; the lease will expire and the reaper "
                    "requeues the unit"
                )
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        except Exception:
            logger.exception("stateless executor task ended with an error")
        self._task = None

    async def _sleep_interruptible(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, delay))

    def _idle_delay(self, idle_polls: int) -> float:
        # A pod holding a warm session never enters backoff: the DB-side
        # affinity grace (AFFINITY_GRACE_SECONDS) is sized for the FAST poll
        # cadence, and a backed-off warm pod would sleep straight through its
        # own head start and hand the thread to a cold pod.
        backed_off = (
            idle_polls >= self._idle_polls_before_backoff
            and self._attached_fingerprint is None
        )
        base = self._idle_backoff_seconds if backed_off else self._idle_poll_seconds
        return base * (1.0 + random.uniform(-self._jitter, self._jitter))

    def _mark_warm(self, claim: ClaimedUnit) -> None:
        """Record this claim as the affinity hint and (re)start the warm clock.

        Called only where a claim finished cleanly, so the cached session (if
        any) matches the thread this pod should keep preferring.
        """
        session = _pa()._session
        if session is None or not session.stateless_warm_reuse_safe:
            # Physical workspace state is retired under the claim before the
            # queue transition. Only lite sessions may remain resident/warm.
            self._prefer_unit_id = None
            self._warm_since = None
            return
        self._prefer_unit_id = claim.unit_id
        self._warm_since = time.monotonic()

    async def _expire_warm_session(self) -> None:
        """Drop a warm session nobody has come back to (see the TTL constant).

        Only ever runs on the idle path — between claims — so it cannot race
        a turn, and the next claim for that thread simply attaches fresh.
        """
        if self._warm_since is None or self._attached_fingerprint is None:
            return
        idle_for = time.monotonic() - self._warm_since
        if idle_for < self._warm_session_idle_ttl:
            return
        logger.info(
            "warm session expired after %.0fs idle (unit=%s) — detaching",
            idle_for,
            self._prefer_unit_id,
        )
        await self._detach_cached_session("warm_idle_ttl")

    def _new_worker_claim_timing(
        self, claim: WorkerClaim
    ) -> tuple[dict[str, Any], datetime, float]:
        """Create the one claim-local payload and its monotonic wall clock."""

        claimed_at = datetime.now(timezone.utc)
        timing: dict[str, Any] = {
            "bundle": 0.0,
            "preflight": 0.0,
            "agent_start": 0.0,
            "stream": 0.0,
            "finish": 0.0,
            "claimed_at": claimed_at.isoformat().replace("+00:00", "Z"),
            "released_at": None,
            "outcome": "error",
            "lease_token": int(claim.lease_token),
            "pod_name": self._pod_name,
            "mcp_attached": False,
        }
        return timing, claimed_at, time.perf_counter()

    @staticmethod
    def _add_worker_finish_timing(
        timing: dict[str, Any] | None, started_at: float
    ) -> None:
        if timing is not None:
            timing["finish"] = float(timing.get("finish") or 0.0) + max(
                0.0, time.perf_counter() - started_at
            )

    def _record_worker_claim_timing(
        self,
        claim: WorkerClaim,
        timing: dict[str, Any],
        *,
        claimed_at: datetime,
        started_at: float,
    ) -> None:
        """Best-effort append of the claim's sole ``claim_timing`` row."""

        released_at = datetime.now(timezone.utc)
        timing["released_at"] = released_at.isoformat().replace("+00:00", "Z")
        elapsed = max(0.0, time.perf_counter() - started_at)
        logger.info(
            "worker claim timing: unit=%s token=%d outcome=%s pod=%s "
            "mcp_attached=%s bundle=%.3fs preflight=%.3fs "
            "agent_start=%.3fs stream=%.3fs finish=%.3fs total=%.3fs",
            claim.unit_id,
            claim.lease_token,
            timing["outcome"],
            self._pod_name,
            timing["mcp_attached"],
            timing["bundle"],
            timing["preflight"],
            timing["agent_start"],
            timing["stream"],
            timing["finish"],
            elapsed,
        )

        writer = self._claim_audit_writer
        if writer is None:
            if not self._claim_audit_unavailable_logged:
                logger.warning(
                    "worker claim timing audit unavailable; claims continue "
                    "without agent_audit timing rows"
                )
                self._claim_audit_unavailable_logged = True
            return
        try:
            writer.insert_audit_pre(
                {
                    "job_id": str(claim.unit_id),
                    "agent_type": "worker",
                    "iteration": claim.unit.attempts_since_completion,
                    "step_type": "claim_timing",
                    "node_name": "worker_claim",
                    # Claim time, not insert time, is the stable ordering key
                    # when a fenced predecessor finishes after its successor.
                    "timestamp": claimed_at,
                    "latency_ms": round(elapsed * 1000),
                    "payload": dict(timing),
                    "metadata": None,
                }
            )
        except Exception:
            # SyncAuditWriter already converts its own readiness/write failures
            # to a warning + None. This belt protects injected/alternate sinks
            # without ever changing the queue disposition.
            logger.warning(
                "worker claim timing audit write failed; claim disposition "
                "is unchanged (unit=%s token=%d)",
                claim.unit_id,
                claim.lease_token,
                exc_info=True,
            )

    @staticmethod
    def _worker_mcp_attached(request: JobStartRequest) -> bool:
        """Whether resolved claim inputs will construct an MCP manager."""

        return any(
            isinstance(datasource, dict) and datasource.get("type") == "mcp"
            for datasource in (request.datasources or ())
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Install the lease handle in THIS context before any task is spawned:
        # the persistent loop, the event writer, and every per-turn helper
        # inherit the same mutable handle (see lease_context.py for why a
        # plain immutable ContextVar value cannot work across claims).
        current_lease.set(self._lease)
        logger.info(
            "stateless turn executor started: pod=%s idle_poll=%.2fs "
            "backoff=%.2fs/%d heartbeat=%ss worker_enabled=%s",
            self._pod_name,
            self._idle_poll_seconds,
            self._idle_backoff_seconds,
            self._idle_polls_before_backoff,
            HEARTBEAT_INTERVAL_SECONDS,
            self._worker_enabled,
        )
        idle_polls = 0
        while not self._stop.is_set():
            try:
                claim = await claim_unit(
                    self._db,
                    unit_kind=UNIT_KIND_SESSION_TURN,
                    pod_name=self._pod_name,
                    prefer_unit_id=self._prefer_unit_id,
                )
            except Exception as e:
                logger.warning("run_queue claim poll failed (transient): %s", e)
                await self._sleep_interruptible(self._idle_backoff_seconds)
                continue
            worker_claim: Optional[WorkerClaim] = None
            if claim is None and self._worker_enabled:
                try:
                    worker_claim = await claim_worker_batch(
                        self._db,
                        pod_name=self._pod_name,
                        completion_commands_enabled=(self._completion_commands_enabled),
                    )
                except Exception as e:
                    logger.warning("worker_batch claim poll failed (transient): %s", e)
                    await self._sleep_interruptible(self._idle_backoff_seconds)
                    continue

            if claim is None and worker_claim is None:
                idle_polls += 1
                await self._expire_warm_session()
                await self._sleep_interruptible(self._idle_delay(idle_polls))
                continue
            idle_polls = 0
            if self._stop.is_set():
                # Claimed on the stop boundary — hand it straight back for
                # another pod (no backoff, not an error).
                stop_claim = worker_claim.unit if worker_claim is not None else claim
                stop_timing: dict[str, Any] | None = None
                stop_claimed_at: datetime | None = None
                stop_started_at: float | None = None
                if worker_claim is not None:
                    (
                        stop_timing,
                        stop_claimed_at,
                        stop_started_at,
                    ) = self._new_worker_claim_timing(worker_claim)
                try:
                    finish_started_at = time.perf_counter()
                    try:
                        state = await release_unit(
                            self._db,
                            unit_id=stop_claim.unit_id,
                            lease_token=stop_claim.lease_token,
                            backoff_seconds=0.0,
                        )
                    finally:
                        self._add_worker_finish_timing(
                            stop_timing,
                            finish_started_at,
                        )
                    logger.info(
                        "run_queue release: unit=%s token=%d reason=shutting_down "
                        "state=%s",
                        stop_claim.unit_id,
                        stop_claim.lease_token,
                        state,
                    )
                except Exception:
                    pass
                finally:
                    if (
                        worker_claim is not None
                        and stop_timing is not None
                        and stop_claimed_at is not None
                        and stop_started_at is not None
                    ):
                        stop_timing["outcome"] = "released:shutting_down"
                        self._record_worker_claim_timing(
                            worker_claim,
                            stop_timing,
                            claimed_at=stop_claimed_at,
                            started_at=stop_started_at,
                        )
                break
            if worker_claim is not None:
                try:
                    await self._serve_worker_claim(worker_claim)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "unhandled error serving worker unit %s — releasing and "
                        "continuing",
                        worker_claim.unit_id,
                    )
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._cleanup_worker_runtime(preserve_shell=True)
                    await self._release_worker_claim(
                        worker_claim,
                        reason="serve_crash",
                    )
                continue
            try:
                assert claim is not None
                await self._serve_claim(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never crash the loop. _serve_claim handles its own error
                # paths; this outermost belt also hands the lease back so an
                # unexpected crash cannot strand the unit until lease expiry.
                # Safe: release_unit fires only on (token match AND state
                # 'leased'), so after a successful complete or a lost lease
                # it is a recorded no-op.
                logger.exception(
                    "unhandled error serving unit %s — releasing and continuing",
                    claim.unit_id,
                )
                await self._release(claim, reason="serve_crash")
        logger.info("stateless turn executor stopped: pod=%s", self._pod_name)

    # ------------------------------------------------------------------
    # One claim
    # ------------------------------------------------------------------

    async def _serve_worker_claim(self, claim: WorkerClaim) -> None:
        """Drive one worker batch under its immutable queue lease.

        This is deliberately a separate lifecycle from the persistent-session
        driver below.  Worker rotation is queue-only; the completion API is
        reserved for genuine terminal/human-facing graph stops.
        """

        unit = claim.unit
        unit_id = str(unit.unit_id)
        token = unit.lease_token
        timing, claimed_at, claim_started_at = self._new_worker_claim_timing(claim)
        logger.info(
            "run_queue claim: unit=%s kind=%s token=%d attempts=%d "
            "input_seq=%s consumed_seq=%s prior_job_status=%s pod=%s",
            unit_id,
            unit.unit_kind,
            token,
            unit.attempts_since_completion,
            unit.input_seq,
            unit.consumed_seq,
            claim.prior_job_status,
            self._pod_name,
        )
        self._worker_preempted = asyncio.Event()
        self._worker_preempt_status = None
        self._worker_terminal_report_generation = None
        self._worker_completion_accepted_generation = None
        heartbeat_task: asyncio.Task | None = None
        try:
            heartbeat_task = asyncio.create_task(
                self._worker_heartbeat_loop(claim),
                name=f"worker-lease-heartbeat-{unit_id[:8]}",
            )
            pa = _pa()
            # A shared pod can still hold a warm interactive session when the
            # next durable claim is a worker.  Perform the same physical claim
            # switch even for the no-work ``attempts > max`` give-up path:
            # terminal cleanup must never clear the singleton agent underneath
            # an attached cached session or inherit its tenant residue.
            if pa._session is not None:
                await self._detach_cached_session("worker_claim_switch")
            self._scrub_process_residue()
            self._lease.update(
                unit_id,
                token,
                executor_id=self._pod_name,
                pod_uid=self._pod_uid,
            )

            timing["outcome"] = await self._serve_worker_claim_inner(
                claim,
                timing=timing,
                retry_exhausted=(unit.attempts_since_completion > claim.max_attempts),
            )
        except asyncio.CancelledError:
            # Hard executor shutdown: close local admission first.  A best-effort
            # release is intentionally left to the outer shutdown/reaper path if
            # cancellation prevents the DB call from completing.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._cleanup_worker_runtime(preserve_shell=True)
            raise
        except Exception as exc:
            logger.exception(
                "worker_batch failed before a disposition: unit=%s token=%d",
                unit_id,
                token,
            )
            report_started = self._worker_terminal_report_generation == (
                unit_id,
                int(token),
            )
            if report_started:
                # Once an HTTP report has begun, correction 8 owns every
                # ambiguous tail failure. Never issue a second report from
                # this generation and never park it: preserve runtime state,
                # release with backoff, and let a successor consume or
                # benignly re-report the durable END checkpoint.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._cleanup_worker_runtime(preserve_shell=True)
                if self._worker_completion_accepted_generation == (
                    unit_id,
                    int(token),
                ):
                    logger.warning(
                        "worker accepted completion hold failed; durable command "
                        "backstop retains ownership: unit=%s token=%d",
                        unit_id,
                        token,
                    )
                    return
                await self._release_worker_claim(
                    claim,
                    reason="terminal_report_failed",
                    park_on_exhaustion=False,
                    timing=timing,
                )
                timing["outcome"] = "released:terminal_report_failed"
                return
            if str(self._lease.unit_id or "") != unit_id or int(
                self._lease.lease_token
            ) != int(token):
                # Failure may precede the normal publication point (for
                # example, while detaching a warm session). The queue claim is
                # nevertheless authoritative; publish this generation before
                # its retry/give-up disposition rather than inheriting the old
                # handle's lost bit.
                self._lease.update(
                    unit_id,
                    token,
                    executor_id=self._pod_name,
                    pod_uid=self._pod_uid,
                )
            if (
                unit.attempts_since_completion > claim.max_attempts
                and not self._lease.lost.is_set()
            ):
                # Above the cap, bundle/setup exists only to inspect the
                # canonical checkpoint. If that inspection is temporarily
                # unavailable, do not overwrite a potentially successful or
                # human-facing END with an invented failure. Retry without
                # parking until the checkpoint can decide the outcome.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._cleanup_worker_runtime(preserve_shell=True)
                await self._release_worker_claim(
                    claim,
                    reason="exhausted_checkpoint_probe_failed",
                    park_on_exhaustion=False,
                    timing=timing,
                )
                timing["outcome"] = "released:exhausted_checkpoint_probe_failed"
                return
            if (
                unit.attempts_since_completion == claim.max_attempts
                and not self._lease.lost.is_set()
            ):
                final_state = self._worker_retry_exhausted_state(
                    self._worker_driver_error_state(str(exc), job_id=unit_id),
                    attempts=unit.attempts_since_completion,
                    max_attempts=claim.max_attempts,
                )
                logger.error(
                    "worker_batch driver retry exhausted: unit=%s token=%d "
                    "attempts=%d/%d — reporting terminal give-up",
                    unit_id,
                    token,
                    unit.attempts_since_completion,
                    claim.max_attempts,
                )
                try:
                    timing["outcome"] = await self._report_worker_terminal(
                        claim,
                        final_state,
                        timing=timing,
                    )
                    return
                except Exception:
                    # The report path itself is retriable forever (correction
                    # 8): never turn an unavailable completion handler into an
                    # invisible parked processing job.
                    logger.exception(
                        "worker_batch exhausted give-up report failed before a "
                        "disposition: unit=%s token=%d",
                        unit_id,
                        token,
                    )
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._cleanup_worker_runtime(preserve_shell=True)
                    await self._release_worker_claim(
                        claim,
                        reason="terminal_report_failed",
                        park_on_exhaustion=False,
                        timing=timing,
                    )
                    timing["outcome"] = "released:terminal_report_failed"
                    return
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._cleanup_worker_runtime(preserve_shell=True)
            await self._release_worker_claim(
                claim,
                reason="driver_error",
                timing=timing,
            )
            timing["outcome"] = "released:driver_error"
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat_task
            self._record_worker_claim_timing(
                claim,
                timing,
                claimed_at=claimed_at,
                started_at=claim_started_at,
            )

    async def _serve_worker_claim_inner(
        self,
        claim: WorkerClaim,
        *,
        timing: dict[str, Any],
        retry_exhausted: bool = False,
    ) -> str:
        pa = _pa()
        unit = claim.unit
        job_id = str(unit.unit_id)
        token = unit.lease_token

        started_at = time.perf_counter()
        try:
            bundle = await self._fetch_bundle(job_id, token)
        finally:
            timing["bundle"] = max(0.0, time.perf_counter() - started_at)
        request, batch = self._parse_worker_bundle(bundle, claim)
        timing["mcp_attached"] = self._worker_mcp_attached(request)
        metadata = self._worker_job_metadata(request)
        context = request.context or {}
        self._seed_worker_inboxes(
            job_id,
            context.get("pending_guidance"),
            context.get("queued_replies"),
        )

        # Close the claim→bundle race and get the authoritative control state
        # before creating a workspace or invoking the graph.
        started_at = time.perf_counter()
        try:
            renewal = await renew_worker_batch(
                self._db,
                unit_id=unit.unit_id,
                lease_token=token,
            )
        finally:
            timing["preflight"] = max(0.0, time.perf_counter() - started_at)
        if renewal is None:
            self._lease.mark_lost()
            logger.warning(
                "lease lost: worker unit=%s token=%d before graph start",
                job_id,
                token,
            )
            return "error"
        self._observe_worker_renewal(job_id, renewal)
        if self._worker_preempted.is_set():
            await self._finish_external_worker_preempt(claim, timing=timing)
            return "preempted"

        agent = pa._agent
        client = pa._orchestrator_client
        if agent is None or client is None:
            raise RuntimeError("worker claim requires an initialized agent and client")
        agent._orchestrator_client = client

        from src.shared.job_steering import CheckpointSteeringAcker

        steering_acker = CheckpointSteeringAcker(job_id, client)

        streaming_gen: Optional[AsyncIterator[Dict[str, Any]]] = None
        agent_start_started_at = time.perf_counter()
        timing["agent_start"] = None
        try:
            streaming_gen = await agent.process_job(
                job_id,
                metadata,
                stream=True,
                resume=claim.resume,
                feedback=context.get("queued_feedback"),
                feedback_reason=context.get("queued_feedback_reason"),
                original_config_name=request.config_name,
                previous_status=claim.prior_job_status,
                worker_lease_token=token,
                worker_batch_target_wall_seconds=batch["target_wall_seconds"],
                worker_batch_min_wall_seconds=batch.get("min_wall_seconds"),
                worker_batch_iteration_cap=batch.get("iteration_cap"),
                worker_resume_id=claim.resume_id,
                worker_retry_exhausted=retry_exhausted,
                defer_cleanup=True,
                worker_checkpoint_post_commit=steering_acker,
            )
            outcome, final_state = await self._consume_worker_stream(
                streaming_gen,
                timing=timing,
                agent_start_started_at=agent_start_started_at,
            )
        finally:
            # ``agent_start`` ends at the first superstep yielded by the graph,
            # not at process_job() returning its async iterator. It therefore
            # intentionally includes workspace SSH, datasource/MCP setup,
            # checkpoint/Todo hydration, and any work inside that first step.
            if timing["agent_start"] is None:
                timing["agent_start"] = max(
                    0.0, time.perf_counter() - agent_start_started_at
                )
            if streaming_gen is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await streaming_gen.aclose()

        if outcome == "lease_lost":
            logger.warning(
                "lease lost: worker unit=%s token=%d — no report/release/complete",
                job_id,
                token,
            )
            await self._cleanup_worker_runtime(preserve_shell=True)
            return "error"
        if outcome == "preempted":
            await self._finish_external_worker_preempt(claim, timing=timing)
            return "preempted"
        # A renewal can commit between the stream's StopAsyncIteration and the
        # disposition branch.  Recheck both signals so an external control is
        # still guaranteed to make zero HTTP completion reports.
        if self._lease.lost.is_set():
            await self._cleanup_worker_runtime(preserve_shell=True)
            return "error"
        if self._worker_preempted.is_set():
            await self._finish_external_worker_preempt(claim, timing=timing)
            return "preempted"
        if outcome != "graph_end" or final_state is None:
            if unit.attempts_since_completion >= claim.max_attempts:
                exhausted = self._worker_retry_exhausted_state(
                    self._worker_driver_error_state(
                        "worker graph stream ended without a durable terminal state",
                        job_id=job_id,
                    ),
                    attempts=unit.attempts_since_completion,
                    max_attempts=claim.max_attempts,
                )
                logger.error(
                    "worker_batch empty-stream retry exhausted: unit=%s token=%d "
                    "attempts=%d/%d — reporting terminal give-up",
                    job_id,
                    token,
                    unit.attempts_since_completion,
                    claim.max_attempts,
                )
                return await self._report_worker_terminal(
                    claim,
                    exhausted,
                    client=client,
                    timing=timing,
                )
            await self._cleanup_worker_runtime(preserve_shell=True)
            await self._release_worker_claim(
                claim,
                reason="graph_stream_ended_empty",
                timing=timing,
            )
            return "released:graph_stream_ended_empty"

        freeze = final_state.get("freeze_data") or {}
        freeze_type = freeze.get("freeze_type") if isinstance(freeze, dict) else None
        if freeze_type == FREEZE_TYPE_BATCH_BOUNDARY:
            await self._cleanup_worker_runtime(preserve_shell=True)
            started_at = time.perf_counter()
            try:
                rotation = await rotate_worker_batch(
                    self._db,
                    unit_id=unit.unit_id,
                    lease_token=token,
                    input_seq=unit.input_seq,
                    fair_key=unit.fair_key,
                )
            finally:
                self._add_worker_finish_timing(timing, started_at)
            if rotation is None:
                self._lease.mark_lost()
                logger.warning(
                    "lease lost: worker rotation fenced out unit=%s token=%d",
                    job_id,
                    token,
                )
                return "error"
            logger.info(
                "worker_batch rotate: unit=%s token=%d "
                "queue_verb=complete_and_requeue queue_state=%s "
                "input_seq=%s next_input_seq=%d complete_calls=0 "
                "http_complete_calls=0",
                job_id,
                token,
                rotation.state,
                rotation.prior_input_seq,
                rotation.next_input_seq,
            )
            return "rotated"

        if self._worker_stop_is_recoverable(final_state, freeze_type):
            if unit.attempts_since_completion >= claim.max_attempts:
                final_state = self._worker_retry_exhausted_state(
                    final_state,
                    attempts=unit.attempts_since_completion,
                    max_attempts=claim.max_attempts,
                )
                freeze_type = "worker_retry_exhausted"
                logger.error(
                    "worker_batch retry exhausted: unit=%s token=%d "
                    "attempts=%d/%d — reporting terminal give-up",
                    job_id,
                    token,
                    unit.attempts_since_completion,
                    claim.max_attempts,
                )
            else:
                await self._cleanup_worker_runtime(preserve_shell=True)
                await self._release_worker_claim(
                    claim,
                    reason="recoverable_stop",
                    timing=timing,
                )
                logger.info(
                    "worker_batch recoverable release: unit=%s token=%d "
                    "freeze=%s attempts=%d/%d complete_calls=0 "
                    "http_complete_calls=0",
                    job_id,
                    token,
                    freeze_type,
                    unit.attempts_since_completion,
                    claim.max_attempts,
                )
                return "released:recoverable_stop"

        return await self._report_worker_terminal(
            claim,
            final_state,
            client=client,
            timing=timing,
        )

    async def _report_worker_terminal(
        self,
        claim: WorkerClaim,
        final_state: Dict[str, Any],
        *,
        client: Any | None = None,
        timing: dict[str, Any] | None = None,
    ) -> str:
        """Report one genuine/give-up stop, then fence the queue disposition."""

        unit = claim.unit
        job_id = str(unit.unit_id)
        token = unit.lease_token
        wire_payload, payload_source = self._worker_completion_wire_payload(final_state)
        if wire_payload.get("should_stop") is not True:
            # Fail closed before marking this generation as report-started.
            # A continue-shaped stateless payload is a driver bug, not a
            # completion attempt: preserve the remote shell and return the
            # claim through ordinary queue backoff/default exhaustion parking.
            logger.error(
                "worker terminal report blocked locally: unit=%s token=%d "
                "payload_source=%s effective_should_stop_not_true — preserving "
                "shell and releasing without /complete",
                job_id,
                token,
                payload_source,
            )
            await self._cleanup_worker_runtime(preserve_shell=True)
            await self._release_worker_claim(
                claim,
                reason="nonterminal_completion_report_blocked",
                timing=timing,
            )
            return "released:nonterminal_completion_report_blocked"
        if client is None:
            client = _pa()._orchestrator_client
        if client is None:
            raise RuntimeError("worker terminal report requires orchestrator client")

        # Genuine terminal/human-facing stop: report exactly once while the
        # queue lease and renewal task remain alive. Only success or exact B4
        # acceptance proof permits finalization hold/queue closure. Ambiguous
        # failures preserve tmux and re-report; the exact coded pre-write 422
        # below instead follows ordinary bounded retry/parking semantics.
        self._worker_terminal_report_generation = (job_id, int(token))
        started_at = time.perf_counter()
        try:
            reported = await client.report_completion(
                job_id,
                final_state,
                lease_token=token,
            )
        except CompletionNonTerminalReportError as exc:
            # This exact coded 422 is a definitive pre-write refusal. Clear
            # the report-started marker before cleanup so even a cleanup fault
            # follows ordinary retry/parking semantics rather than the
            # ambiguous-HTTP no-park path.
            self._worker_terminal_report_generation = None
            logger.error(
                "worker completion definitively refused: unit=%s token=%d code=%s",
                job_id,
                token,
                exc.code,
            )
            try:
                await self._cleanup_worker_runtime(preserve_shell=True)
            except Exception:
                # Cleanup is best-effort but the definitive pre-write result
                # must remain definitive. Letting this escape could enter the
                # driver-exhaustion handler and issue a second /complete at
                # the retry cap. Keep the diagnostic bounded and continue to
                # the exact token-fenced ordinary release.
                logger.error(
                    "worker completion refusal cleanup failed: "
                    "unit=%s token=%d code=%s",
                    job_id,
                    token,
                    exc.code,
                )
            await self._release_worker_claim(
                claim,
                reason=exc.code,
                timing=timing,
            )
            return f"released:{exc.code}"
        finally:
            self._add_worker_finish_timing(timing, started_at)
        if not reported:
            # A pause/cancel may win after the handler's thin entry fence.  The
            # handler's jobs-row disposition CAS then rejects the report. Read
            # the authoritative status once immediately (rather than waiting
            # for the next heartbeat) and honor the external control with zero
            # queue error-release. Never cancel an in-flight report: Starlette
            # cancellation can strand the existing multi-write handler.
            failed_report_status: str | None = None
            accepted_completion = None
            if not self._lease.lost.is_set():
                renewal = await renew_worker_batch(
                    self._db,
                    unit_id=unit.unit_id,
                    lease_token=token,
                )
                if renewal is None:
                    accepted_completion = await self._accepted_worker_completion(claim)
                    if accepted_completion is None:
                        self._lease.mark_lost()
                    else:
                        failed_report_status = accepted_completion.job_status
                else:
                    failed_report_status = renewal.job_status
                    self._observe_worker_renewal(job_id, renewal)
            if accepted_completion is not None:
                logger.info(
                    "worker completion already accepted: unit=%s token=%d "
                    "command=%s state=%s after ambiguous HTTP result",
                    job_id,
                    token,
                    accepted_completion.command_id,
                    accepted_completion.command_state,
                )
                resolved_status = await self._finish_accepted_worker_completion(
                    claim,
                    accepted_completion,
                    http_result_ambiguous=True,
                )
                return self._worker_terminal_timing_outcome(
                    final_state,
                    observed_status=resolved_status or accepted_completion.job_status,
                )
            if self._lease.lost.is_set():
                await self._cleanup_worker_runtime(preserve_shell=True)
                return "error"
            if self._worker_preempted.is_set():
                await self._finish_external_worker_preempt(
                    claim,
                    http_complete_calls=1,
                    timing=timing,
                )
                return "preempted"
            await self._cleanup_worker_runtime(
                preserve_shell=failed_report_status
                not in {"completed", "failed", "cancelled"}
            )
            await self._release_worker_claim(
                claim,
                reason="terminal_report_failed",
                park_on_exhaustion=False,
                timing=timing,
            )
            return "released:terminal_report_failed"

        # Do not rely on the heartbeat event observed before/during the HTTP
        # call. Re-read the exact token and authoritative job status after the
        # handler returns so a same-window control transition cannot be missed
        # before queue closure. Report-authored paused/failed states use the
        # same safe terminal closure; their cleanup disposition is status-based.
        post_report_status: str | None = None
        accepted_completion = None
        if not self._lease.lost.is_set():
            renewal = await renew_worker_batch(
                self._db,
                unit_id=unit.unit_id,
                lease_token=token,
            )
            if renewal is None:
                accepted_completion = await self._accepted_worker_completion(claim)
                if accepted_completion is None:
                    self._lease.mark_lost()
                else:
                    post_report_status = accepted_completion.job_status
            else:
                post_report_status = renewal.job_status
                self._observe_worker_renewal(job_id, renewal)
        if self._lease.lost.is_set():
            await self._cleanup_worker_runtime(preserve_shell=True)
            logger.warning(
                "lease lost: worker unit=%s token=%d after accepted terminal "
                "report — successor owns queue closure",
                job_id,
                token,
            )
            return self._worker_terminal_timing_outcome(final_state)
        if self._worker_preempted.is_set():
            await self._finish_external_worker_preempt(
                claim,
                http_complete_calls=1,
                timing=timing,
            )
            return "preempted"
        if accepted_completion is not None:
            resolved_status = await self._finish_accepted_worker_completion(
                claim,
                accepted_completion,
                http_result_ambiguous=False,
            )
            return self._worker_terminal_timing_outcome(
                final_state,
                observed_status=resolved_status or accepted_completion.job_status,
            )
        await self._cleanup_worker_runtime(
            preserve_shell=post_report_status in _WORKER_PRESERVE_SHELL_STATUSES
        )
        state = await complete_worker_batch(
            self._db,
            unit_id=unit.unit_id,
            lease_token=token,
            consumed_seq=unit.input_seq,
        )
        if state is None:
            self._lease.mark_lost()
            logger.warning(
                "lease lost: worker terminal queue closure fenced out unit=%s token=%d",
                job_id,
                token,
            )
            return self._worker_terminal_timing_outcome(
                final_state,
                observed_status=post_report_status,
            )
        logger.info(
            "worker_batch terminal: unit=%s token=%d queue_state=%s "
            "complete_calls=1 http_complete_calls=1",
            job_id,
            token,
            state,
        )
        return self._worker_terminal_timing_outcome(
            final_state,
            observed_status=post_report_status,
        )

    @staticmethod
    def _worker_completion_wire_payload(
        final_state: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str]:
        """Return the exact payload source ``report_completion`` will use."""

        checkpointed = final_state.get("completion_report_payload")
        if isinstance(checkpointed, dict) and set(checkpointed) == set(
            _COMPLETION_REPORT_PAYLOAD_FIELDS
        ):
            return checkpointed, "checkpoint_envelope"
        return final_state, "live_state"

    @classmethod
    def _worker_terminal_timing_outcome(
        cls,
        final_state: Dict[str, Any],
        *,
        observed_status: str | None = None,
    ) -> str:
        """Label telemetry without making an orchestrator status decision."""

        normalized = str(observed_status or "").strip()
        if normalized and normalized not in {"created", "processing"}:
            return f"terminal:{normalized}"
        payload, _ = cls._worker_completion_wire_payload(final_state)
        freeze = payload.get("freeze_data")
        if isinstance(freeze, dict):
            reported_status = str(freeze.get("status") or "").strip()
            if reported_status:
                if reported_status == "job_completed":
                    reported_status = "completed"
                return f"terminal:{reported_status}"
        if payload.get("goal_achieved") is True:
            return "terminal:completed"
        if payload.get("error"):
            return "terminal:failed"
        return "terminal:unknown"

    @staticmethod
    def _parse_worker_bundle(
        bundle: Dict[str, Any], claim: WorkerClaim
    ) -> Tuple[JobStartRequest, Dict[str, Any]]:
        job_id = str(claim.unit_id)
        if (
            str(bundle.get("unit_id")) != job_id
            or str(bundle.get("job_id")) != job_id
            or bundle.get("unit_kind") != "worker_batch"
            or bundle.get("execution_lane") != "stateless"
        ):
            raise ValueError("claim bundle does not describe the leased worker unit")
        request = JobStartRequest.model_validate(bundle.get("job") or {})
        if request.job_id != job_id:
            raise ValueError("worker claim bundle job payload id mismatch")
        from ..shared.workspace_contract import validate_worker_workspace_projection

        validate_worker_workspace_projection(
            config_override=request.config_override,
            resolved_config=request.resolved_config,
            workspace_runtime=request.workspace_runtime,
        )
        authority = {
            "workspace_generation": request.workspace_generation,
            "workspace_runtime_incarnation": request.workspace_runtime_incarnation,
            "workspace_ssh_host_key_fingerprint": (
                request.workspace_ssh_host_key_fingerprint
            ),
            "workspace_owner_kind": request.workspace_owner_kind,
            "workspace_owner_id": request.workspace_owner_id,
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in authority.values()
        ):
            raise ValueError("worker claim bundle is missing workspace authority")
        if request.workspace_owner_kind != "job":
            raise ValueError("worker claim workspace owner kind is invalid")
        for field in (
            "workspace_generation",
            "workspace_runtime_incarnation",
            "workspace_owner_id",
        ):
            value = str(authority[field])
            try:
                canonical = str(UUID(value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"worker claim {field} is invalid") from exc
            if value != canonical:
                raise ValueError(f"worker claim {field} is invalid")
        if (
            re.fullmatch(
                r"SHA256:[A-Za-z0-9+/]{43}",
                str(request.workspace_ssh_host_key_fingerprint),
            )
            is None
        ):
            raise ValueError("worker claim SSH host identity is invalid")
        batch = bundle.get("batch")
        if not isinstance(batch, dict):
            raise ValueError("worker claim bundle is missing its batch envelope")
        return request, dict(batch)

    @staticmethod
    def _worker_job_metadata(request: JobStartRequest) -> Dict[str, Any]:
        """Build the exact metadata shape used by the pinned start path."""

        metadata: Dict[str, Any] = {"description": request.description}
        for field, key in (
            ("upload_id", "upload_id"),
            ("config_upload_id", "config_upload_id"),
            ("instructions_upload_id", "instructions_upload_id"),
            ("document_path", "document_path"),
            ("document_dir", "document_dir"),
        ):
            value = getattr(request, field)
            if value:
                metadata[key] = value
        if request.context:
            metadata.update(request.context)
        if request.instructions:
            metadata["instructions"] = request.instructions
        if request.config_name and request.config_name != "worker_base":
            metadata["config_name"] = request.config_name
        for field, key in (
            ("expert_id", "expert_id"),
            ("config_override", "config_override"),
            ("resolved_config", "resolved_config"),
            ("git_remote_url", "git_remote_url"),
            ("datasources", "datasources"),
            ("repositories", "repositories"),
            (
                "managed_repository_credentials",
                "managed_repository_credentials",
            ),
            ("branch_name", "branch_name"),
            ("project_id", "project_id"),
            ("runtime_actor", "runtime_actor"),
            ("workspace_runtime", "workspace_runtime"),
            ("workspace_generation", "workspace_generation"),
            (
                "workspace_runtime_incarnation",
                "workspace_runtime_incarnation",
            ),
            (
                "workspace_ssh_host_key_fingerprint",
                "workspace_ssh_host_key_fingerprint",
            ),
            ("workspace_owner_kind", "workspace_owner_kind"),
            ("workspace_owner_id", "workspace_owner_id"),
        ):
            value = getattr(request, field)
            if value:
                metadata[key] = value
        return metadata

    async def _consume_worker_stream(
        self,
        stream: AsyncIterator[Dict[str, Any]],
        *,
        timing: dict[str, Any],
        agent_start_started_at: float,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Race each graph step against exact-lease loss and external stop."""

        final_state: Optional[Dict[str, Any]] = None
        stream_started_at: float | None = None
        lost_waiter = asyncio.create_task(self._lease.lost.wait())
        preempt_waiter = asyncio.create_task(self._worker_preempted.wait())
        try:
            while True:
                next_state = asyncio.create_task(anext(stream))
                try:
                    await asyncio.wait(
                        {next_state, lost_waiter, preempt_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Ownership/control always wins a same-tick graph result.
                    if self._lease.lost.is_set():
                        next_state.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await next_state
                        return "lease_lost", final_state
                    if self._worker_preempted.is_set():
                        next_state.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await next_state
                        return "preempted", final_state
                    try:
                        state = next_state.result()
                    except StopAsyncIteration:
                        return "graph_end", final_state
                    if stream_started_at is None:
                        stream_started_at = time.perf_counter()
                        timing["agent_start"] = max(
                            0.0, stream_started_at - agent_start_started_at
                        )
                    if isinstance(state, dict):
                        final_state = state
                finally:
                    if not next_state.done():
                        next_state.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await next_state
        finally:
            finished_at = time.perf_counter()
            if timing["agent_start"] is None:
                timing["agent_start"] = max(0.0, finished_at - agent_start_started_at)
            timing["stream"] = (
                max(0.0, finished_at - stream_started_at)
                if stream_started_at is not None
                else 0.0
            )
            for waiter in (lost_waiter, preempt_waiter):
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter

    async def _worker_heartbeat_loop(self, claim: WorkerClaim) -> None:
        unit = claim.unit
        job_id = str(unit.unit_id)
        token = unit.lease_token
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                renewal = await renew_worker_batch(
                    self._db,
                    unit_id=unit.unit_id,
                    lease_token=token,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "worker lease heartbeat failed for unit %s (transient): %s",
                    job_id,
                    e,
                )
                continue
            if renewal is None:
                accepted_completion = await self._accepted_worker_completion(claim)
                if accepted_completion is not None:
                    logger.info(
                        "worker completion accepted during heartbeat: unit=%s "
                        "token=%d command=%s state=%s",
                        job_id,
                        token,
                        accepted_completion.command_id,
                        accepted_completion.command_state,
                    )
                    return
                self._lease.mark_lost()
                logger.warning(
                    "lease lost: worker unit=%s token=%d (renewal rejected)",
                    job_id,
                    token,
                )
                return
            self._observe_worker_renewal(job_id, renewal)

    async def _accepted_worker_completion(
        self,
        claim: WorkerClaim,
        *,
        command_id: Any | None = None,
    ) -> WorkerCompletionAcceptance | None:
        """Return an exact B4 accept only while the shared rollout gate is on."""

        if not self._completion_commands_enabled:
            return None
        try:
            kwargs: dict[str, Any] = {
                "unit_id": claim.unit_id,
                "lease_token": claim.lease_token,
            }
            if command_id is not None:
                kwargs["command_id"] = command_id
            return await get_worker_completion_acceptance(self._db, **kwargs)
        except Exception as exc:
            logger.warning(
                "worker completion-acceptance lookup failed for unit %s "
                "(treating renewal rejection as lease loss): %s",
                claim.unit_id,
                exc,
            )
            return None

    @staticmethod
    def _stored_worker_completion_status(
        accepted: WorkerCompletionAcceptance,
    ) -> str | None:
        """Resolve shell disposition from the finalized command's own result."""

        outcome = accepted.command_outcome
        state = accepted.command_state
        value: Any = None
        if state == "done":
            value = outcome.get("new_status")
        elif state == "superseded":
            value = outcome.get("observed_status") or outcome.get("observed_job_status")
        elif state == "force_resolved":
            value = outcome.get("terminal_status")
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _worker_finalization_poll_delay(
        accepted: WorkerCompletionAcceptance,
    ) -> float:
        """Bound the next observation by DB-clock run/lease/deadline horizons."""

        horizons = [
            WORKER_FINALIZATION_POLL_SECONDS,
            accepted.deadline_remaining_seconds,
        ]
        if accepted.command_state == "pending":
            horizons.append(accepted.run_after_remaining_seconds)
        elif (
            accepted.command_state == "finalizing"
            and accepted.lease_remaining_seconds is not None
        ):
            horizons.append(accepted.lease_remaining_seconds)
        positive = [value for value in horizons if value > 0]
        return max(0.05, min(positive or [WORKER_FINALIZATION_POLL_SECONDS]))

    async def _sleep_worker_finalization_poll(self, seconds: float) -> None:
        """Sleep seam kept separate from the worker lease heartbeat in tests."""

        await asyncio.sleep(seconds)

    async def _finish_accepted_worker_completion(
        self,
        claim: WorkerClaim,
        accepted: WorkerCompletionAcceptance,
        *,
        http_result_ambiguous: bool,
    ) -> str | None:
        """Hold an accepted worker shell until its exact command resolves.

        B4 already changed the queue row to ``done``; releasing or completing
        it here would create a second execution owner.  Pending/finalizing work
        instead retires local shell admission and scrubs claim-local state,
        then observes the same command through the established B4 lookup.  Its
        PostgreSQL deadline is the absolute local wait bound.  Parked, lookup
        loss, deadline, or cancellation hands the still-live remote shell to
        the command/lifecycle backstop without requeueing.  Only an explicit
        terminal status in the stored finalized outcome destroys tmux.
        """

        current = accepted
        self._worker_completion_accepted_generation = (
            str(claim.unit_id),
            int(claim.lease_token),
        )
        held = False
        while (
            current.command_state in _WORKER_UNFINISHED_COMMAND_STATES
            and not current.deadline_expired
        ):
            if not held:
                agent = _pa()._agent
                if agent is not None:
                    await agent.hold_worker_finalization()
                held = True
                logger.info(
                    "worker finalization-pending hold: unit=%s token=%d "
                    "command=%s state=%s deadline_in=%.3fs",
                    claim.unit_id,
                    claim.lease_token,
                    current.command_id,
                    current.command_state,
                    current.deadline_remaining_seconds,
                )
            await self._sleep_worker_finalization_poll(
                self._worker_finalization_poll_delay(current)
            )
            observed = await self._accepted_worker_completion(
                claim,
                command_id=current.command_id,
            )
            if observed is None:
                await self._cleanup_worker_runtime(preserve_shell=True)
                logger.warning(
                    "worker finalization hold handed off after lookup loss: "
                    "unit=%s token=%d command=%s",
                    claim.unit_id,
                    claim.lease_token,
                    current.command_id,
                )
                return None
            current = observed

        resolved_status = (
            self._stored_worker_completion_status(current)
            if current.command_state in _WORKER_FINALIZED_COMMAND_STATES
            else None
        )
        preserve_shell = resolved_status not in _WORKER_TERMINAL_JOB_STATUSES
        await self._cleanup_worker_runtime(preserve_shell=preserve_shell)
        logger.info(
            "worker_batch completion handoff settled: unit=%s token=%d "
            "queue_state=%s command=%s command_state=%s outcome_status=%s "
            "shell=%s ambiguous_http=%s complete_calls=0 http_complete_calls=1",
            claim.unit_id,
            claim.lease_token,
            current.queue_state,
            current.command_id,
            current.command_state,
            resolved_status,
            "preserved" if preserve_shell else "retired",
            http_result_ambiguous,
        )
        return resolved_status or current.job_status

    def _observe_worker_renewal(self, job_id: str, renewal: WorkerRenewal) -> None:
        self._seed_worker_inboxes(
            job_id,
            list(renewal.pending_guidance),
            list(renewal.queued_replies),
        )
        if renewal.preempted:
            if not self._worker_preempted.is_set():
                logger.info(
                    "worker_batch preempt discovered: unit=%s status=%s",
                    job_id,
                    renewal.job_status,
                )
            self._worker_preempt_status = renewal.job_status
            self._worker_preempted.set()

    @staticmethod
    def _seed_worker_inboxes(
        job_id: str,
        pending_guidance: Any,
        queued_replies: Any,
    ) -> None:
        try:
            import src.api.dual_app as dual_app

            dual_app._replace_inbox(
                dual_app._guidance_inbox,
                job_id,
                pending_guidance,
                "Supervisor guidance",
            )
            dual_app._replace_inbox(
                dual_app._reply_inbox,
                job_id,
                queued_replies,
                "Queued replies",
            )
        except Exception:
            logger.debug("Worker steering inbox refresh failed", exc_info=True)

    @staticmethod
    def _worker_stop_is_recoverable(
        final_state: Dict[str, Any], freeze_type: Any
    ) -> bool:
        # An explicit human-facing freeze wins over a coincident retryable
        # error.  Those stops must remain visible/actionable through the
        # completion handler (condition 3 of the governing scope correction).
        if freeze_type and freeze_type not in AUTO_CONTINUE_FREEZE_TYPES:
            return False
        error = final_state.get("error")
        if isinstance(error, dict) and error.get("recoverable") is True:
            return True
        return bool(
            freeze_type in AUTO_CONTINUE_FREEZE_TYPES
            and freeze_type != FREEZE_TYPE_BATCH_BOUNDARY
        )

    @staticmethod
    def _worker_driver_error_state(
        message: str,
        *,
        job_id: str | None = None,
    ) -> Dict[str, Any]:
        return {
            "job_id": job_id,
            "should_stop": True,
            "goal_achieved": False,
            "error": {
                "type": "worker_driver_error",
                "recoverable": True,
                "message": str(message),
            },
        }

    @staticmethod
    def _worker_retry_exhausted_state(
        final_state: Dict[str, Any],
        *,
        attempts: int,
        max_attempts: int,
    ) -> Dict[str, Any]:
        """Turn the queue's last recoverable attempt into a visible give-up.

        The queue owns retry accounting, but the orchestrator remains the sole
        job-status authority. The final holder therefore reports a factual,
        non-recoverable terminal envelope while it still owns the exact token;
        it never writes ``jobs.status`` directly.
        """

        exhausted = dict(final_state)
        prior_error = final_state.get("error")
        error = dict(prior_error) if isinstance(prior_error, dict) else {}
        prior_freeze = final_state.get("freeze_data")
        reason = error.get("message")
        if not reason and isinstance(prior_freeze, dict):
            reason = prior_freeze.get("reason") or prior_freeze.get("error_summary")
        error.update(
            {
                "type": "worker_retry_exhausted",
                "recoverable": False,
                "message": (
                    f"Stateless worker recovery exhausted {attempts}/{max_attempts} "
                    f"queue attempts" + (f": {reason}" if reason else "")
                ),
            }
        )
        exhausted.update(
            {
                "should_stop": True,
                "goal_achieved": False,
                "error": error,
                "freeze_data": {
                    "freeze_type": "worker_retry_exhausted",
                    "reason": error["message"],
                    "attempts": attempts,
                    "max_attempts": max_attempts,
                    "prior_freeze": prior_freeze,
                },
            }
        )
        return exhausted

    async def _finish_external_worker_preempt(
        self,
        claim: WorkerClaim,
        *,
        http_complete_calls: int = 0,
        timing: dict[str, Any] | None = None,
    ) -> None:
        status = self._worker_preempt_status or "unknown"
        preserve_shell = status in _WORKER_PRESERVE_SHELL_STATUSES
        await self._cleanup_worker_runtime(preserve_shell=preserve_shell)
        started_at = time.perf_counter()
        try:
            state = await complete_worker_batch(
                self._db,
                unit_id=claim.unit_id,
                lease_token=claim.lease_token,
                consumed_seq=claim.unit.input_seq,
            )
        finally:
            self._add_worker_finish_timing(timing, started_at)
        logger.info(
            "worker_batch external stop: unit=%s token=%d status=%s "
            "queue_state=%s complete_calls=0 http_complete_calls=%d",
            claim.unit_id,
            claim.lease_token,
            status,
            state,
            http_complete_calls,
        )

    async def _cleanup_worker_runtime(self, *, preserve_shell: bool) -> None:
        agent = _pa()._agent
        if agent is not None:
            await agent.cleanup_worker_claim(preserve_shell=preserve_shell)

    async def _release_worker_claim(
        self,
        claim: WorkerClaim,
        *,
        reason: str,
        park_on_exhaustion: bool = True,
        timing: dict[str, Any] | None = None,
    ) -> None:
        if self._lease.lost.is_set():
            logger.info(
                "run_queue release: worker unit=%s token=%d reason=%s "
                "skipped after local ownership loss",
                claim.unit_id,
                claim.lease_token,
                reason,
            )
            return
        started_at = time.perf_counter()
        try:
            state = await release_worker_batch(
                self._db,
                unit_id=claim.unit_id,
                lease_token=claim.lease_token,
                park_on_exhaustion=park_on_exhaustion,
            )
        except Exception:
            logger.warning(
                "run_queue release failed for worker unit %s (reason=%s) — "
                "the lease will expire instead",
                claim.unit_id,
                reason,
                exc_info=True,
            )
            return
        finally:
            self._add_worker_finish_timing(timing, started_at)
        logger.info(
            "run_queue release: worker unit=%s token=%d reason=%s state=%s",
            claim.unit_id,
            claim.lease_token,
            reason,
            state,
        )

    async def _serve_claim(self, claim: ClaimedUnit) -> None:
        pa = _pa()
        unit_id = str(claim.unit_id)
        token = claim.lease_token
        logger.info(
            "run_queue claim: unit=%s kind=%s token=%d attempts=%d "
            "input_seq=%s consumed_seq=%s control_input_seq=%s "
            "control_consumed_seq=%s pod=%s",
            unit_id,
            claim.unit_kind,
            token,
            claim.attempts_since_completion,
            claim.input_seq,
            claim.consumed_seq,
            claim.control_input_seq,
            claim.control_consumed_seq,
            self._pod_name,
        )

        # (a) Skip-if-answered (§5.1): a steal can land between a
        # predecessor's final persist and its completion — the fence cannot
        # catch that (our lease is VALID), only the watermark can. No LLM.
        watermarks_answered = (
            claim.consumed_seq is not None
            and claim.input_seq is not None
            and claim.consumed_seq >= claim.input_seq
            and claim.control_consumed_seq >= claim.control_input_seq
        )
        pending_event = False
        if watermarks_answered:
            try:
                fetchval = getattr(self._db, "fetchval", None)
                if fetchval is not None:
                    pending_event = bool(
                        await fetchval(_PENDING_EVENT_EXISTS_SQL, claim.unit_id)
                    )
            except Exception:
                logger.warning(
                    "pending-event authority query failed for unit %s; "
                    "releasing instead of skipping",
                    unit_id,
                    exc_info=True,
                )
                await self._release(claim, reason="pending_event_query_failed")
                return
        if watermarks_answered and not pending_event:
            await self._detach_physical_before_transition("skip_if_answered")
            state = await complete_unit(
                self._db,
                unit_id=claim.unit_id,
                lease_token=token,
                consumed_seq=claim.consumed_seq,
            )
            logger.info(
                "run_queue complete: unit=%s consumed_seq=%s state=%s "
                "(skip-if-answered)",
                unit_id,
                claim.consumed_seq,
                state,
            )
            if state is None:
                await self._ack_terminal_claim_loss(claim)
            self._mark_warm(claim)
            return

        # (c) Independent heartbeat — spawned BEFORE the bundle fetch/attach,
        # which can themselves outlast the 60s lease TTL (MCP connect_all,
        # message-tail restore). Never an astream hook.
        claim_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(claim, claim_lost),
            name=f"lease-heartbeat-{unit_id[:8]}",
        )
        cancelled = False
        try:
            await self._serve_claim_inner(pa, claim, unit_id, token, claim_lost)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # Belt for every exception/cancellation path. Normal completion
            # and release paths stop it earlier, before mutating the queue;
            # this prevents a lease-scoped consumer leaking into warm idle.
            interrupt_turn = (
                pa._interrupt_owner_turn_id
                if pa._interrupt_owner_lease_token == token
                else None
            )
            if interrupt_turn is not None:
                try:
                    await self._close_interrupt_window(
                        pa,
                        claim,
                        target_turn_id=int(interrupt_turn),
                    )
                except (asyncio.CancelledError, Exception):
                    self._lease.mark_lost()
                    logger.warning(
                        "interrupt window cleanup failed; lease will not be "
                        "released (unit=%s token=%d turn=%d)",
                        unit_id,
                        token,
                        interrupt_turn,
                        exc_info=True,
                    )
            else:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pa._stop_thread_interrupt_watcher()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pa._stop_thread_control_watcher()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
            pa._turn_start_external_hook = None
            pa._turn_complete_external_hook = None
            if cancelled:
                # SIGTERM's hard-cancel is still an ownership transition. Do
                # not leave a live exact claim to expire after this Pod object
                # disappears: first drain every local/SFTP/background writer,
                # then release the exact token while this process is alive. A
                # concurrent End/reaper steal turns release into a no-op and
                # is acknowledged against its exact loss ledger below.
                await self._shutdown_dispose_cancelled_claim(claim)
            if claim_lost.is_set() or self._exact_claim_handle_lost(claim):
                if not await self._ack_terminal_claim_loss(claim):
                    # A pod that cannot durably settle its exact claimant debt
                    # must never return to the claim loop.  The reaper's
                    # UID-preconditioned eviction/absence path is the only
                    # safe successor owner from here.
                    self.request_stop()

    async def _shutdown_dispose_cancelled_claim(self, claim: ClaimedUnit) -> None:
        """Quiesce and durably dispose one SIGTERM-cancelled session claim."""

        try:
            await self._detach_physical_before_transition("shutdown_cancelled_claim")
        except BaseException:
            logger.critical(
                "shutdown could not quiesce exact stateless claimant; keeping "
                "the executor task alive for the Pod grace window "
                "(unit=%s token=%d)",
                claim.unit_id,
                claim.lease_token,
                exc_info=True,
            )
            raise

        last_error: BaseException | None = None
        for attempt in range(1, COMPLETE_RETRY_ATTEMPTS + 1):
            try:
                state = await release_unit(
                    self._db,
                    unit_id=claim.unit_id,
                    lease_token=claim.lease_token,
                    error=True,
                )
                if state is not None:
                    logger.info(
                        "run_queue release: unit=%s token=%d "
                        "reason=shutdown_cancelled state=%s",
                        claim.unit_id,
                        claim.lease_token,
                        state,
                    )
                    return
                # End/reaper won. Its thread->queue transaction publishes any
                # credential-bearing claimant debt before this SELECT can see
                # the advanced token; exact post-detach ACK is therefore safe.
                await self._ack_terminal_claim_loss(claim)
                return
            except asyncio.CancelledError:
                # The outer task is already cancelled; cleanup itself must be
                # cancellation-resistant until it has a durable disposition.
                continue
            except BaseException as exc:
                last_error = exc
                if attempt < COMPLETE_RETRY_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(
            "shutdown could not durably release its exact stateless claim"
        ) from last_error

    async def _serve_claim_inner(
        self,
        pa: Any,
        claim: ClaimedUnit,
        unit_id: str,
        token: int,
        claim_lost: asyncio.Event,
    ) -> None:
        # (d) Claim bundle — the pinned contract. On failure the token-guarded
        # release below is a no-op when the lease is genuinely gone (401/403 =
        # "treat as lease lost: release nothing" happens naturally via the
        # WHERE lease_token=$2 AND state='leased' guard in release_unit).
        timing: Dict[str, float] = {}
        t0 = time.perf_counter()
        try:
            bundle = await self._fetch_bundle(unit_id, token)
        except ClaimBundleError as e:
            if e.status_code == 404:
                logger.warning(
                    "claim bundle 404 for unit %s — unit vanished, dropping claim",
                    unit_id,
                )
                return
            logger.warning(
                "claim bundle %d for unit %s — releasing (token-guarded: a "
                "genuinely lost lease makes this a no-op): %s",
                e.status_code,
                unit_id,
                e.detail[:200] if e.detail else "",
            )
            await self._release(claim, reason=f"bundle_{e.status_code}")
            return
        except Exception as e:
            logger.warning("claim bundle fetch failed for unit %s: %s", unit_id, e)
            await self._release(claim, reason="bundle_error")
            return

        # The heartbeat starts before the slow credential bundle.  Its loss
        # signal is claim-local because the shared LeaseHandle may still point
        # at the previous warm session until that session is safely detached.
        # Never erase a loss by installing a fresh handle/event afterwards.
        if claim_lost.is_set():
            logger.warning(
                "lease lost during claim bundle: unit=%s token=%d; "
                "discarding credentials without attach or release",
                unit_id,
                token,
            )
            return

        timing["bundle"] = time.perf_counter() - t0
        attach = bundle.get("attach") or {}
        # Watermarks: the claim's values were read atomically inside the claim
        # statement — they are the authority; the bundle's copy is diagnostics.
        consumed_seq = claim.consumed_seq

        # (e) Attach with affinity + (b/D) scrub-on-claim.
        fingerprint = attach_fingerprint(attach)
        fresh_attach = False
        reuse = (
            pa._session is not None
            and pa._thread_id == unit_id
            and self._attached_fingerprint == fingerprint
            and pa._session.stateless_warm_reuse_safe
        )
        t0 = time.perf_counter()
        if reuse:
            # Same lite thread, unchanged config: reuse the live session. The
            # mutable LeaseHandle repoints every fenced writer (message
            # persists, journal flushes) at THIS claim's token in place.
            # Physical sessions deliberately miss this path: detach retires
            # their old backend before a fresh object receives the new token.
            self._lease.update(
                unit_id,
                token,
                executor_id=self._pod_name,
                pod_uid=self._pod_uid,
            )
            if claim_lost.is_set():
                self._lease.mark_lost()
                return
            logger.info(
                "session reuse (affinity): unit=%s fingerprint=%s",
                unit_id,
                fingerprint[:12],
            )
        else:
            if (
                pa._session is not None
                and pa._thread_id == unit_id
                and self._attached_bundle is not None
            ):
                # Same thread, changed fingerprint: name WHICH bundle paths
                # were volatile (§5.3.4 affinity-miss diagnosis; paths only,
                # never values).
                changed = fingerprint_diff_paths(self._attached_bundle, attach)
                logger.info("affinity miss: unit=%s changed_paths=%s", unit_id, changed)
            if pa._session is not None:
                # Detach BEFORE repointing the lease handle: the old writer's
                # close/drain must flush under the OLD unit's lease identity,
                # not this claim's.
                await self._detach_cached_session("claim_switch")
            timing["detach"] = time.perf_counter() - t0
            self._scrub_process_residue()
            self._lease.update(
                unit_id,
                token,
                executor_id=self._pod_name,
                pod_uid=self._pod_uid,
            )
            if claim_lost.is_set():
                self._lease.mark_lost()
                return
            t0 = time.perf_counter()
            try:
                await pa._attach_session(**attach)
            except Exception as e:
                logger.warning(
                    "attach failed for unit %s: %s", unit_id, e, exc_info=True
                )
                await self._detach_cached_session("attach_failed")
                await self._release(claim, reason="attach_failed")
                return
            self._attached_fingerprint = fingerprint
            self._attached_bundle = attach
            fresh_attach = True
        if claim_lost.is_set():
            self._lease.mark_lost()
            await self._detach_cached_session("lease_lost_during_attach")
            return
        timing["attach"] = time.perf_counter() - t0

        # Bind every remote tmux mutation to this monotonic queue token before
        # controls or user input can start tool work. Reuse invalidates the
        # previous claim's local tab cache; fresh attach records the token for
        # the first lazy shell initialization.
        if pa._session is not None:
            pa._session.set_shell_owner_token(token)

        # A claim may beat the reaper's post-steal journal transaction. Close
        # that abandoned generation and settle its exact interrupted input
        # before controls or pending-input selection can expose successor
        # output. The returned watermark is newer than the claim snapshot
        # precisely when an applied old-turn receipt consumed its target.
        t0 = time.perf_counter()
        try:
            (
                stale_count,
                recovered_consumed_seq,
            ) = await pa._reconcile_stale_thread_interrupts(lease_token=token)
        except Exception as e:
            logger.warning(
                "stale-interrupt recovery failed for unit %s; no successor "
                "input will be injected: %s",
                unit_id,
                e,
                exc_info=True,
            )
            await self._detach_cached_session("stale_interrupt_recovery_failed")
            await self._release(claim, reason="stale_interrupt_recovery_failed")
            return
        timing["interrupt_recovery"] = time.perf_counter() - t0
        if recovered_consumed_seq is not None:
            consumed_seq = max(
                consumed_seq if consumed_seq is not None else -1,
                int(recovered_consumed_seq),
            )
        if stale_count:
            logger.info(
                "session-interrupt claim recovery: unit=%s token=%d count=%d "
                "consumed_seq=%s total=%.3fs",
                unit_id,
                token,
                stale_count,
                consumed_seq,
                timing["interrupt_recovery"],
            )

        # Controls are consumed only by the exact serving owner. The initial
        # drain is synchronous so a control-only claim cannot take either
        # no-input completion edge; the watcher then stays live for mid-turn
        # mode changes until we stop it immediately before complete/release.
        t0 = time.perf_counter()
        try:
            drained_controls = await pa._start_thread_control_watcher(lease_token=token)
        except Exception as e:
            logger.warning(
                "control-inbox attach failed for unit %s; request remains "
                "pending for retry: %s",
                unit_id,
                e,
                exc_info=True,
            )
            # A strict journal fence can terminally close the attached writer.
            # Never leave that dead writer in the affinity cache for the next
            # claim; detach while the handle still carries this claim's token.
            await self._detach_cached_session("control_inbox_failed")
            await self._release(claim, reason="control_inbox_failed")
            return
        timing["controls"] = time.perf_counter() - t0
        if drained_controls:
            logger.info(
                "session-control claim drain: unit=%s token=%d count=%d total=%.3fs",
                unit_id,
                token,
                drained_controls,
                timing["controls"],
            )

        # (f) Oldest unanswered input.
        t0 = time.perf_counter()
        try:
            pending = await self._fetch_pending_rows(unit_id, consumed_seq)
        except Exception as e:
            logger.warning("pending-input query failed for unit %s: %s", unit_id, e)
            await self._release(claim, reason="pending_query_failed")
            return
        if not pending:
            # Enqueue without input (possible race) — nothing to answer.
            fallback = (
                claim.input_seq
                if claim.input_seq is not None
                else (consumed_seq if consumed_seq is not None else 0)
            )
            await pa._stop_thread_control_watcher()
            await self._detach_physical_before_transition("no_pending_complete")
            state = await complete_unit(
                self._db,
                unit_id=claim.unit_id,
                lease_token=token,
                consumed_seq=fallback,
            )
            logger.info(
                "run_queue complete: unit=%s consumed_seq=%s state=%s "
                "(no-pending-input)",
                unit_id,
                fallback,
                state,
            )
            if state is None:
                await self._ack_terminal_claim_loss(claim)
            self._mark_warm(claim)
            return

        target = pending[0]

        # (g) Strip restored pending copies — only a fresh attach ran the
        # restore; a reused session's memory holds no unanswered copies
        # (inputs land in the DB via the orchestrator, never in this pod's
        # memory outside a claim).
        if fresh_attach and pa._session is not None:
            removed = strip_restored_pending_humans(pa._session.messages, pending)
            if removed:
                logger.info(
                    "stripped %d restored pending message(s) before injection "
                    "(unit=%s)",
                    removed,
                    unit_id,
                )

        if not target["content"]:
            if target.get("delivery_id") is not None:
                logger.error(
                    "durable event input is empty; refusing to consume it "
                    "without provider admission (unit=%s seq=%s)",
                    unit_id,
                    target["seq"],
                )
                await pa._stop_thread_control_watcher()
                await self._detach_cached_session("empty_event_input")
                await self._release(claim, reason="empty_event_input")
                return
            # An empty row can never produce a turn (the loop skips empty
            # input, and the completion hook would never fire) — consume it.
            await pa._stop_thread_control_watcher()
            await self._detach_physical_before_transition("empty_input_complete")
            state = await complete_unit(
                self._db,
                unit_id=claim.unit_id,
                lease_token=token,
                consumed_seq=target["seq"],
            )
            logger.info(
                "run_queue complete: unit=%s consumed_seq=%s state=%s "
                "(empty-input row)",
                unit_id,
                target["seq"],
                state,
            )
            if state is None:
                await self._ack_terminal_claim_loss(claim)
            self._mark_warm(claim)
            return

        target_turn_id = target.get("turn_number")
        if (
            isinstance(target_turn_id, bool)
            or not isinstance(target_turn_id, int)
            or target_turn_id <= 0
            or pa._session is None
        ):
            logger.error(
                "pending input lacks an exact durable turn identity; refusing "
                "injection (unit=%s seq=%s turn=%r)",
                unit_id,
                target.get("seq"),
                target_turn_id,
            )
            await pa._stop_thread_control_watcher()
            await self._detach_cached_session("pending_turn_identity_invalid")
            await self._release(claim, reason="pending_turn_identity_invalid")
            return

        expected_previous_turn = int(target_turn_id) - 1
        if fresh_attach:
            # Restore includes unanswered human rows and therefore seeds
            # turn_count to the newest pending row. We just stripped those
            # copies; rewind the in-process counter to the predecessor of the
            # OLDEST pending row so on_turn_start opens admission for the
            # turn_number already durable on that exact human row. This must
            # happen before queue injection: persist_message runs only after
            # on_turn_start and cannot repair a crash in between.
            pa._session.turn_count = expected_previous_turn
        elif int(pa._session.turn_count) != expected_previous_turn:
            logger.error(
                "warm pending turn identity diverged; refusing injection "
                "(unit=%s session_turn=%s target_turn=%d)",
                unit_id,
                pa._session.turn_count,
                target_turn_id,
            )
            await pa._stop_thread_control_watcher()
            await self._detach_cached_session("pending_turn_identity_mismatch")
            await self._release(claim, reason="pending_turn_identity_mismatch")
            return

        timing["pending"] = time.perf_counter() - t0

        # (h) Inject — the row already exists (accept-time persist is
        # orchestrator-side on this lane), so ONLY the queue put + loop
        # arming from _accept_user_input are reproduced here; its persist is
        # deliberately not. The id makes the loop's own turn-start persist an
        # idempotent upsert onto the same row.
        turn_done = asyncio.Event()
        pa._turn_start_external_hook = lambda turn_id: self._arm_interrupt_window(
            pa,
            claim,
            target_turn_id=turn_id,
        )
        pa._turn_complete_external_hook = lambda _turn_id: turn_done.set()
        if not pa._ensure_persistent_loop_started("stateless_claim"):
            await self._detach_cached_session("loop_not_ready")
            await self._release(claim, reason="loop_not_ready")
            return
        delivery_id = target.get("delivery_id")
        delivery_claim_generation: int | None = None
        if delivery_id is not None:
            try:
                claimed_delivery = await self._db.claim_stateless_input_delivery(
                    thread_id=unit_id,
                    delivery_id=str(delivery_id),
                    lease_token=token,
                    executor_id=self._pod_name,
                    pod_uid=self._pod_uid,
                )
            except Exception:
                logger.warning(
                    "stateless event-delivery claim failed for unit %s token=%d",
                    unit_id,
                    token,
                    exc_info=True,
                )
                await self._detach_cached_session("event_delivery_claim_failed")
                await self._release(claim, reason="event_delivery_claim_failed")
                return
            if (
                claimed_delivery is None
                or int(claimed_delivery.get("seq") or -1) != int(target["seq"])
                or str(claimed_delivery.get("message_id") or "") != str(target["id"])
            ):
                logger.warning(
                    "stateless event-delivery authority changed before injection "
                    "(unit=%s token=%d)",
                    unit_id,
                    token,
                )
                await self._detach_cached_session("event_delivery_claim_lost")
                await self._release(claim, reason="event_delivery_claim_lost")
                return
            delivery_claim_generation = int(claimed_delivery["claim_generation"])
        loop_task = pa._loop_task
        queue_item = {"content": target["content"], "id": target["id"]}
        if delivery_id is not None and delivery_claim_generation is not None:
            queue_item.update(
                {
                    "role": "event",
                    "delivery_id": str(delivery_id),
                    "claim_generation": delivery_claim_generation,
                }
            )
        await pa._loop_user_queue.put(queue_item)

        # (i) Wait for the full-turn settlement hook (event, not a poll), the
        # lease-lost signal, or the loop dying under us. PersistentApp publishes
        # it only after transcript persistence and Git push/turn-ledger mapping,
        # so detach cannot cancel a half-recorded workspace turn.
        t0 = time.perf_counter()
        outcome = await self._await_turn(turn_done, loop_task)
        timing["turn"] = time.perf_counter() - t0

        if outcome == "turn_done":
            interrupt_turn_id = pa._interrupt_owner_turn_id
            if pa._interrupt_owner_lease_token != token or interrupt_turn_id is None:
                self._lease.mark_lost()
                logger.error(
                    "interrupt window identity missing at turn completion; "
                    "leaving lease to expire (unit=%s token=%d)",
                    unit_id,
                    token,
                )
                await self._detach_cached_session("interrupt_identity_missing")
                return
            # The transcript, memory, Git mapping, and workspace effects have
            # settled, but a protected-cloud generation may still be flushing.
            # Keep the exact interrupt gate open until that PUT reaches a
            # terminal outcome: consumed_seq must never become the no-replay
            # authority while an external writer is still live.
            t0 = time.perf_counter()
            await self._await_cloud_push(pa)
            timing["push"] = time.perf_counter() - t0
            completed_input_seq = max(
                int(target["seq"]),
                int(claim.consumed_seq) if claim.consumed_seq is not None else -1,
            )
            self._pending_settled_close = (
                str(claim.unit_id),
                int(token),
                int(interrupt_turn_id),
                completed_input_seq,
            )
            try:
                interrupt_closed = await self._close_interrupt_window(
                    pa,
                    claim,
                    target_turn_id=int(interrupt_turn_id),
                    completed_input_seq=completed_input_seq,
                )
            except Exception:
                self._lease.mark_lost()
                logger.error(
                    "atomic input checkpoint/final interrupt drain failed after "
                    "turn completion; "
                    "leaving lease to expire (unit=%s token=%d)",
                    unit_id,
                    token,
                    exc_info=True,
                )
                await self._detach_cached_session("interrupt_final_drain_failed")
                return
            if not interrupt_closed:
                logger.warning(
                    "lease lost: unit=%s token=%d while closing interrupt "
                    "window — successor owns completion",
                    unit_id,
                    token,
                )
                await self._detach_cached_session("interrupt_close_lost_lease")
                await self._ack_terminal_claim_loss(claim)
                return
            # Close the owner-consumption window before completion. A control
            # committed after this point advances control_input_seq; the
            # completion statement observes it and requeues atomically.
            await pa._stop_thread_control_watcher()
            t0 = time.perf_counter()
            await self._detach_physical_before_transition("turn_complete")
            timing["detach_final"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            state = await self._complete_with_retry(
                claim, consumed_seq=completed_input_seq
            )
            timing["complete"] = time.perf_counter() - t0
            if state is None:
                # Fenced out at completion: a steal beat us after the final
                # persist. The successor's claim decides what still needs
                # answering via the watermarks (skip-if-answered). Discard
                # the cached session — its in-memory tail may diverge from
                # what the successor persists.
                logger.warning(
                    "lease lost: unit=%s token=%d at completion (fenced out) — "
                    "successor decides via watermarks",
                    unit_id,
                    token,
                )
                await self._detach_cached_session("lease_lost_completion")
                await self._ack_terminal_claim_loss(claim)
                return
            logger.info(
                "run_queue complete: unit=%s consumed_seq=%d state=%s",
                unit_id,
                completed_input_seq,
                state,
            )
            logger.info(
                "turn timing: unit=%s mode=%s bundle=%.2fs detach=%.2fs "
                "attach=%.2fs controls=%.2fs pending=%.2fs turn=%.2fs push=%.2fs "
                "detach_final=%.2fs complete=%.2fs total=%.2fs",
                unit_id,
                "reuse" if reuse else "fresh",
                timing.get("bundle", 0.0),
                timing.get("detach", 0.0),
                timing.get("attach", 0.0),
                timing.get("controls", 0.0),
                timing.get("pending", 0.0),
                timing.get("turn", 0.0),
                timing.get("push", 0.0),
                timing.get("detach_final", 0.0),
                timing.get("complete", 0.0),
                sum(timing.values()),
            )
            if state != "error":
                self._mark_warm(claim)  # lite-only affinity + warm-TTL clock
        elif outcome == "lease_lost":
            # Polite fast path (§5.2): stop the turn the way the graceful
            # interrupt does; further fenced persists would only die at the
            # fence anyway. NO release_unit — the lease is not ours anymore.
            logger.warning(
                "lease lost: unit=%s token=%d — aborting turn politely "
                "(no release; no completion)",
                unit_id,
                token,
            )
            turn_id = (
                pa._interrupt_owner_turn_id
                if pa._interrupt_owner_lease_token == token
                else None
            )
            if turn_id is not None:
                await self._close_interrupt_window(
                    pa,
                    claim,
                    target_turn_id=int(turn_id),
                )
            else:
                await pa._stop_thread_interrupt_watcher()
            await pa._stop_thread_control_watcher()
            self._abort_turn_politely(pa)
            await self._wait_turn_unwind(turn_done, loop_task)
            # The aborted turn's in-memory tail may hold messages the fence
            # rejected — a later affinity reuse would diverge from the DB.
            # Discard; the next claim rebuilds from thread_messages (§5.2
            # torn-turn invariant).
            await self._detach_cached_session("lease_lost")
            # A fenced message/event/interrupt persist can signal the shared
            # LeaseHandle before the heartbeat observes the stolen row. ACK
            # directly after full unwind+detach; the exact marker matcher makes
            # this a no-op for ordinary reaper loss.
            await self._ack_terminal_claim_loss(claim)
        else:  # loop_died
            logger.warning(
                "persistent loop ended mid-turn for unit %s (%s) — releasing",
                unit_id,
                outcome,
            )
            turn_id = (
                pa._interrupt_owner_turn_id
                if pa._interrupt_owner_lease_token == token
                else None
            )
            if turn_id is not None:
                try:
                    interrupt_closed = await self._close_interrupt_window(
                        pa,
                        claim,
                        target_turn_id=int(turn_id),
                    )
                except Exception:
                    self._lease.mark_lost()
                    logger.error(
                        "interrupt final drain failed after loop death; "
                        "leaving lease to expire (unit=%s token=%d)",
                        unit_id,
                        token,
                        exc_info=True,
                    )
                    await self._detach_cached_session(
                        "loop_died_interrupt_drain_failed"
                    )
                    return
                if not interrupt_closed:
                    await self._detach_cached_session("loop_died_lost_lease")
                    return
            await self._release(claim, reason="loop_died")
            await self._detach_cached_session("loop_died")

    # ------------------------------------------------------------------
    # Pieces
    # ------------------------------------------------------------------

    async def _arm_interrupt_window(
        self,
        pa: Any,
        claim: ClaimedUnit,
        *,
        target_turn_id: int,
    ) -> None:
        """Arm the consumer, then publish exact-turn admission.

        The watcher starts before the public gate opens. A synchronous drain
        after opening closes the LISTEN-registration window; only then may the
        persistent loop emit ``turn.started``.
        """

        token = claim.lease_token
        opened = False
        try:
            await pa._start_thread_interrupt_watcher(
                lease_token=token,
                target_turn_id=int(target_turn_id),
            )
            opened = await open_interrupt_admission(
                self._db,
                unit_id=claim.unit_id,
                lease_token=token,
                turn_id=int(target_turn_id),
            )
            if not opened:
                self._lease.mark_lost()
                raise LeaseLostError(
                    "interrupt admission rejected stale lease/turn: "
                    f"{claim.unit_id}/{token}/{target_turn_id}"
                )
            await pa._drain_thread_interrupts(
                lease_token=token,
                target_turn_id=int(target_turn_id),
            )
        except BaseException:
            closed = False
            if opened:
                try:
                    closed = await close_interrupt_admission(
                        self._db,
                        unit_id=claim.unit_id,
                        lease_token=token,
                        turn_id=int(target_turn_id),
                    )
                except BaseException:
                    self._lease.mark_lost()
            try:
                await pa._stop_thread_interrupt_watcher()
            except BaseException:
                # A consumer that cannot be joined must never survive a queue
                # transition, even when the public gate did not open.
                self._lease.mark_lost()
            if opened and closed:
                try:
                    await pa._drain_thread_interrupts(
                        lease_token=token,
                        target_turn_id=int(target_turn_id),
                    )
                except BaseException:
                    # Never release a queue row after closing a window whose
                    # committed admission tail could not be settled.
                    self._lease.mark_lost()
            elif opened:
                self._lease.mark_lost()
            raise

    async def _close_interrupt_window(
        self,
        pa: Any,
        claim: ClaimedUnit,
        *,
        target_turn_id: int,
        completed_input_seq: int | None = None,
    ) -> bool:
        """Close admission, optionally checkpoint, then drain committed tail."""

        token = claim.lease_token
        pending_close = self._pending_settled_close
        if (
            completed_input_seq is None
            and pending_close is not None
            and pending_close[:3]
            == (str(claim.unit_id), int(token), int(target_turn_id))
        ):
            completed_input_seq = pending_close[3]
        attempts = 0
        try:
            while True:
                try:
                    closed = await close_interrupt_admission(
                        self._db,
                        unit_id=claim.unit_id,
                        lease_token=token,
                        turn_id=int(target_turn_id),
                        completed_input_seq=completed_input_seq,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if completed_input_seq is None:
                        raise
                    attempts += 1
                    logger.warning(
                        "atomic completed-input checkpoint failed; retrying "
                        "under live lease (unit=%s token=%d turn=%d attempt=%d)",
                        claim.unit_id,
                        token,
                        target_turn_id,
                        attempts,
                        exc_info=attempts <= COMPLETE_RETRY_ATTEMPTS,
                    )
                    # A transport exception may be an ambiguous commit. The
                    # SQL's already-consumed arm makes every retry idempotent;
                    # do not detach or expose a successor merely because a
                    # finite retry budget elapsed while heartbeat still owns
                    # this row.
                    await asyncio.sleep(0.5 * min(attempts, COMPLETE_RETRY_ATTEMPTS))
        except BaseException:
            self._lease.mark_lost()
            with contextlib.suppress(BaseException):
                await pa._stop_thread_interrupt_watcher()
            raise
        if pending_close is not None and pending_close[:3] == (
            str(claim.unit_id),
            int(token),
            int(target_turn_id),
        ):
            self._pending_settled_close = None
        await pa._stop_thread_interrupt_watcher()
        if not closed:
            self._lease.mark_lost()
            return False
        try:
            await pa._drain_thread_interrupts(
                lease_token=token,
                target_turn_id=int(target_turn_id),
            )
        except BaseException:
            self._lease.mark_lost()
            raise
        return True

    async def _fetch_bundle(self, unit_id: str, token: int) -> Dict[str, Any]:
        client = _pa()._orchestrator_client
        if client is None:
            raise RuntimeError("no orchestrator client — cannot fetch the claim bundle")
        return await client.get_claim_bundle(unit_id, token)

    async def _fetch_pending_rows(
        self, thread_id: str, consumed_seq: Optional[int]
    ) -> List[Dict[str, Any]]:
        rows = await self._db.fetch(
            _PENDING_INPUT_SQL,
            thread_id,
            consumed_seq if consumed_seq is not None else -1,
            PENDING_ROWS_LIMIT,
        )
        return [
            {
                "id": str(r["id"]),
                "seq": r["seq"],
                "content": r["content"] or "",
                "turn_number": r["turn_number"],
                "role": str(r.get("role") or "human"),
                "delivery_id": (
                    str(r.get("delivery_id"))
                    if r.get("delivery_id") is not None
                    else None
                ),
            }
            for r in rows
        ]

    async def _heartbeat_loop(
        self, claim: ClaimedUnit, claim_lost: asyncio.Event | None = None
    ) -> None:
        """Renew every HEARTBEAT_INTERVAL_SECONDS; on a lost lease, signal the
        driver via the shared handle. Independent of the graph loop by
        construction (its own task — a long tool call cannot starve it)."""
        unit_id = claim.unit_id
        token = claim.lease_token
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                renewed = await heartbeat_unit(
                    self._db, unit_id=unit_id, lease_token=token
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Transient DB trouble: keep trying — the lease has TTL/3
                # slack, and if the DB stays down the reaper takes over.
                logger.warning(
                    "lease heartbeat failed for unit %s (transient): %s",
                    unit_id,
                    e,
                )
                continue
            if renewed is None:
                logger.warning(
                    "lease lost: unit=%s token=%d (heartbeat renewal found no "
                    "leased row)",
                    unit_id,
                    token,
                )
                if claim_lost is not None:
                    claim_lost.set()
                # Do not poison a previous warm claim's handle while its
                # teardown still runs. Once this identity is installed, signal
                # both the shared writers and _await_turn directly.
                if self._lease.unit_id == str(
                    unit_id
                ) and self._lease.lease_token == int(token):
                    self._lease.mark_lost()
                return

    async def _await_turn(
        self, turn_done: asyncio.Event, loop_task: Optional[asyncio.Task]
    ) -> str:
        """First of: turn completed | lease lost | loop died. Never cancels
        the loop task itself."""
        lost_event = self._lease.lost
        done_waiter = asyncio.create_task(turn_done.wait())
        lost_waiter = asyncio.create_task(lost_event.wait())
        waiters = {done_waiter, lost_waiter}
        if loop_task is not None:
            waiters.add(loop_task)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (done_waiter, lost_waiter):
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter
        # Preference order: a completed turn wins even if the lease was lost
        # in the same instant — complete_unit is token-guarded, so a truly
        # lost lease turns the completion into the fenced-out path anyway.
        if turn_done.is_set():
            return "turn_done"
        if lost_event.is_set():
            return "lease_lost"
        return "loop_died"

    def _abort_turn_politely(self, pa: Any) -> None:
        """Interrupt the loop the way the interrupt verb does: graceful while
        a tool call is mid-invoke, hard otherwise (cancels a blocked LLM
        stream immediately)."""
        try:
            mode = "graceful" if pa._tool_inflight else "hard"
            pa._loop_interrupt_flag = mode
            if mode == "hard" and pa._hard_interrupt_event is not None:
                pa._hard_interrupt_event.set()
            logger.info("turn abort requested (mode=%s)", mode)
        except Exception:
            logger.warning("failed to signal turn abort", exc_info=True)

    async def _wait_turn_unwind(
        self, turn_done: asyncio.Event, loop_task: Optional[asyncio.Task]
    ) -> None:
        """Bounded wait for the interrupted turn to unwind. On timeout the
        detach path's loop-task cancel finishes the job."""
        done_waiter = asyncio.create_task(turn_done.wait())
        waiters: set = {done_waiter}
        if loop_task is not None:
            waiters.add(loop_task)
        try:
            await asyncio.wait(
                waiters,
                timeout=self._abort_grace_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not done_waiter.done():
                done_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await done_waiter

    async def _await_cloud_push(self, pa: Any) -> None:
        """Keep the lease until the turn-end push reaches a terminal outcome.

        A live task after ``complete_unit`` has no durable ownership and can
        race the next claimant's recovery/pull. The DB generation stays
        pending when a finished task reports failure, so the successor can
        safely replay; there is no safe equivalent for a still-running PUT.
        """
        task = pa._pending_cloud_push_task
        if task is None:
            return
        started = time.monotonic()
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "turn-end cloud push reached failure under the lease; "
                "durable generation remains pending for successor recovery",
                exc_info=True,
            )
        logger.info(
            "turn-end cloud push reached a terminal outcome in %.1fs under the lease",
            time.monotonic() - started,
        )

    async def _complete_with_retry(
        self, claim: ClaimedUnit, *, consumed_seq: int
    ) -> Optional[str]:
        """complete_unit with bounded retries. Returns the resulting state,
        None when fenced out, or the sentinel 'error' after exhausted retries.

        NEVER falls back to release_unit here: the answer is already
        persisted, and a release (which does not advance ``consumed_seq``)
        would hand the unit to another pod for a full re-answer. Leaving the
        lease to expire is the lesser evil — loud in the log, bounded by the
        reaper (§5.2 torn-turn: completion IS the watermark write)."""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, COMPLETE_RETRY_ATTEMPTS + 1):
            try:
                return await complete_unit(
                    self._db,
                    unit_id=claim.unit_id,
                    lease_token=claim.lease_token,
                    consumed_seq=consumed_seq,
                )
            except Exception as e:
                last_exc = e
                if attempt < COMPLETE_RETRY_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)
        logger.error(
            "run_queue complete FAILED after %d attempts for unit %s "
            "(consumed_seq=%s) — leaving the lease to expire: %s",
            COMPLETE_RETRY_ATTEMPTS,
            claim.unit_id,
            consumed_seq,
            last_exc,
        )
        return "error"

    async def _release(self, claim: ClaimedUnit, *, reason: str) -> None:
        """Voluntary error release (§5.1): default linear backoff, token-
        guarded (a genuinely lost lease makes this a recorded no-op)."""
        pa = _pa()
        # Warm/lite sessions skip physical detach, so teardown cannot be the
        # only push drain. No leased->queued transition may expose a successor
        # while this owner's external PUT is still live.
        await self._await_cloud_push(pa)
        if (
            pa._pending_cloud_push_task is not None
            and pa._pending_cloud_push_task.done()
        ):
            pa._pending_cloud_push_task = None
        if self._lease.lost.is_set():
            logger.info(
                "run_queue release: unit=%s token=%d reason=%s "
                "skipped after local ownership loss",
                claim.unit_id,
                claim.lease_token,
                reason,
            )
            if self._exact_claim_handle_lost(claim):
                await self._ack_terminal_claim_loss(claim)
            return
        # Exact lifecycle boundary: no consumer may survive the state change
        # from leased back to queued, even on an error path.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pa._stop_thread_control_watcher()
        await self._detach_physical_before_transition(f"release_{reason}")
        try:
            state = await release_unit(
                self._db,
                unit_id=claim.unit_id,
                lease_token=claim.lease_token,
                error=True,
            )
        except Exception:
            logger.warning(
                "run_queue release failed for unit %s (reason=%s) — the lease "
                "will expire instead",
                claim.unit_id,
                reason,
                exc_info=True,
            )
            return
        if state is None:
            logger.info(
                "run_queue release: unit=%s token=%d reason=%s "
                "(already fenced out — nothing to release)",
                claim.unit_id,
                claim.lease_token,
                reason,
            )
            await self._ack_terminal_claim_loss(claim)
        else:
            logger.info(
                "run_queue release: unit=%s token=%d reason=%s state=%s",
                claim.unit_id,
                claim.lease_token,
                reason,
                state,
            )

    async def _detach_physical_before_transition(self, reason: str) -> None:
        """Retire physical owner state while this claim is still exclusive."""
        session = _pa()._session
        if session is None or session.stateless_warm_reuse_safe:
            return
        await self._detach_cached_session(reason)

    def _exact_claim_handle_lost(self, claim: ClaimedUnit) -> bool:
        """Bind the shared mutable loss event to the claim being unwound."""

        return bool(
            self._lease.unit_id == str(claim.unit_id)
            and self._lease.lease_token == int(claim.lease_token)
            and self._lease.lost.is_set()
        )

    async def _ack_terminal_claim_loss(
        self,
        claim: ClaimedUnit,
    ) -> bool:
        """Acknowledge public-End fencing only after local I/O is quiescent.

        Reaper steals and ordinary lifecycle races reach this method too; the
        shared DB helper returns False unless an exact End marker names this
        old token and pod.  Physical sessions are detached once more as an
        idempotent belt so attach/bundle loss paths receive the same guarantee
        as the normal mid-turn unwind path.
        """

        pa = _pa()
        if pa._thread_id == str(claim.unit_id) and pa._session is not None:
            await self._detach_cached_session("terminal_claim_fenced")
        try:
            acknowledged = await acknowledge_session_claim_quiesced(
                self._db,
                thread_id=claim.unit_id,
                previous_lease_token=claim.lease_token,
                leased_by=self._pod_name,
                pod_uid=self._pod_uid,
            )
        except Exception:
            logger.warning(
                "terminal claimant-quiescence acknowledgement failed: "
                "unit=%s token=%d pod=%s",
                claim.unit_id,
                claim.lease_token,
                self._pod_name,
                exc_info=True,
            )
            return False
        if acknowledged:
            logger.info(
                "terminal claimant quiesced: unit=%s token=%d pod=%s",
                claim.unit_id,
                claim.lease_token,
                self._pod_name,
            )
        return bool(acknowledged)

    async def _detach_cached_session(self, reason: str) -> None:
        """Drop the cached session (and the affinity that pointed at it).

        Uses the same teardown /session/detach uses (_terminate_session) but
        NEVER marks the thread — on the stateless lane thread lifecycle is
        orchestrator-owned, and a pod-side 'ended' would force an epoch bump
        (client cache-wipe cascade) on the next claim's attach."""
        pa = _pa()
        self._attached_fingerprint = None
        self._attached_bundle = None
        self._prefer_unit_id = None
        self._warm_since = None
        pa._turn_complete_external_hook = None
        pa._turn_start_external_hook = None
        if pa._session is None:
            # A failed attach can leave _thread_id set with no session
            # (dual_app precedent) — clear it so the next claim starts clean.
            pa._thread_id = None
            return
        # Queue-claim detach retires only this Python/SFTP owner. Workspace
        # rclone/overlay residents are durable handoff state and may still be
        # flushing VFS bytes; destroying them on every claim boundary can lose
        # writeback. Public End owns a separate exact remote resident-retirement
        # acknowledgement before any emptyDir snapshot.
        preserve_workspace_daemons = True
        await pa._terminate_session(
            reason,
            mark_thread=False,
            preserve_shell=True,
            preserve_workspace_daemons=preserve_workspace_daemons,
        )
        if pa._session is not None:
            raise RuntimeError(
                "physical stateless session detach did not retire its owner"
            )

    def _scrub_process_residue(self) -> None:
        """§5.6 scrub-on-claim, executor half (deliverable D).

        _terminate_session already clears the session-scoped state (loop
        primitives, canvas awareness, subscribers, journal cursor, writer,
        ToolContext via session.cleanup). What it does NOT touch is the
        env/singleton-shaped process residue — exactly what a warm pod leaks
        across sequential tenants:

        * memory-embedding env keys + singleton, KB profile keys + singleton
          (also scrubbed pop-first inside _attach_session's
          _apply_session_embedding_env — this claim-time pass covers claims
          whose attach then fails before reaching that block);
        * the dual-mode guidance/reply inboxes (worker-plane; a stateless pod
          never runs jobs, but they are process-global dicts, so clear them).
        """
        try:
            pa = _pa()
            pa._apply_session_embedding_env(None)
        except Exception:
            logger.warning("embedding scrub failed (non-fatal)", exc_info=True)
        try:
            import src.api.dual_app as dual_app

            dual_app._guidance_inbox.clear()
            dual_app._reply_inbox.clear()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level lifecycle (used by persistent_app's stateless lifespan branch)
# ---------------------------------------------------------------------------

_executor: Optional[StatelessTurnExecutor] = None


async def start_stateless_executor() -> StatelessTurnExecutor:
    """Create + start the singleton executor (idempotent)."""
    global _executor
    if _executor is not None and _executor.running:
        return _executor
    executor = StatelessTurnExecutor()
    # Fail fast when the pool is missing — a stateless pod without its app-DB
    # pool can never serve a claim, and /ready must go 503, not lie.
    executor._db
    executor.start()
    _executor = executor
    return executor


async def stop_stateless_executor(timeout: Optional[float] = None) -> None:
    global _executor
    try:
        if _executor is not None:
            await _executor.stop(timeout)
            _executor = None
    finally:
        # Worker savers share one process pool.  It is lifespan-owned, never
        # closed at a batch boundary, and must still close when startup failed
        # after opening it but before publishing the executor singleton.
        from ..core.fenced_checkpointer import close_fenced_checkpointer_pool

        await close_fenced_checkpointer_pool()


def executor_running() -> bool:
    return _executor is not None and _executor.running
