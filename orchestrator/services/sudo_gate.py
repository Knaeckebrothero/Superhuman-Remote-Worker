"""Sudo Approval Gate — Orchestrator-side service.

Handles:
  1. NATS subscription for sudo.request.> — receives approval requests from daemons
  2. Auto-approval rule evaluation (fnmatch, shell metacharacter check)
  3. Expiration sweeper — denies timed-out requests
  4. SSE event broadcasting — pushes new requests to cockpit clients
  5. Database operations for sudo_approval_requests and sudo_auto_rules

This module is optional — if NATS is unavailable, the gate simply doesn't exist
and agents operate with unrestricted sudo (the pre-gate default).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Shell metacharacters that prevent auto-approval.
# Commands containing these are always forwarded to human review.
_SHELL_META_RE = re.compile(r"[|;&`$><]|\$\(|\|\||&&")


class SudoGateService:
    """Manages sudo approval requests and auto-approval rules."""

    def __init__(self):
        self._db: Optional[Any] = None
        self._nc: Optional[Any] = None  # NATS connection (from NatsBridge)
        self._sse_queues: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._pending_msgs: dict[str, Any] = {}  # request_id → NATS msg for respond()

    def connect(self, db: Any, nc: Optional[Any] = None) -> None:
        """Bind to database and optional NATS connection."""
        self._db = db
        self._nc = nc

    # =========================================================================
    # NATS subscription handler
    # =========================================================================

    async def on_sudo_request(self, msg) -> None:
        """Handle sudo.request.> — a daemon is requesting approval.

        The handler:
          1. Parses the request payload
          2. Inserts a row in sudo_approval_requests with msg.reply stored
          3. Evaluates auto-approval rules
          4. If auto-match: publishes response immediately
          5. If no match: pushes to SSE, waits for human decision

        This handler returns immediately — it does NOT block. The NATS reply
        subject is persisted in the DB for async response later.
        """
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error("Malformed sudo request payload: %s", e)
            return

        job_id = data.get("job_id", "")
        vm_id = data.get("vm_id", "")
        command = data.get("command", "")
        argv = data.get("argv", [])
        user = data.get("user", "unknown")
        runas_user = data.get("runas_user", "root")
        cwd = data.get("cwd", "")

        logger.info(
            "Sudo request: job=%s vm=%s user=%s cmd=%s",
            job_id, vm_id, command, " ".join(argv),
        )

        # Store the request with the NATS reply subject.
        reply_subject = msg.reply if hasattr(msg, "reply") else None
        request_id = await self._insert_request(
            job_id=job_id,
            vm_name=vm_id,
            command=command,
            arguments=argv,
            cwd=cwd,
            requesting_user=user,
            target_user=runas_user,
            nats_reply_subject=reply_subject,
            metadata=data,
        )

        if not request_id:
            # DB insert failed — deny immediately.
            await self._nats_reply(reply_subject, False, "internal error")
            return

        # Evaluate auto-approval rules.
        cmd_string = " ".join(argv) if argv else command
        auto_result = await self._evaluate_auto_rules(cmd_string)

        if auto_result == "approve":
            logger.info("Auto-approved: %s (request %s)", cmd_string, request_id)
            await self._finalize_request(
                request_id, "auto_approved", "auto-approval rule", "system",
                reply_subject,
            )
            # Also respond directly via msg for immediate delivery
            try:
                await msg.respond(json.dumps({"approved": True, "reason": "auto-approval rule"}).encode())
            except Exception:
                pass
            return

        if auto_result == "deny":
            logger.info("Auto-denied: %s (request %s)", cmd_string, request_id)
            await self._finalize_request(
                request_id, "auto_denied", "auto-denial rule", "system",
                reply_subject,
            )
            try:
                await msg.respond(json.dumps({"approved": False, "reason": "auto-denial rule"}).encode())
            except Exception:
                pass
            return

        # No auto-match — store msg for later respond() and push to SSE.
        if request_id:
            self._pending_msgs[request_id] = msg

        event = {
            "id": str(request_id),
            "job_id": job_id,
            "vm_name": vm_id,
            "command": command,
            "arguments": argv,
            "requesting_user": user,
            "target_user": runas_user,
            "working_directory": cwd,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._broadcast_sse("new_request", event)

    # =========================================================================
    # Approval / Denial (from REST endpoints)
    # =========================================================================

    async def approve_request(
        self, request_id: str, reason: str = "", decided_by: str = "operator"
    ) -> Optional[dict]:
        """Approve a pending sudo request."""
        row = await self._get_request(request_id)
        if not row:
            return None
        if row["status"] != "pending":
            return {"error": f"Request status is '{row['status']}', not 'pending'"}

        reply_subject = row.get("nats_reply_subject")
        await self._finalize_request(
            request_id, "approved", reason, decided_by, reply_subject,
        )

        # Respond via the stored NATS msg object (most reliable for cross-leaf)
        msg = self._pending_msgs.pop(request_id, None)
        if msg:
            try:
                await msg.respond(json.dumps({"approved": True, "reason": reason}).encode())
                logger.info("Responded via msg.respond() for request %s", request_id)
            except Exception as e:
                logger.warning("msg.respond() failed for %s: %s", request_id, e)

        await self._broadcast_sse("request_decided", {
            "id": str(request_id),
            "status": "approved",
            "decided_by": decided_by,
        })

        return {"id": str(request_id), "status": "approved"}

    async def deny_request(
        self, request_id: str, reason: str = "", decided_by: str = "operator"
    ) -> Optional[dict]:
        """Deny a pending sudo request."""
        row = await self._get_request(request_id)
        if not row:
            return None
        if row["status"] != "pending":
            return {"error": f"Request status is '{row['status']}', not 'pending'"}

        reply_subject = row.get("nats_reply_subject")
        await self._finalize_request(
            request_id, "denied", reason, decided_by, reply_subject,
        )

        # Respond via the stored NATS msg object
        msg = self._pending_msgs.pop(request_id, None)
        if msg:
            try:
                await msg.respond(json.dumps({"approved": False, "reason": reason}).encode())
                logger.info("Responded via msg.respond() for request %s", request_id)
            except Exception as e:
                logger.warning("msg.respond() failed for %s: %s", request_id, e)

        await self._broadcast_sse("request_decided", {
            "id": str(request_id),
            "status": "denied",
            "decided_by": decided_by,
            "reason": reason,
        })

        return {"id": str(request_id), "status": "denied"}

    # =========================================================================
    # Expiration sweeper
    # =========================================================================

    async def sweep_expired(self) -> int:
        """Deny all pending requests past their TTL.

        Returns the number of expired requests processed.
        """
        if not self._db:
            return 0

        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    UPDATE sudo_approval_requests
                    SET status = 'expired',
                        decided_at = NOW(),
                        decided_by = 'system',
                        decision_reason = 'TTL expired'
                    WHERE status = 'pending' AND expires_at < NOW()
                    RETURNING id, nats_reply_subject
                    """
                )

            count = 0
            for row in rows:
                req_id = str(row["id"])
                reply_subject = row.get("nats_reply_subject")
                # Try msg.respond() first, fallback to publish
                msg = self._pending_msgs.pop(req_id, None)
                if msg:
                    try:
                        await msg.respond(json.dumps({"approved": False, "reason": "approval timed out"}).encode())
                    except Exception:
                        await self._nats_reply(reply_subject, False, "approval timed out")
                else:
                    await self._nats_reply(reply_subject, False, "approval timed out")
                count += 1

                await self._broadcast_sse("request_decided", {
                    "id": str(row["id"]),
                    "status": "expired",
                    "decided_by": "system",
                })

            if count > 0:
                logger.info("Expired %d sudo approval request(s)", count)
            return count
        except Exception as e:
            logger.error("Expiration sweep failed: %s", e)
            return 0

    # =========================================================================
    # Auto-approval rules
    # =========================================================================

    async def _evaluate_auto_rules(self, command: str) -> Optional[str]:
        """Evaluate auto-approval rules against a command string.

        Returns "approve", "deny", "review", or None (no match).
        """
        if not self._db:
            return None

        # Shell metacharacter check — always require human review.
        if _SHELL_META_RE.search(command):
            logger.debug("Shell metacharacters in '%s' — skipping auto-approval", command)
            return None

        try:
            async with self._db.acquire() as conn:
                rules = await conn.fetch(
                    """
                    SELECT pattern, action FROM sudo_auto_rules
                    WHERE enabled = TRUE
                    ORDER BY priority ASC
                    """
                )

            for rule in rules:
                if fnmatch(command, rule["pattern"]):
                    return rule["action"]

            return None
        except Exception as e:
            logger.error("Auto-rule evaluation failed: %s", e)
            return None

    async def list_rules(self) -> list[dict]:
        """List all auto-approval rules."""
        if not self._db:
            return []
        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM sudo_auto_rules ORDER BY priority ASC"
                )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Failed to list auto-rules: %s", e)
            return []

    async def create_rule(
        self,
        pattern: str,
        action: str,
        priority: int = 100,
        description: str = "",
        created_by: str = "operator",
    ) -> Optional[dict]:
        """Create an auto-approval rule."""
        if action not in ("approve", "deny", "review"):
            return None
        if not self._db:
            return None

        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO sudo_auto_rules (pattern, action, priority, description, created_by)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING *
                    """,
                    pattern, action, priority, description, created_by,
                )
            return dict(row) if row else None
        except Exception as e:
            logger.error("Failed to create auto-rule: %s", e)
            return None

    async def delete_rule(self, rule_id: str) -> bool:
        """Delete an auto-approval rule."""
        if not self._db:
            return False
        try:
            async with self._db.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM sudo_auto_rules WHERE id = $1",
                    rule_id,
                )
            return "DELETE 1" in result
        except Exception as e:
            logger.error("Failed to delete auto-rule %s: %s", rule_id, e)
            return False

    # =========================================================================
    # Query
    # =========================================================================

    async def list_requests(
        self,
        job_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """List sudo approval requests with optional filters."""
        if not self._db:
            return []

        conditions = []
        params = []
        idx = 1

        if job_id:
            conditions.append(f"job_id = ${idx}")
            params.append(job_id)
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT id, job_id, vm_name, command, arguments,
                           working_directory, requesting_user, target_user,
                           status, requested_at, decided_at, decided_by,
                           decision_reason, ttl_seconds, expires_at, metadata
                    FROM sudo_approval_requests
                    {where}
                    ORDER BY requested_at DESC
                    LIMIT ${idx}
                    """,
                    *params, limit,
                )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Failed to list sudo requests: %s", e)
            return []

    async def get_request(self, request_id: str) -> Optional[dict]:
        """Get a single sudo approval request."""
        row = await self._get_request(request_id)
        return dict(row) if row else None

    # =========================================================================
    # SSE broadcasting
    # =========================================================================

    def subscribe_sse(self) -> asyncio.Queue:
        """Create a new SSE subscription queue.

        Returns a Queue that receives (event_type, data_dict) tuples.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._sse_queues.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue) -> None:
        """Remove an SSE subscription queue."""
        try:
            self._sse_queues.remove(q)
        except ValueError:
            pass

    async def _broadcast_sse(self, event_type: str, data: dict) -> None:
        """Push an event to all connected SSE clients."""
        dead = []
        for q in self._sse_queues:
            try:
                q.put_nowait((event_type, data))
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_queues.remove(q)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _insert_request(
        self,
        job_id: str,
        vm_name: str,
        command: str,
        arguments: list,
        cwd: str,
        requesting_user: str,
        target_user: str,
        nats_reply_subject: Optional[str],
        metadata: dict,
    ) -> Optional[str]:
        """Insert a new sudo approval request. Returns the request ID."""
        if not self._db:
            return None
        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO sudo_approval_requests
                        (job_id, vm_name, command, arguments, working_directory,
                         requesting_user, target_user, nats_reply_subject, metadata,
                         expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                            NOW() + INTERVAL '300 seconds')
                    RETURNING id
                    """,
                    job_id, vm_name, command, arguments, cwd,
                    requesting_user, target_user, nats_reply_subject,
                    json.dumps(metadata),
                )
            return str(row["id"]) if row else None
        except Exception as e:
            logger.error("Failed to insert sudo request: %s", e)
            return None

    async def _get_request(self, request_id: str):
        """Fetch a single request row."""
        if not self._db:
            return None
        try:
            async with self._db.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT * FROM sudo_approval_requests WHERE id = $1",
                    request_id,
                )
        except Exception as e:
            logger.error("Failed to get sudo request %s: %s", request_id, e)
            return None

    async def _finalize_request(
        self,
        request_id: str,
        status: str,
        reason: str,
        decided_by: str,
        nats_reply_subject: Optional[str],
    ) -> None:
        """Set the final status and send the NATS reply."""
        if self._db:
            try:
                async with self._db.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE sudo_approval_requests
                        SET status = $2, decided_at = NOW(),
                            decided_by = $3, decision_reason = $4
                        WHERE id = $1 AND status = 'pending'
                        """,
                        request_id, status, decided_by, reason,
                    )
            except Exception as e:
                logger.error("Failed to finalize sudo request %s: %s", request_id, e)

        approved = status in ("approved", "auto_approved")
        await self._nats_reply(nats_reply_subject, approved, reason)

    async def _nats_reply(
        self, reply_subject: Optional[str], approved: bool, reason: str = ""
    ) -> None:
        """Publish an approval/denial to the daemon via the stored reply subject."""
        if not reply_subject or not self._nc:
            return

        response = {"approved": approved}
        if reason:
            response["reason"] = reason

        try:
            logger.info("Publishing NATS reply to %s: %s", reply_subject, response)
            await self._nc.publish(reply_subject, json.dumps(response).encode())
            await self._nc.flush()
            logger.info("NATS reply published and flushed to %s", reply_subject)
        except Exception as e:
            logger.error("Failed to publish NATS reply to %s: %s", reply_subject, e)


# Singleton instance
sudo_gate = SudoGateService()
