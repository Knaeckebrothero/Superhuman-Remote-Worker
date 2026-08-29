"""``SubagentRuntime`` — one per parent (U3 WP2, plan B.3 / B.6 / B.8).

What the runtime owns, and the ``delegate_agent`` tool only calls:

- the roster lookup (``tool_config["subagents"]["roster"]`` — RESOLVED
  entries, plus ``default`` for a call that names no type);
- the handle mint — ``<type>-<4 hex>``, unique per parent for the life of the
  runtime (B.1);
- the worktree index counter (``.worktrees/<handle>`` is handle-named; the
  index keeps ``reader_env``'s port block allocation distinct per child);
- the per-parent ``asyncio.Semaphore(delegation.max_concurrent)`` — N calls
  in one batch run concurrently up to the cap and in waves above it;
- ``begin_batch(n)`` — the tool node stamps how many delegate calls the
  current batch carries so every envelope shares the parent's headroom by N
  (B.5);
- ``run_foreground(call)`` — build the child (``build_child``), run the
  driver on the brief, render the envelope, record through the
  ``SubagentLedger`` (``NullLedger`` until WP3), remember the result;
- idempotent re-execution keyed by ``(parent_job_id, tool_call_id)``: a
  repeated call (LangGraph re-running the tools node after a hard kill, once
  WP3's DB ledger makes the record survive rotation) returns the stored
  report without re-spending.

Nothing here assumes a worker: the parent is a :class:`ParentHost` and the
graph-specific stamps live in ``graph.py`` (B.13).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .budgets import ChildBudgets
from .child import SharedWriterGuard, SpawnRefused, build_child
from .driver import SubagentDriver, SubagentResult
from .envelope import build_envelope, report_path
from .fork import seed_fork_history
from .host import ParentHost
from .ledger import SUBAGENT_STATUSES, NullLedger, SubagentLedger

logger = logging.getLogger(__name__)

#: What ``run_in_background=true`` returns until U4 ships the control plane.
BACKGROUND_UNAVAILABLE = (
    "Error: run_in_background=true is not available yet — background "
    "subagents arrive with the control plane (wait_agent / message_agent / "
    "stop_agent / list_agents). Call again with run_in_background=false: the "
    "child runs now and its report returns as this tool's result."
)

#: How long a ledger write may block the parent (non-fatal past it).
_LEDGER_TIMEOUT_S = 5.0


@dataclass
class SubagentCall:
    """One ``delegate_agent`` invocation, as the tool hands it to the runtime."""

    tool_call_id: str
    subagent_type: str
    prompt: str
    description: str = ""
    #: ``None`` = the roster entry's own ``isolation`` (then ``shared``).
    isolation: Optional[str] = None
    fork: bool = False
    owned_paths: List[str] = field(default_factory=list)
    run_in_background: bool = False


@dataclass
class SubagentRecord:
    """What the runtime keeps per finished call (the idempotency value)."""

    key: Tuple[str, str]
    handle: str
    subagent_id: str
    subagent_type: str
    status: str
    envelope: str
    result: Optional[SubagentResult] = None


def _handle_base(subagent_type: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", str(subagent_type or "").lower()).strip("-")
    return base or "agent"


class SubagentRuntime:
    """The per-parent delegation runtime behind ``delegate_agent``."""

    def __init__(
        self,
        parent_context: Any,
        host: ParentHost,
        *,
        roster: Optional[Mapping[str, Mapping[str, Any]]] = None,
        default: Optional[str] = None,
        max_concurrent: int = 4,
        ledger: Optional[SubagentLedger] = None,
        llm_factory: Optional[Callable[[Any, Any], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        hex_source: Optional[Callable[[], str]] = None,
        driver_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.parent_context = parent_context
        self.host = host
        self.roster: Dict[str, Mapping[str, Any]] = {
            str(name): entry
            for name, entry in (roster or {}).items()
            if isinstance(entry, Mapping)
        }
        self.default = str(default) if default else None
        try:
            cap = int(max_concurrent)
        except (TypeError, ValueError):
            cap = 4
        self.max_concurrent = max(1, cap)
        self.ledger: SubagentLedger = ledger if ledger is not None else NullLedger()
        self._llm_factory = llm_factory
        self.clock = clock
        self._hex_source = hex_source or (lambda: secrets.token_hex(2))
        self._driver_kwargs = dict(driver_kwargs or {})

        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._writer_guard = SharedWriterGuard()
        self._handles: set[str] = set()
        self._worktree_index = 0
        self._batch_size = 1
        self._active: Dict[str, SubagentDriver] = {}
        self._records: Dict[Tuple[str, str], SubagentRecord] = {}
        self._inflight: Dict[Tuple[str, str], asyncio.Future] = {}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_context(
        cls, context: Any, host: ParentHost, **kwargs: Any
    ) -> "SubagentRuntime":
        """The runtime of a parent ``ToolContext``: roster / default from
        ``config["subagents"]``, the cap from ``config["delegation"]``."""
        config = getattr(context, "config", None) or {}
        subagents = config.get("subagents") or {}
        if not isinstance(subagents, Mapping):
            subagents = {}
        delegation = config.get("delegation") or {}
        if not isinstance(delegation, Mapping):
            delegation = {}
        kwargs.setdefault("roster", subagents.get("roster") or {})
        kwargs.setdefault("default", subagents.get("default"))
        raw_cap = delegation.get("max_concurrent")
        kwargs.setdefault("max_concurrent", 4 if raw_cap is None else raw_cap)
        return cls(context, host, **kwargs)

    # ------------------------------------------------------------------
    # Roster / handles / counters / batch
    # ------------------------------------------------------------------

    @property
    def roster_names(self) -> List[str]:
        return list(self.roster)

    def resolve_entry(
        self, subagent_type: Optional[str]
    ) -> Tuple[str, Mapping[str, Any]]:
        """``(name, resolved entry)`` for a type; the roster ``default`` when
        the call names none. Unknown → :class:`SpawnRefused` listing the roster."""
        name = str(subagent_type or "").strip() or (self.default or "")
        names = ", ".join(self.roster_names) or "(empty — this expert has no roster)"
        if not name:
            raise SpawnRefused(
                f"subagent_type is required — this expert's roster: {names}"
            )
        entry = self.roster.get(name)
        if entry is None:
            raise SpawnRefused(
                f"unknown subagent_type {name!r} — this expert's roster: {names}"
            )
        return name, entry

    def mint_handle(self, subagent_type: str) -> str:
        """``<type>-<4 hex>``, never reused within this parent."""
        base = _handle_base(subagent_type)
        for _ in range(256):
            handle = f"{base}-{self._hex_source()}"
            if handle not in self._handles:
                self._handles.add(handle)
                return handle
        raise RuntimeError(f"could not mint a unique handle for {subagent_type!r}")

    @property
    def handles(self) -> set[str]:
        return set(self._handles)

    def next_worktree_index(self) -> int:
        self._worktree_index += 1
        return self._worktree_index

    def begin_batch(self, n: int) -> None:
        """Stamped by the parent's tool node: N delegate calls share the
        return headroom of the current batch (B.5)."""
        try:
            size = int(n)
        except (TypeError, ValueError):
            size = 1
        self._batch_size = max(1, size)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def active(self) -> Dict[str, SubagentDriver]:
        return dict(self._active)

    @property
    def records(self) -> Dict[Tuple[str, str], SubagentRecord]:
        return dict(self._records)

    # ------------------------------------------------------------------
    # Foreground run
    # ------------------------------------------------------------------

    def _key(self, call: SubagentCall) -> Optional[Tuple[str, str]]:
        call_id = str(call.tool_call_id or "").strip()
        if not call_id:
            return None
        return (str(getattr(self.host, "job_id", "") or ""), call_id)

    async def run_foreground(self, call: SubagentCall) -> str:
        """Run one child to its end and return the envelope text (the
        ToolMessage content). Idempotent per ``(parent_job_id, tool_call_id)``."""
        key = self._key(call)
        if key is not None:
            record = self._records.get(key)
            if record is not None:
                logger.info(
                    "subagent %s: replaying the stored report for tool call %s",
                    record.handle,
                    key[1],
                )
                return record.envelope
            inflight = self._inflight.get(key)
            if inflight is not None:
                return await asyncio.shield(inflight)

        if call.run_in_background:
            return BACKGROUND_UNAVAILABLE
        try:
            name, entry = self.resolve_entry(call.subagent_type)
        except SpawnRefused as refused:
            return f"Error: {refused}"

        if key is None:
            return await self._spawn(key, name, entry, call)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            envelope = await self._spawn(key, name, entry, call)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(envelope)
            return envelope
        finally:
            self._inflight.pop(key, None)

    async def _spawn(
        self,
        key: Optional[Tuple[str, str]],
        name: str,
        entry: Mapping[str, Any],
        call: SubagentCall,
    ) -> str:
        isolation = str(call.isolation or entry.get("isolation") or "shared")
        handle = self.mint_handle(name)
        subagent_id = str(uuid.uuid4())

        async with self._semaphore:
            try:
                build = await build_child(
                    entry,
                    parent_context=self.parent_context,
                    host=self.host,
                    handle=handle,
                    subagent_type=name,
                    isolation=isolation,
                    owned_paths=list(call.owned_paths or []),
                    writer_guard=self._writer_guard,
                    llm_factory=self._llm_factory,
                    worktree_index=(
                        self.next_worktree_index() if isolation == "worktree" else None
                    ),
                )
            except SpawnRefused as refused:
                return f"Error: {refused}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "subagent %s (%s): build failed: %s",
                    handle,
                    name,
                    exc,
                    exc_info=True,
                )
                return (
                    f"Error: subagent {handle} ({name}) could not be started — "
                    f"{type(exc).__name__}: {exc}"
                )

            budgets = ChildBudgets.from_entry(entry, name)
            messages = None
            if call.fork:
                messages = seed_fork_history(
                    self.host.fork_source(),
                    child_model=getattr(
                        getattr(build.config, "llm", None), "model", None
                    ),
                    parent_model=self._parent_model(),
                )
            driver = SubagentDriver(
                build,
                host=self.host,
                parent_context=self.parent_context,
                subagent_id=subagent_id,
                budgets=budgets,
                ledger=self.ledger,
                clock=self.clock,
                parent_tool_call_id=call.tool_call_id or None,
                messages=messages,
                **self._driver_kwargs,
            )
            self._active[handle] = driver
            await self._ledger_open(
                subagent_id,
                status="running",
                handle=handle,
                subagent_type=name,
                parent_job_id=getattr(self.host, "job_id", None),
                parent_thread_id=getattr(self.host, "thread_id", None),
                parent_tool_call_id=call.tool_call_id or None,
                isolation=build.isolation,
                write_policy=build.write_policy,
                brief_description=call.description,
                fork=bool(call.fork),
            )
            logger.info(
                "subagent %s (%s) spawned: isolation=%s write_policy=%s tools=%d "
                "budgets=%d turns/%d tokens fork=%s",
                handle,
                name,
                build.isolation,
                build.write_policy,
                len(build.tools),
                budgets.max_turns,
                budgets.max_tokens,
                call.fork,
            )
            try:
                result = await driver.run(call.prompt)
            except asyncio.CancelledError:
                await self._ledger_update(
                    subagent_id,
                    status="cancelled",
                    outcome="cancelled",
                    turns=driver.provider_calls,
                    tokens=driver.tokens,
                    error="the parent's batch was cancelled",
                )
                raise
            except Exception as exc:
                logger.error(
                    "subagent %s (%s): run failed: %s", handle, name, exc, exc_info=True
                )
                await self._ledger_update(
                    subagent_id, status="error", outcome="error", error=str(exc)
                )
                return (
                    f"Error: subagent {handle} ({name}) failed — "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                self._active.pop(handle, None)
                try:
                    await driver.close()
                except Exception:  # pragma: no cover - best effort
                    logger.warning("subagent %s: close failed", handle, exc_info=True)

        envelope = build_envelope(
            result,
            workspace_manager=getattr(self.parent_context, "workspace_manager", None),
            entry_budget=budgets.return_budget_tokens,
            probe=self.host.context_probe(),
            n_in_batch=self._batch_size,
            model=self._parent_model(),
        )
        status = result.kind if result.kind in SUBAGENT_STATUSES else "error"
        spilled = report_path(handle) if self._report_exists(handle) else None
        await self._ledger_update(
            subagent_id,
            status=status,
            outcome=result.status,
            turns=result.turns,
            tokens=result.tokens,
            report_path=spilled,
            error=result.error,
        )
        logger.info(
            "subagent %s (%s) %s: %d turns, %d tokens, %.1fs",
            handle,
            name,
            result.status,
            result.turns,
            result.tokens,
            result.duration,
        )
        if key is not None:
            self._records[key] = SubagentRecord(
                key=key,
                handle=handle,
                subagent_id=subagent_id,
                subagent_type=name,
                status=status,
                envelope=envelope,
                result=result,
            )
        return envelope

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Stop every running child (job teardown; foreground children never
        outlive their batch, so this is a safety net)."""
        drivers = list(self._active.values())
        self._active.clear()
        for driver in drivers:
            try:
                await driver.close()
            except Exception:  # pragma: no cover - best effort
                logger.warning(
                    "subagent %s: close failed", driver.handle, exc_info=True
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parent_model(self) -> Optional[str]:
        live = getattr(self.host, "live_llm_config", None)
        model = getattr(live, "model", None)
        if model:
            return str(model)
        cfg = getattr(self.parent_context, "_llm_config", None)
        model = getattr(cfg, "model", None)
        return str(model) if model else None

    def _report_exists(self, handle: str) -> bool:
        ws = getattr(self.parent_context, "workspace_manager", None)
        exists = getattr(ws, "exists", None)
        if not callable(exists):
            return False
        try:
            return bool(exists(report_path(handle)))
        except Exception:
            return False

    async def _ledger_open(self, subagent_id: str, **fields: Any) -> None:
        opener = getattr(self.ledger, "open", None)
        if not callable(opener):
            await self._ledger_update(subagent_id, **fields)
            return
        try:
            await asyncio.wait_for(opener(subagent_id, **fields), _LEDGER_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("subagent ledger open failed (non-fatal): %s", exc)

    async def _ledger_update(self, subagent_id: str, **fields: Any) -> None:
        try:
            await asyncio.wait_for(
                self.ledger.update(subagent_id, **fields), _LEDGER_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("subagent ledger update failed (non-fatal): %s", exc)


__all__ = [
    "BACKGROUND_UNAVAILABLE",
    "SubagentCall",
    "SubagentRecord",
    "SubagentRuntime",
]
