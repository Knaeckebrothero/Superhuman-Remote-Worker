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
import socket
import time
from typing import Any, Dict, List, Optional

from .lease_context import LeaseHandle, current_lease
from .orchestrator_client import ClaimBundleError
from ..shared.run_queue import (
    HEARTBEAT_INTERVAL_SECONDS,
    UNIT_KIND_SESSION_TURN,
    ClaimedUnit,
    claim_unit,
    complete_unit,
    heartbeat_unit,
    release_unit,
)

logger = logging.getLogger(__name__)

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

# Oldest-unanswered query (§5.1 watermarks + M0b role vocabulary): LangChain
# roles ('human'/'ai'); rewound rows are dead timelines and must not be
# answered. ``seq > COALESCE(consumed_seq, -1)`` — a NULL consumed watermark
# means nothing was ever answered, so the oldest human row qualifies.
_PENDING_INPUT_SQL = """
    SELECT id, seq, content
    FROM thread_messages
    WHERE thread_id = $1
      AND role = 'human'
      AND rewound_at IS NULL
      AND seq > $2
    ORDER BY seq ASC
    LIMIT $3
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
    remaining = list(pending_rows)
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
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
        idle_backoff_seconds: float = IDLE_POLL_BACKOFF_SECONDS,
        idle_polls_before_backoff: int = IDLE_POLLS_BEFORE_BACKOFF,
        jitter: float = POLL_JITTER,
        cloud_push_wait_seconds: float = CLOUD_PUSH_WAIT_SECONDS,
        abort_grace_seconds: float = TURN_ABORT_GRACE_SECONDS,
        warm_session_idle_ttl_seconds: float = WARM_SESSION_IDLE_TTL_SECONDS,
    ) -> None:
        self._pod_name = (
            pod_name or os.getenv("POD_NAME") or socket.gethostname() or "agent"
        )
        self._idle_poll_seconds = idle_poll_seconds
        self._idle_backoff_seconds = idle_backoff_seconds
        self._idle_polls_before_backoff = idle_polls_before_backoff
        self._jitter = jitter
        self._cloud_push_wait_seconds = cloud_push_wait_seconds
        self._abort_grace_seconds = abort_grace_seconds
        self._warm_session_idle_ttl = warm_session_idle_ttl_seconds
        self._warm_since: Optional[float] = None

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
            "backoff=%.2fs/%d heartbeat=%ss",
            self._pod_name,
            self._idle_poll_seconds,
            self._idle_backoff_seconds,
            self._idle_polls_before_backoff,
            HEARTBEAT_INTERVAL_SECONDS,
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
            if claim is None:
                idle_polls += 1
                await self._expire_warm_session()
                await self._sleep_interruptible(self._idle_delay(idle_polls))
                continue
            idle_polls = 0
            if self._stop.is_set():
                # Claimed on the stop boundary — hand it straight back for
                # another pod (no backoff, not an error).
                with contextlib.suppress(Exception):
                    state = await release_unit(
                        self._db,
                        unit_id=claim.unit_id,
                        lease_token=claim.lease_token,
                        backoff_seconds=0.0,
                    )
                    logger.info(
                        "run_queue release: unit=%s token=%d reason=shutting_down "
                        "state=%s",
                        claim.unit_id,
                        claim.lease_token,
                        state,
                    )
                break
            try:
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
        if (
            claim.consumed_seq is not None
            and claim.input_seq is not None
            and claim.consumed_seq >= claim.input_seq
            and claim.control_consumed_seq >= claim.control_input_seq
        ):
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
            self._mark_warm(claim)
            return

        # (c) Independent heartbeat — spawned BEFORE the bundle fetch/attach,
        # which can themselves outlast the 60s lease TTL (MCP connect_all,
        # message-tail restore). Never an astream hook.
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(claim),
            name=f"lease-heartbeat-{unit_id[:8]}",
        )
        try:
            await self._serve_claim_inner(pa, claim, unit_id, token)
        finally:
            # Belt for every exception/cancellation path. Normal completion
            # and release paths stop it earlier, before mutating the queue;
            # this prevents a lease-scoped consumer leaking into warm idle.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pa._stop_thread_control_watcher()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
            pa._turn_complete_external_hook = None

    async def _serve_claim_inner(
        self, pa: Any, claim: ClaimedUnit, unit_id: str, token: int
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
            self._lease.update(unit_id, token)
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
            self._lease.update(unit_id, token)
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
        timing["attach"] = time.perf_counter() - t0

        # Bind every remote tmux mutation to this monotonic queue token before
        # controls or user input can start tool work. Reuse invalidates the
        # previous claim's local tab cache; fresh attach records the token for
        # the first lazy shell initialization.
        if pa._session is not None:
            pa._session.set_shell_owner_token(token)

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
            self._mark_warm(claim)
            return

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

        target = pending[0]
        if not target["content"]:
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
            self._mark_warm(claim)
            return

        timing["pending"] = time.perf_counter() - t0

        # (h) Inject — the row already exists (accept-time persist is
        # orchestrator-side on this lane), so ONLY the queue put + loop
        # arming from _accept_user_input are reproduced here; its persist is
        # deliberately not. The id makes the loop's own turn-start persist an
        # idempotent upsert onto the same row.
        turn_done = asyncio.Event()
        pa._turn_complete_external_hook = lambda _turn_id: turn_done.set()
        if not pa._ensure_persistent_loop_started("stateless_claim"):
            await self._detach_cached_session("loop_not_ready")
            await self._release(claim, reason="loop_not_ready")
            return
        loop_task = pa._loop_task
        await pa._loop_user_queue.put(
            {"content": target["content"], "id": target["id"]}
        )

        # (i) Wait for the turn's completion hook (event, not a poll), the
        # lease-lost signal, or the loop dying under us.
        t0 = time.perf_counter()
        outcome = await self._await_turn(turn_done, loop_task)
        timing["turn"] = time.perf_counter() - t0

        if outcome == "turn_done":
            t0 = time.perf_counter()
            await self._await_cloud_push(pa)
            timing["push"] = time.perf_counter() - t0
            # Close the owner-consumption window before completion. A control
            # committed after this point advances control_input_seq; the
            # completion statement observes it and requeues atomically.
            await pa._stop_thread_control_watcher()
            t0 = time.perf_counter()
            await self._detach_physical_before_transition("turn_complete")
            timing["detach_final"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            state = await self._complete_with_retry(claim, consumed_seq=target["seq"])
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
                return
            logger.info(
                "run_queue complete: unit=%s consumed_seq=%d state=%s",
                unit_id,
                target["seq"],
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
            await pa._stop_thread_control_watcher()
            self._abort_turn_politely(pa)
            await self._wait_turn_unwind(turn_done, loop_task)
            # The aborted turn's in-memory tail may hold messages the fence
            # rejected — a later affinity reuse would diverge from the DB.
            # Discard; the next claim rebuilds from thread_messages (§5.2
            # torn-turn invariant).
            await self._detach_cached_session("lease_lost")
        else:  # loop_died
            logger.warning(
                "persistent loop ended mid-turn for unit %s (%s) — releasing",
                unit_id,
                outcome,
            )
            await self._release(claim, reason="loop_died")
            await self._detach_cached_session("loop_died")

    # ------------------------------------------------------------------
    # Pieces
    # ------------------------------------------------------------------

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
            {"id": str(r["id"]), "seq": r["seq"], "content": r["content"] or ""}
            for r in rows
        ]

    async def _heartbeat_loop(self, claim: ClaimedUnit) -> None:
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
        if pa._session is None:
            # A failed attach can leave _thread_id set with no session
            # (dual_app precedent) — clear it so the next claim starts clean.
            pa._thread_id = None
            return
        session = pa._session
        try:
            await pa._terminate_session(
                reason,
                mark_thread=False,
                preserve_shell=True,
            )
        except Exception:
            logger.warning(
                "session detach (%s) failed — force-clearing session globals",
                reason,
                exc_info=True,
            )
            # A teardown exception must not leave a cancelled sync tool holding
            # an admitted/reconnectable physical backend after the queue moves.
            with contextlib.suppress(Exception):
                session.retire_shell_owner()
            with contextlib.suppress(Exception):
                backend = session._unwrapped_backend()
                retire = getattr(backend, "retire", None)
                if retire is not None:
                    retire()
            # Last resort: never let a wedged teardown brick the claim loop.
            pa._session = None
            pa._thread_id = None

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
    if _executor is None:
        return
    await _executor.stop(timeout)
    _executor = None


def executor_running() -> bool:
    return _executor is not None and _executor.running
