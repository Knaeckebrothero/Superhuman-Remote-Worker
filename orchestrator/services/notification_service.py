"""Unified notification service.

Two generations live side by side while the cutover runs
(knowledge-base/knowledge/features/unified_notification_system.md):

* ``record()`` — the new front door (D1: callers *record* what happened and
  who has a stake; they never choose a channel). It writes one durable feed
  row per recipient (D2/D3), broadcasts the ``notification`` SSE frame once,
  performs the zero-delay channel deliveries of the row's severity class
  with a claim-before-send ledger so a replayed completion effect or a
  dual-leader retry can never send twice (D10), and writes the class's
  *deferred* steps to ``notification_steps`` in the same transaction as the
  row (D5/D6: "wait the officer's window, then mail unless somebody looked
  or somebody settled it"). ``services/notification_steps.py`` runs those
  when due; :meth:`send_step_group` is the send half it calls back into.

* ``dispatch()`` and the ``notify_*`` helpers — the legacy fan-out (email +
  webhooks + a transient ``new_message`` frame, no durable row). Still used by
  the producers slice 3 migrates; every remaining call site is enumerated in
  ``policy/notification_producers.txt`` and the count may only go down.

Follows the NatsBridge graceful degradation pattern: fully optional, all
operations are no-ops when unconfigured.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from services.notification_catalog import (
    DEFERRED_STEP_INDEX_BASE,
    DELAY_OFFICER_RESPONSE,
    ESCALATION_MINUTES_BOUNDS,
    NO_OFFICER_DELAY_MINUTES,
    RECIPIENT_KINDS,
    WEBHOOK_CHANNELS,
    ActionContext,
    action_handler,
    batch_key_for,
    bucket_due_at,
    bypasses_quiet_hours,
    category_spec,
    channel_enabled,
    normalize_severity,
    notification_id as mint_notification_id,
    quiet_hours_window,
    serialize_actions,
    serialize_notification,
    serialize_step,
    source_probe,
    steps_for,
)
from services.webhook_transports import (
    DiscordWebhookTransport,
    NotificationPayload,
    NtfyTransport,
    SlackWebhookTransport,
)

logger = logging.getLogger(__name__)


class NotificationNotFound(LookupError):
    """No such row for this recipient (the endpoint answers 404)."""


class ActionNotDeclared(ValueError):
    """The row does not carry that action (400)."""


class ActionUnregistered(RuntimeError):
    """The category declares the action but nothing handles it — a wiring bug
    that must be loud, like ``_run_completion_effect``'s registry gate (500)."""


@dataclass(frozen=True, slots=True)
class RecordResult:
    notification_id: str
    inserted: bool
    deliveries: dict[str, Any]


class NotificationService:
    """Multi-channel notification dispatcher.

    Reads user channel preferences from ``users.settings.communication.channels``
    and dispatches to each enabled channel. Queues notifications during quiet hours.
    """

    def __init__(self) -> None:
        self._db: Any = None
        self._email_service: Any = None
        self._notification_feed: Any = None
        self._available = False

        self._cockpit_url = os.getenv(
            "COCKPIT_EXTERNAL_URL", "http://localhost:4200"
        ).rstrip("/")

        # Initialize transports (each checks its own env vars)
        self._transports = {
            "ntfy": NtfyTransport(),
            "slack_webhook": SlackWebhookTransport(),
            "discord_webhook": DiscordWebhookTransport(),
        }

        configured = [name for name, t in self._transports.items() if t.is_configured]
        if configured:
            logger.info("Webhook transports configured: %s", ", ".join(configured))
        else:
            logger.info("No webhook transports configured. Email-only notifications.")

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def configured_transports(self) -> list[str]:
        """Names of transports that have valid configuration."""
        return [name for name, t in self._transports.items() if t.is_configured]

    def connect(
        self,
        db: Any,
        email_service: Any,
        notification_feed: Any = None,
    ) -> None:
        """Store references to collaborating services.

        Args:
            db: PostgresDB instance
            email_service: EmailService instance for SMTP delivery
            notification_feed: NotificationFeedService for SSE broadcast (optional)
        """
        self._db = db
        self._email_service = email_service
        self._notification_feed = notification_feed
        self._available = True
        logger.info("NotificationService initialized")

    # =========================================================================
    # The unified feed (record / act / engagement / resolution)
    # =========================================================================

    async def record(
        self,
        *,
        recipient_id: str,
        category: str,
        dedup_key: str,
        subject: str,
        body: str = "",
        recipient_kind: str = "user",
        source_kind: str | None = None,
        source_id: str | None = None,
        severity: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        action_params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RecordResult:
        """Record that something happened that ``recipient`` has a stake in.

        Idempotent on ``(recipient_kind, recipient_id, dedup_key)``: a replay
        finds the existing row, broadcasts nothing, and re-attempts only the
        channel deliveries that never got a ``sent`` claim. Callers never see
        or influence delivery beyond the returned outcome dict (D1).

        The severity class decides what happens beyond the feed row: its
        immediate steps run here; its deferred steps (``normal``: wait the
        officer's response window, then mail only if ``not_seen`` and
        ``not_resolved``) are written with the row and run by the sweeper.
        """
        if not self._available:
            raise RuntimeError("NotificationService not initialized")
        if recipient_kind not in RECIPIENT_KINDS:
            raise ValueError(f"unknown recipient_kind {recipient_kind!r}")
        if not dedup_key:
            raise ValueError("dedup_key is required")
        if (source_kind is None) != (source_id is None):
            raise ValueError("source_kind and source_id go together")
        spec = category_spec(category)
        resolved_severity = normalize_severity(spec, severity)

        nid = str(mint_notification_id(recipient_kind, str(recipient_id), dedup_key))
        row: dict[str, Any] = {
            "id": nid,
            "recipient_kind": recipient_kind,
            "recipient_id": str(recipient_id),
            "category": category,
            "severity": resolved_severity,
            "subject": subject,
            "body": body or "",
            "source_kind": source_kind,
            "source_id": str(source_id) if source_id is not None else None,
            "dedup_key": dedup_key,
            "actions": (
                list(actions)
                if actions is not None
                else serialize_actions(spec, action_params)
            ),
            "payload": dict(payload or {}),
            # Provisional: the DB default is authoritative; the cockpit upserts
            # by id and the next feed load corrects the millisecond drift.
            "created_at": datetime.now(timezone.utc),
            "seen_at": None,
            "read_at": None,
            "interacted_at": None,
            "resolved_at": None,
            "resolved_by": None,
            "archived_at": None,
        }

        steps = steps_for(spec, resolved_severity)
        planned: list[dict[str, Any]] = []
        if recipient_kind == "user" and any(not s.immediate for s in steps):
            planned = await self._plan_deferred_steps(row, steps)

        stored_id, inserted = await self._persist_notification(
            row, steps=planned or None
        )
        row["id"] = stored_id
        if inserted:
            self._broadcast_notification(row)
        deliveries = await self._deliver_immediate(row, spec, inserted=inserted)
        if planned:
            deliveries["scheduled"] = {
                p["step_kind"]: p["due_at"].isoformat() for p in planned
            }
        logger.info(
            "notification %s %s for %s:%s (%s/%s)%s",
            stored_id[:8],
            "recorded" if inserted else "replayed",
            recipient_kind,
            str(recipient_id)[:8],
            category,
            resolved_severity,
            (
                f" — {len(planned)} step(s) due {planned[0]['due_at'].isoformat()}"
                if planned
                else ""
            ),
        )
        return RecordResult(stored_id, inserted, deliveries)

    async def act(
        self,
        *,
        notification_id: str,
        user: dict[str, Any],
        action_type: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a declared action through its registered handler and stamp the
        row. The center posts ``{action_type, params}`` and knows nothing
        else; category meaning lives entirely in the handler (D7)."""
        row = await self._db.get_notification(notification_id)
        if (
            not row
            or row.get("recipient_kind") != "user"
            or str(row.get("recipient_id")) != str(user.get("id"))
        ):
            raise NotificationNotFound(notification_id)
        declared = next(
            (a for a in row.get("actions") or [] if a.get("type") == action_type),
            None,
        )
        if declared is None:
            raise ActionNotDeclared(action_type)
        handler = action_handler(row["category"], action_type)
        if handler is None:
            raise ActionUnregistered(f"{row['category']}/{action_type}")

        # Server-declared params win over anything the client sends; the
        # client contributes only the collected input (feedback, reason, …).
        merged = dict(params or {})
        merged.update(declared.get("params") or {})
        context = ActionContext(notification=row, user=user, params=merged, db=self._db)
        result = await handler(context)

        updated = await self._db.stamp_notification_interacted(row["id"])
        if result.resolve:
            updated = await self._db.resolve_notification(
                row["id"], resolved_by=result.resolved_by or f"user:{user.get('id')}"
            )
        updated = updated or row
        self._broadcast_update(
            str(updated["recipient_id"]),
            {
                "id": str(updated["id"]),
                **{
                    k: serialize_notification(updated)[k]
                    for k in (
                        "seen_at",
                        "read_at",
                        "interacted_at",
                        "resolved_at",
                        "resolved_by",
                    )
                },
            },
        )
        return {
            "result": result.result,
            "notification": serialize_notification(updated),
        }

    async def mark_seen(
        self, *, recipient_kind: str, recipient_id: str, ids: list[str]
    ) -> list[str]:
        stamped = await self._db.mark_notifications_seen(
            recipient_kind=recipient_kind, recipient_id=recipient_id, ids=ids
        )
        for entry in stamped:
            seen_at = entry.get("seen_at")
            self._broadcast_update(
                str(recipient_id),
                {
                    "id": str(entry["id"]),
                    "seen_at": seen_at.isoformat() if seen_at else None,
                },
            )
        return [str(entry["id"]) for entry in stamped]

    async def mark_read(
        self, *, recipient_kind: str, recipient_id: str, notification_id: str
    ) -> dict[str, Any] | None:
        row = await self._db.mark_notification_read_v2(
            recipient_kind=recipient_kind,
            recipient_id=recipient_id,
            notification_id=notification_id,
        )
        if row is None:
            return None
        wire = serialize_notification(row)
        self._broadcast_update(
            str(recipient_id),
            {"id": wire["id"], "seen_at": wire["seen_at"], "read_at": wire["read_at"]},
        )
        return wire

    async def archive(
        self, *, recipient_kind: str, recipient_id: str, notification_id: str
    ) -> dict[str, Any] | None:
        row = await self._db.archive_notification(
            recipient_kind=recipient_kind,
            recipient_id=recipient_id,
            notification_id=notification_id,
        )
        if row is None:
            return None
        wire = serialize_notification(row)
        self._broadcast_update(
            str(recipient_id), {"id": wire["id"], "archived_at": wire["archived_at"]}
        )
        return wire

    async def resolve_source(
        self, source_kind: str, source_id: str, *, resolved_by: str
    ) -> list[str]:
        """The underlying thing was settled — by a user, the officer, or a
        sweeper. Stamp every open row about it, whoever it belongs to (D6).
        Best-effort by design: called from state-change hooks that must not
        fail because the feed did."""
        if not self._available or not self._db:
            return []
        try:
            rows = await self._db.resolve_notifications_by_source(
                source_kind=source_kind,
                source_id=str(source_id),
                resolved_by=resolved_by,
            )
        except Exception as e:
            logger.warning(
                "resolve_source(%s, %s) failed: %s", source_kind, source_id, e
            )
            return []
        for row in rows:
            wire = serialize_notification(row)
            self._broadcast_update(
                str(row["recipient_id"]),
                {
                    "id": wire["id"],
                    "resolved_at": wire["resolved_at"],
                    "resolved_by": wire["resolved_by"],
                },
            )
        return [str(row["id"]) for row in rows]

    async def get_feed_page(
        self,
        *,
        recipient_kind: str,
        recipient_id: str,
        before: str | None = None,
        limit: int = 50,
        categories: list[str] | None = None,
        status: str = "all",
    ) -> dict[str, Any]:
        rows, next_before = await self._db.list_notifications_page(
            recipient_kind=recipient_kind,
            recipient_id=recipient_id,
            before=before,
            limit=limit,
            categories=categories,
            status=status,
        )
        counts = await self._db.get_notification_counts(
            recipient_kind=recipient_kind, recipient_id=recipient_id
        )
        return {
            "items": [serialize_notification(r) for r in rows],
            "next_before": next_before,
            "counts": counts,
        }

    async def get_counts(
        self, *, recipient_kind: str, recipient_id: str
    ) -> dict[str, Any]:
        return await self._db.get_notification_counts(
            recipient_kind=recipient_kind, recipient_id=recipient_id
        )

    # --- stubbable seams (tests build NotificationService.__new__ and replace these) ---

    async def _persist_notification(
        self, row: dict[str, Any], *, steps: list[dict[str, Any]] | None = None
    ) -> tuple[str, bool]:
        inserted = await self._db.insert_notification_once(
            notification_id=row["id"],
            recipient_kind=row["recipient_kind"],
            recipient_id=row["recipient_id"],
            category=row["category"],
            severity=row["severity"],
            subject=row["subject"],
            body=row["body"],
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            dedup_key=row["dedup_key"],
            actions=row["actions"],
            payload=row["payload"],
            steps=steps or None,
        )
        return row["id"], bool(inserted)

    async def _defer_steps(
        self, notification_id: str, steps: list[dict[str, Any]]
    ) -> int:
        """Quiet hours: park the immediate steps until the window ends. The
        insert is idempotent per step index, so a replay cannot stack a
        second promise. Failure here is logged, not raised — the feed row
        already exists and the in-app signal is intact."""
        try:
            return int(await self._db.insert_notification_steps(notification_id, steps))
        except Exception as e:
            logger.warning(
                "could not defer notification %s steps: %s", notification_id[:8], e
            )
            return 0

    async def _claim_delivery(
        self,
        notification_id: str,
        channel: str,
        *,
        address: str | None,
        step_index: int | None = None,
        batch_id: str | None = None,
    ) -> str | None:
        return await self._db.claim_notification_delivery(
            notification_id=notification_id,
            channel=channel,
            recipient_address=address,
            step_index=step_index,
            batch_id=batch_id,
        )

    async def _settle_delivery(
        self,
        delivery_id: str,
        *,
        state: str,
        provider_msg_id: str | None = None,
        error: str | None = None,
    ) -> None:
        await self._db.settle_notification_delivery(
            delivery_id, state=state, provider_msg_id=provider_msg_id, error=error
        )

    async def _record_suppressed(
        self, notification_id: str, channel: str, reason: str
    ) -> None:
        """A channel that was deliberately not attempted still gets a row, so
        "why did no mail go out" is answerable. A suppressed row does not hold
        the claim slot."""
        try:
            claim = await self._claim_delivery(notification_id, channel, address=None)
            if claim:
                await self._settle_delivery(claim, state="suppressed", error=reason)
        except Exception as e:
            logger.debug("suppressed-delivery row failed (%s): %s", channel, e)

    async def _get_user(self, user_id: str) -> dict[str, Any] | None:
        if not self._db:
            return None
        try:
            return await self._db.get_user(str(user_id))
        except Exception:
            return None

    def _broadcast_notification(self, row: dict[str, Any]) -> None:
        if not self._notification_feed or row.get("recipient_kind") != "user":
            return
        try:
            self._notification_feed.broadcast(
                user_id=str(row["recipient_id"]),
                event_type="notification",
                data={"notification": serialize_notification(row)},
            )
        except Exception as e:
            logger.debug("notification SSE broadcast failed: %s", e)

    def _broadcast_update(self, recipient_id: str, patch: dict[str, Any]) -> None:
        if not self._notification_feed:
            return
        try:
            self._notification_feed.broadcast(
                user_id=str(recipient_id),
                event_type="notification.updated",
                data=patch,
            )
        except Exception as e:
            logger.debug("notification.updated SSE broadcast failed: %s", e)

    def _channel_deliverable(self, channel: str) -> bool:
        """Is there any transport behind this channel at all? Channels with
        nothing behind them get no delivery rows — a suppressed row answers
        "why did this not go out", and "the operator never configured Slack"
        is not a per-notification question."""
        if channel == "email":
            return self._email_service is not None and bool(
                getattr(self._email_service, "is_configured", True)
            )
        if channel in WEBHOOK_CHANNELS:
            transport = self._transports.get(channel)
            return bool(transport and transport.is_configured)
        return False  # 'push' has no v1 transport (non-goal)

    async def _deliver_immediate(
        self, row: dict[str, Any], spec: Any, *, inserted: bool
    ) -> dict[str, Any]:
        """Run the zero-delay channel steps of the row's severity class.

        Always runs — including on replay — because a crash between a send
        and the completion journal's mark replays the callback; the claim
        ledger decides per channel whether anything is actually sent. Rows
        that were deliberately not attempted (preference off, no address)
        are recorded as ``suppressed`` only by the inserting call so a replay
        does not pile up duplicates. Quiet hours never drop a step: they
        defer it to the window's end as a ``not_resolved``-gated step row.
        """
        results: dict[str, Any] = {"in_app": True}
        steps = steps_for(spec, row["severity"])
        immediate = [step for step in steps if step.immediate]
        if not immediate or row["recipient_kind"] != "user":
            return results

        nid = row["id"]
        recipient_id = row["recipient_id"]
        deliverable = [s for s in immediate if self._channel_deliverable(s.channel)]
        if not deliverable:
            return results
        channels = await self._get_user_channels(recipient_id)
        settings = await self._get_user_settings(recipient_id)
        categories = ((settings or {}).get("communication") or {}).get("categories")
        wanted = [
            s
            for s in deliverable
            if channel_enabled(channels, categories, row["category"], s.channel)
        ]
        if inserted:
            for step in deliverable:
                if step not in wanted:
                    await self._record_suppressed(nid, step.channel, "preference")
        if not wanted:
            return results

        if not bypasses_quiet_hours(spec, row["severity"]) and self._is_in_quiet_hours(
            settings
        ):
            resume_at = self.next_quiet_hours_end(settings) or (
                datetime.now(timezone.utc) + timedelta(hours=1)
            )
            await self._defer_steps(
                nid,
                [
                    {
                        "step_index": DEFERRED_STEP_INDEX_BASE + steps.index(step),
                        "step_kind": step.channel,
                        "due_at": resume_at,
                        "conditions": ["not_resolved"],
                        "batch_key": None,
                    }
                    for step in wanted
                ],
            )
            results["deferred_until"] = resume_at.isoformat()
            return results

        user = await self._get_user(recipient_id)
        for step in wanted:
            results.update(
                await self._send_one(
                    row,
                    step.channel,
                    user=user,
                    subject=row["subject"],
                    body=row["body"],
                    cockpit_path=f"/inbox?n={nid}",
                    inserted=inserted,
                )
            )
        return results

    async def _send_one(
        self,
        row: dict[str, Any],
        channel: str,
        *,
        user: dict[str, Any] | None,
        subject: str,
        body: str,
        cockpit_path: str,
        inserted: bool,
    ) -> dict[str, Any]:
        """Claim, send, settle — one channel of one row."""
        nid = row["id"]
        address = None
        if channel == "email":
            address = (user or {}).get("email")
            if not address:
                if inserted:
                    await self._record_suppressed(nid, "email", "no_email")
                return {}
        claim = await self._claim_delivery(nid, channel, address=address)
        if claim is None:
            return {channel: "already_delivered"}
        ok, msg_id, error = await self._send_channel(
            channel,
            user=user,
            subject=subject,
            body=body,
            cockpit_path=cockpit_path,
            payload=row.get("payload") or {},
            source_id=row.get("source_id"),
        )
        await self._settle_delivery(
            claim,
            state="sent" if ok else "failed",
            provider_msg_id=msg_id,
            error=error,
        )
        out: dict[str, Any] = {channel: bool(ok)}
        if msg_id:
            out["email_message_id"] = msg_id
        return out

    async def _send_channel(
        self,
        channel: str,
        *,
        user: dict[str, Any] | None,
        subject: str,
        body: str,
        cockpit_path: str,
        payload: dict[str, Any],
        source_id: str | None,
    ) -> tuple[bool, str | None, str | None]:
        """The provider call for one channel: ``(ok, provider_msg_id, error)``.
        Never raises — a channel failure is a settled ``failed`` delivery,
        not a lost notification."""
        if channel == "email":
            try:
                ok, msg_id = await self._email_service.send_notification_email(
                    to=(user or {}).get("email") or "",
                    to_name=(user or {}).get("display_name") or "User",
                    subject=subject,
                    body_md=body,
                    cockpit_path=cockpit_path,
                )
                return bool(ok), msg_id, None if ok else "send returned False"
            except Exception as e:
                logger.warning("notification email failed: %s", e)
                return False, None, str(e)
        transport = self._transports.get(channel)
        if transport is None:
            return False, None, f"no transport for {channel}"
        try:
            ok = bool(
                await transport.send(
                    NotificationPayload(
                        subject=subject,
                        body_text=body,
                        job_id=str(payload.get("job_id") or source_id or ""),
                        job_description=str(payload.get("job_description") or ""),
                        config_name=str(payload.get("config_name") or ""),
                        thread_id=payload.get("thread_id"),
                        cockpit_url=f"{self._cockpit_url}{cockpit_path}",
                    )
                )
            )
            return ok, None, None if ok else "send returned False"
        except Exception as e:
            logger.warning("notification %s transport failed: %s", channel, e)
            return False, None, str(e)

    # --- deferred steps (slice 2) ----------------------------------------------

    async def _plan_deferred_steps(
        self, row: dict[str, Any], steps: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        """Turn the class's non-immediate steps into rows for
        ``notification_steps``: the delay becomes ``due_at`` (D5 — a delay is
        not its own row), batched steps round up to their window bucket, and
        the conditions ride along to be evaluated when due, not now."""
        settings = await self._get_user_settings(row["recipient_id"])
        now = datetime.now(timezone.utc)
        officer_minutes: int | None = None
        planned: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if step.immediate:
                continue
            if step.delay == DELAY_OFFICER_RESPONSE:
                if officer_minutes is None:
                    officer_minutes = await self._resolve_delay_minutes(row, settings)
                minutes = officer_minutes
            else:
                minutes = int(step.delay)
            due = now + timedelta(minutes=minutes)
            if step.batch_window_minutes:
                due = bucket_due_at(due, step.batch_window_minutes)
            planned.append(
                {
                    "step_index": index,
                    "step_kind": step.channel,
                    "due_at": due,
                    "conditions": list(step.conditions),
                    "batch_key": batch_key_for(step, row),
                }
            )
        return planned

    async def _resolve_delay_minutes(
        self, row: dict[str, Any], settings: dict[str, Any] | None
    ) -> int:
        """D6: wait as long as the project's officer is allowed to take — the
        mail that survives that window says the automated tier did not
        settle it. Without a live, un-held officer the recipient's own
        ``communication.escalation_minutes`` applies, default 5."""
        if row.get("source_kind") == "job" and self._db:
            try:
                minutes = await self._officer_response_minutes(str(row["source_id"]))
            except Exception as e:
                logger.warning(
                    "officer window lookup failed for job %s: %s",
                    str(row.get("source_id"))[:8],
                    e,
                )
                minutes = None
            if minutes is not None:
                return minutes
        configured = ((settings or {}).get("communication") or {}).get(
            "escalation_minutes"
        )
        lo, hi = ESCALATION_MINUTES_BOUNDS
        if (
            isinstance(configured, int)
            and not isinstance(configured, bool)
            and lo <= configured <= hi
        ):
            return configured
        return NO_OFFICER_DELAY_MINUTES

    async def _officer_response_minutes(self, job_id: str) -> int | None:
        """The commissioned, un-held officer's response window for the job's
        project, or ``None`` when nobody but the human can settle it. Deliberately
        independent of the worker-message policy: an officer reviews jobs even
        for a project whose messages go user-direct."""
        from services import message_routing as routing_svc

        job = await self._db.get_job(job_id)
        project_id = (job or {}).get("project_id")
        if not project_id:
            return None
        post = await self._db.get_project_officer(str(project_id))
        if not post:
            return None
        officer = await self._db.get_officer_thread_for_project(str(project_id))
        if not officer or routing_svc.officer_hold(officer) is not None:
            return None
        policy = post.get("communication_policy") or {}
        if isinstance(policy, str):
            try:
                policy = json.loads(policy)
            except ValueError:
                policy = {}
        lo, hi = routing_svc.OFFICER_RESPONSE_MINUTES_BOUNDS
        try:
            value = int(
                policy.get(
                    "officer_response_minutes",
                    routing_svc.DEFAULT_OFFICER_RESPONSE_MINUTES,
                )
            )
        except (TypeError, ValueError):
            value = routing_svc.DEFAULT_OFFICER_RESPONSE_MINUTES
        return min(max(value, lo), hi)

    async def _source_resolved(self, source_kind: str | None, source_id: Any) -> bool:
        """Ask the registered probe whether the source is settled. Unknown
        kinds and probe failures read as *not* resolved: the failure mode
        stays "occasionally mails about something just settled", never
        "silently never mails"."""
        probe = source_probe(source_kind)
        if probe is None or not self._db or source_id is None:
            return False
        try:
            return bool(await probe(self._db, str(source_id)))
        except Exception as e:
            logger.warning("source probe %s/%s failed: %s", source_kind, source_id, e)
            return False

    async def describe_steps(self, notification_id: str) -> list[dict[str, Any]]:
        if not self._db:
            return []
        rows = await self._db.list_notification_steps(notification_id)
        return [serialize_step(r) for r in rows]

    @staticmethod
    def render_step_message(
        members: list[dict[str, Any]], *, cockpit_url: str
    ) -> tuple[str, str, str]:
        """``(subject, body_md, cockpit_path)`` for a due group. One member is
        the row itself; several become one digest ("3 review queue items
        waiting") that links each row's own deep link (D8 batching)."""
        if len(members) == 1:
            row = members[0]
            return (
                str(row.get("subject") or ""),
                str(row.get("body") or ""),
                f"/inbox?n={row['notification_id']}",
            )
        category = str(members[0].get("category") or "notification")
        label = category.replace("_", " ")
        subject = f"{len(members)} {label} items waiting for you"
        lines = [f"You have **{len(members)}** {label} items nobody has settled yet:\n"]
        for row in members:
            excerpt = str(row.get("body") or "").strip().splitlines()
            first = excerpt[0].strip() if excerpt else ""
            if len(first) > 160:
                first = first[:157] + "…"
            lines.append(
                f"- **{row.get('subject') or '(no subject)'}** — "
                f"[open]({cockpit_url}/inbox?n={row['notification_id']})"
            )
            if first:
                lines.append(f"  {first}")
        return subject, "\n".join(lines), "/inbox"

    async def send_step_group(
        self, members: list[dict[str, Any]], *, channel: str
    ) -> dict[str, Any]:
        """The send half of a due step group (called by the sweeper).

        Claims one delivery per member BEFORE sending (D10) so a member whose
        channel already went out — an earlier attempt that crashed after the
        provider call — is left out rather than mailed twice; renders one
        message for the survivors; settles every claim the same way.
        """
        batch_id = str(uuid.uuid4())
        recipient_id = str(members[0]["recipient_id"])
        user = await self._get_user(recipient_id)
        address = (user or {}).get("email") if channel == "email" else None
        outcome: dict[str, Any] = {
            "batch_id": batch_id,
            "attempted": [],
            "already": [],
            "unaddressed": [],
            "ok": True,
            "error": None,
        }
        if channel == "email" and not address:
            outcome["unaddressed"] = [m["id"] for m in members]
            return outcome

        attempted: list[dict[str, Any]] = []
        claims: list[str] = []
        for member in members:
            claim = await self._claim_delivery(
                str(member["notification_id"]),
                channel,
                address=address,
                step_index=int(member["step_index"]),
                batch_id=batch_id,
            )
            if claim is None:
                outcome["already"].append(member["id"])
                continue
            attempted.append(member)
            claims.append(claim)
        if not attempted:
            return outcome

        subject, body, cockpit_path = self.render_step_message(
            attempted, cockpit_url=self._cockpit_url
        )
        first = attempted[0]
        ok, msg_id, error = await self._send_channel(
            channel,
            user=user,
            subject=subject,
            body=body,
            cockpit_path=cockpit_path,
            payload=first.get("payload") or {},
            source_id=first.get("source_id"),
        )
        for claim in claims:
            await self._settle_delivery(
                claim,
                state="sent" if ok else "failed",
                provider_msg_id=msg_id,
                error=error,
            )
        outcome["attempted"] = [m["id"] for m in attempted]
        outcome["ok"] = bool(ok)
        outcome["error"] = error
        return outcome

    # =========================================================================
    # Legacy fan-out — retired in slice 3; every caller is in the manifest
    # =========================================================================

    async def dispatch(
        self,
        user_id: str,
        job_id: str,
        subject: str,
        message_md: str,
        job_description: str,
        config_name: str,
        thread_id: str | None = None,
        phase_number: int | None = None,
        recipient_email: str | None = None,
        recipient_name: str | None = None,
        sudo_request_id: str | None = None,
        bypass_quiet_hours: bool = False,
    ) -> dict[str, Any]:
        """Dispatch notification to all enabled channels.

        Respects user channel preferences and quiet hours —
        ``bypass_quiet_hours=True`` skips only the quiet-hours queueing
        (officer *pages*: 'action needed from you NOW' is the one urgency the
        notify contract lets through at night, centurion.md §6; same doctrine
        as the bespoke no-quiet-hours system alerts in this module). Channel
        preferences still apply.

        Returns:
            Dict with channel results, e.g.::

                {
                    "email": True,
                    "email_message_id": "<uuid@domain>",
                    "ntfy": True,
                    "queued": False,
                }
        """
        if not self._available:
            return {"error": "NotificationService not initialized"}

        results: dict[str, Any] = {}

        # Load user channel preferences
        user_channels = await self._get_user_channels(user_id)
        user_settings = await self._get_user_settings(user_id)

        # Check quiet hours
        if not bypass_quiet_hours and self._is_in_quiet_hours(user_settings):
            # Queue for digest delivery when quiet hours end
            await self._queue_notification(
                user_id=user_id,
                job_id=job_id,
                thread_id=thread_id,
                subject=subject,
                message=message_md,
                channels=user_channels,
            )
            results["queued"] = True
            logger.info(
                "Notification queued (quiet hours): job=%s, subject=%s",
                job_id[:8],
                subject,
            )

            # Still broadcast to cockpit SSE (in-app is not affected by quiet hours)
            await self._broadcast_sse(user_id, job_id, subject, thread_id)

            return results

        results["queued"] = False

        # Build cockpit deep link (into action center inbox)
        if thread_id:
            cockpit_link = f"{self._cockpit_url}/inbox?job={job_id}&thread={thread_id}"
        elif sudo_request_id:
            cockpit_link = f"{self._cockpit_url}/inbox?sudo={sudo_request_id}"
        else:
            cockpit_link = f"{self._cockpit_url}/inbox?job={job_id}&review=1"

        # Dispatch to email
        if user_channels.get("email", True) and self._email_service:
            try:
                email_sent, email_msg_id = await self._email_service.send_agent_message(
                    to=recipient_email or "",
                    to_name=recipient_name or "User",
                    subject=subject,
                    message_md=message_md,
                    job_id=job_id,
                    job_description=job_description,
                    config_name=config_name,
                    phase_number=phase_number,
                    thread_id=thread_id,
                    sudo_request_id=sudo_request_id,
                )
                results["email"] = email_sent
                if email_msg_id:
                    results["email_message_id"] = email_msg_id
            except Exception as e:
                logger.warning("Email dispatch failed: %s", e)
                results["email"] = False

        # Build webhook payload
        payload = NotificationPayload(
            subject=subject,
            body_text=message_md,
            job_id=job_id,
            job_description=job_description,
            config_name=config_name,
            thread_id=thread_id,
            cockpit_url=cockpit_link,
        )

        # Dispatch to webhook transports
        for name, transport in self._transports.items():
            if not transport.is_configured:
                continue
            if not user_channels.get(name, True):
                continue
            try:
                results[name] = await transport.send(payload)
            except Exception as e:
                logger.warning("Transport %s failed: %s", name, e)
                results[name] = False

        # Broadcast to cockpit notification feed (SSE)
        await self._broadcast_sse(user_id, job_id, subject, thread_id)

        return results

    async def notify_automation_auto_disabled(
        self,
        user_id: str,
        automation_id: str,
        automation_name: str,
        reason: str,
    ) -> dict[str, Any]:
        """Notify the owner that their automation was auto-disabled.

        Called from ``cron_dispatcher`` after the dispatcher trips a safety
        guard (max_fires_per_day or invalid cron expression). Sends an SSE
        event for real-time cockpit updates plus an email if the user has
        email notifications enabled. Does NOT respect quiet hours — system
        safety events should reach the owner promptly so they can fix and
        re-enable.

        The disable itself is already committed when this is called; this
        method is best-effort and its failures are non-fatal.
        """
        if not self._available:
            return {"error": "NotificationService not initialized"}

        results: dict[str, Any] = {}
        user_channels = await self._get_user_channels(user_id)

        display_name = automation_name or "(unnamed)"
        subject = f"Automation '{display_name}' was auto-paused"
        body_md = (
            f"Your automation **{display_name}** was paused automatically "
            f"because: {reason}\n\n"
            f"Edit the automation in the cockpit to fix the underlying "
            f"issue and re-enable it."
        )

        # SSE broadcast — real-time cockpit notification.
        if self._notification_feed:
            try:
                self._notification_feed.broadcast(
                    user_id=user_id,
                    event_type="automation_auto_disabled",
                    data={
                        "automation_id": automation_id,
                        "automation_name": automation_name,
                        "reason": reason,
                        "cockpit_url": f"{self._cockpit_url}/automations",
                    },
                )
                results["sse"] = True
            except Exception as e:
                logger.warning("SSE broadcast failed for auto-disable: %s", e)
                results["sse"] = False

        # Email — if the user has the email channel enabled and we have
        # SMTP configured. Failures are logged; the SSE event is the
        # primary user-visible signal in the cockpit.
        if user_channels.get("email", True) and self._email_service and self._db:
            try:
                user = await self._db.get_user(user_id)
            except Exception:
                user = None
            if user and user.get("email"):
                try:
                    results[
                        "email"
                    ] = await self._email_service.send_system_notification(
                        to=user["email"],
                        to_name=user.get("display_name") or "User",
                        subject=subject,
                        body_md=body_md,
                        cockpit_path="/automations",
                    )
                except Exception as e:
                    logger.warning("Auto-disable email failed: %s", e)
                    results["email"] = False

        return results

    async def notify_review_returned_to_manual(
        self,
        user_id: str,
        job_id: str,
        config_name: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Notify the owner that automated review ended and a human must decide.

        Two callers, both meaning "nothing was approved, it is yours now":

        - ``stale_verification_sweeper``, after ``unstick_reviewing_parents``
          flips a parent 'reviewing' → 'pending_review' because its critic
          pipeline died. No ``reason`` — the cause is the pipeline itself.
        - ``_escalate_target`` (orchestrator/main.py), the verification gate's
          primary escalation: round cap reached, no progress since the last
          round, or a critic that finished with no verdict. Passes ``reason``,
          which is the same text written to the job's ``error_message``.

        Mirrors ``notify_automation_auto_disabled``: SSE for real-time cockpit
        plus an email if the user has that channel enabled. Does NOT respect
        quiet hours — an unattended job needing manual review should reach the
        owner promptly. Best-effort; failures are logged, never raised.
        """
        if not self._available:
            return {"error": "NotificationService not initialized"}

        results: dict[str, Any] = {}
        user_channels = await self._get_user_channels(user_id)

        label = config_name or "job"
        review_path = f"/inbox?job={job_id}&review=1"
        subject = "Job needs manual review — automated verification failed"
        if reason:
            body_md = (
                f"Automated verification for your **{label}** job stopped without "
                f"approving it, so it needs **manual review**.\n\n"
                f"> {reason}\n\n"
                f"Open it in the cockpit to approve it or send it back with "
                f"feedback."
            )
        else:
            body_md = (
                f"Automated verification for your **{label}** job did not complete "
                f"(the review pipeline died), so it has been returned to **manual "
                f"review**. Open it in the cockpit to approve it or send it back "
                f"with feedback."
            )

        # SSE broadcast — real-time cockpit notification.
        if self._notification_feed:
            try:
                self._notification_feed.broadcast(
                    user_id=user_id,
                    event_type="review_returned_to_manual",
                    data={
                        "job_id": job_id,
                        "config_name": config_name,
                        "reason": reason or "",
                        "cockpit_url": f"{self._cockpit_url}{review_path}",
                    },
                )
                results["sse"] = True
            except Exception as e:
                logger.warning(
                    "SSE broadcast failed for review-returned (job=%s): %s",
                    job_id,
                    e,
                )
                results["sse"] = False

        # Email — only if the user has the email channel enabled and SMTP is
        # configured. The SSE event is the primary in-cockpit signal.
        if user_channels.get("email", True) and self._email_service and self._db:
            try:
                user = await self._db.get_user(user_id)
            except Exception:
                user = None
            if user and user.get("email"):
                try:
                    results[
                        "email"
                    ] = await self._email_service.send_system_notification(
                        to=user["email"],
                        to_name=user.get("display_name") or "User",
                        subject=subject,
                        body_md=body_md,
                        cockpit_path=review_path,
                    )
                except Exception as e:
                    logger.warning(
                        "Email send failed for review-returned (job=%s): %s",
                        job_id,
                        e,
                    )
                    results["email"] = False
        return results

    async def notify_admins_user_registered(
        self,
        new_user_id: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Notify all admins that a new user registered and awaits approval.

        Fan-out to every admin (app-side admission — an admin decides who gets
        in). SSE is sent unconditionally (the in-app feed isn't subject to quiet
        hours); email is sent per each admin's channel preference and skipped
        during that admin's quiet hours, since a registration is not
        safety-critical. Best-effort; failures are logged, never raised.

        Mirrors :py:meth:`notify_automation_auto_disabled` (direct SSE +
        ``send_system_notification``), not the job-shaped :py:meth:`dispatch`.
        """
        if not self._available or not self._db:
            return {"error": "NotificationService not initialized"}

        try:
            admin_ids = await self._db.list_admin_user_ids()
        except Exception as e:
            logger.warning("Could not list admins for registration notify: %s", e)
            return {"error": "admin lookup failed"}

        label = display_name or email or "A new user"
        suffix = f" ({email})" if email else ""
        subject = f"New user pending approval: {label}"
        body_md = (
            f"**{label}**{suffix} just registered and is awaiting approval.\n\n"
            "Review and approve pending users on the Users page in the cockpit."
        )

        results: dict[str, Any] = {"notified": 0}
        for admin_id in admin_ids:
            if admin_id == str(new_user_id):
                continue  # a self-registered admin isn't pending anyway

            # SSE — always (the in-app feed isn't quiet-houred).
            if self._notification_feed:
                try:
                    self._notification_feed.broadcast(
                        user_id=admin_id,
                        event_type="user_registered",
                        data={
                            "user_id": str(new_user_id),
                            "display_name": display_name,
                            "email": email,
                            "cockpit_url": f"{self._cockpit_url}/admin/users",
                        },
                    )
                    results["notified"] += 1
                except Exception as e:
                    logger.warning("SSE broadcast failed for user_registered: %s", e)

            # Email — per preference, skipped during the admin's quiet hours.
            if not self._email_service:
                continue
            try:
                channels = await self._get_user_channels(admin_id)
                settings = await self._get_user_settings(admin_id)
                if not channels.get("email", True) or self._is_in_quiet_hours(settings):
                    continue
                admin = await self._db.get_user(admin_id)
                if admin and admin.get("email"):
                    await self._email_service.send_system_notification(
                        to=admin["email"],
                        to_name=admin.get("display_name") or "Admin",
                        subject=subject,
                        body_md=body_md,
                        cockpit_path="/admin/users",
                    )
            except Exception as e:
                logger.warning("Registration email to admin %s failed: %s", admin_id, e)

        return results

    async def dispatch_digest(
        self,
        user_id: str,
        notifications: list[dict],
    ) -> dict[str, bool]:
        """Send a batched digest of queued notifications.

        Called by the quiet hours digest loop when quiet hours end.
        """
        if not notifications:
            return {}

        # Build digest body
        lines = [
            f"You have {len(notifications)} notification(s) from while you were away:\n"
        ]
        for n in notifications:
            lines.append(f"**{n['subject']}**")
            lines.append(f"{n['message'][:200]}")
            lines.append("")

        digest_body = "\n".join(lines)
        digest_subject = f"{len(notifications)} queued notification(s)"

        # Get user's email for the digest
        user = None
        if self._db:
            try:
                user = await self._db.get_user(user_id)
            except Exception:
                pass

        user_channels = await self._get_user_channels(user_id)
        cockpit_link = self._cockpit_url

        results: dict[str, bool] = {}

        # Send email digest
        if user_channels.get("email", True) and self._email_service and user:
            try:
                email_sent, _ = await self._email_service.send_agent_message(
                    to=user.get("email", ""),
                    to_name=user.get("display_name", "User"),
                    subject=digest_subject,
                    message_md=digest_body,
                    job_id=notifications[0].get("job_id", ""),
                    job_description="Notification Digest",
                    config_name="system",
                )
                results["email"] = email_sent
            except Exception as e:
                logger.warning("Digest email failed: %s", e)
                results["email"] = False

        # Send webhook digest
        payload = NotificationPayload(
            subject=digest_subject,
            body_text=digest_body,
            job_id=notifications[0].get("job_id", ""),
            cockpit_url=cockpit_link,
        )

        for name, transport in self._transports.items():
            if not transport.is_configured:
                continue
            if not user_channels.get(name, True):
                continue
            try:
                results[name] = await transport.send(payload)
            except Exception as e:
                logger.warning("Digest transport %s failed: %s", name, e)
                results[name] = False

        return results

    async def _get_user_channels(self, user_id: str) -> dict[str, bool]:
        """Load user's channel preferences from settings JSONB."""
        defaults = {"email": True, "cockpit": True}
        if not self._db:
            return defaults
        try:
            settings = await self._db.get_user_settings(user_id)
            channels = (settings or {}).get("communication", {}).get("channels", {})
            return {**defaults, **channels}
        except Exception:
            return defaults

    async def _get_user_settings(self, user_id: str) -> dict:
        """Load full user settings."""
        if not self._db:
            return {}
        try:
            return await self._db.get_user_settings(user_id) or {}
        except Exception:
            return {}

    @staticmethod
    def _is_in_quiet_hours(user_settings: dict) -> bool:
        """Check if current time falls within user's quiet hours."""
        return quiet_hours_window(user_settings)[0]

    @staticmethod
    def next_quiet_hours_end(user_settings: dict) -> datetime | None:
        """When the current quiet-hours window ends (UTC), or ``None`` when
        the recipient is not in one."""
        return quiet_hours_window(user_settings)[1]

    async def _queue_notification(
        self,
        user_id: str,
        job_id: str,
        thread_id: str | None,
        subject: str,
        message: str,
        channels: dict,
    ) -> None:
        """Queue a notification for later digest delivery."""
        if not self._db:
            return
        try:
            await self._db.queue_notification(
                user_id=user_id,
                job_id=job_id,
                thread_id=thread_id,
                subject=subject,
                message=message,
                channels=channels,
            )
        except Exception as e:
            logger.warning("Failed to queue notification: %s", e)

    async def _broadcast_sse(
        self,
        user_id: str,
        job_id: str,
        subject: str,
        thread_id: str | None,
    ) -> None:
        """Broadcast to cockpit notification feed (SSE)."""
        if not self._notification_feed:
            return
        try:
            self._notification_feed.broadcast(
                user_id=user_id,
                event_type="new_message",
                data={
                    "job_id": job_id,
                    "subject": subject,
                    "thread_id": thread_id,
                },
            )
        except Exception as e:
            logger.debug("SSE broadcast failed: %s", e)


# Module-level singleton
notification_service = NotificationService()
