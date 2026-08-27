"""Stable identities for checkpoint-coupled worker steering entries.

New queued replies carry an explicit UUID.  Replies written by older
orchestrator versions do not, so stateless workers need a deterministic key
that both the graph and the ack endpoint can reconstruct without mutating the
legacy row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def queued_reply_key(reply: Mapping[str, Any]) -> str:
    """Return the durable identity for one queued reply.

    The explicit id is the normal path.  The content hash is deliberately
    limited to the immutable fields present on legacy queued rows; ack metadata
    added after consumption must not change their identity.
    """

    reply_id = str(reply.get("id") or "").strip()
    if reply_id:
        return f"id:{reply_id}"

    legacy_payload = {
        field: str(reply.get(field) or "")
        for field in ("thread_id", "timestamp", "message")
    }
    encoded = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"legacy:{hashlib.sha256(encoded).hexdigest()}"


def context_delivery_key(
    delivery_kind: str,
    value: Any,
    *,
    delivery_id: Any = None,
    companion: Any = None,
) -> str:
    """Identify an orchestrator-context one-shot delivered to the graph.

    Stateless producers stamp a UUID companion.  The content hash keeps rows
    written before that rollout safe and retryable; ``companion`` binds paired
    values such as feedback and its explanatory reason.
    """

    kind = str(delivery_kind).strip()
    explicit_id = str(delivery_id or "").strip()
    if explicit_id:
        return f"{kind}:id:{explicit_id}"
    encoded = json.dumps(
        {"value": value, "companion": companion},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{kind}:legacy:{hashlib.sha256(encoded).hexdigest()}"


def _checkpoint_values(checkpoint: Any) -> Mapping[str, Any]:
    if not isinstance(checkpoint, Mapping):
        return {}
    values = checkpoint.get("channel_values")
    return values if isinstance(values, Mapping) else {}


def _checkpoint_id(checkpoint: Any, next_config: Any) -> str:
    if isinstance(next_config, Mapping):
        configurable = next_config.get("configurable")
        if isinstance(configurable, Mapping):
            value = str(configurable.get("checkpoint_id") or "").strip()
            if value:
                return value
    if isinstance(checkpoint, Mapping):
        return str(checkpoint.get("id") or "").strip()
    return ""


@dataclass
class CheckpointSteeringAcker:
    """Ack cumulative steering sets only after their checkpoint committed.

    ``FencedAsyncPostgresSaver`` invokes this object synchronously after a
    successful ``aput`` transaction.  A failed request is left unsuppressed so
    the next checkpoint (including the next claim's batch-arming update) retries
    it.  Successful entries are suppressed only for this claim; a successor
    starts empty and therefore performs one reconciliation against its latest
    checkpoint.
    """

    job_id: str
    client: Any
    timeout_seconds: float = 15.0
    _acked_guidance_ids: set[str] = field(default_factory=set, init=False)
    _acked_reply_keys: set[str] = field(default_factory=set, init=False)
    _acked_feedback_keys: set[str] = field(default_factory=set, init=False)
    _acked_delegation_keys: set[str] = field(default_factory=set, init=False)

    async def __call__(
        self,
        config: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        metadata: Mapping[str, Any],
        next_config: Mapping[str, Any],
    ) -> None:
        del config, metadata
        values = _checkpoint_values(checkpoint)
        checkpoint_id = _checkpoint_id(checkpoint, next_config)
        await self.reconcile_values(values, checkpoint_id=checkpoint_id)

    async def reconcile_values(
        self,
        values: Mapping[str, Any],
        *,
        checkpoint_id: str,
    ) -> bool:
        """Retry acks represented by an already-committed checkpoint.

        A successor can reclaim an END checkpoint after the last post-commit
        HTTP ack failed. It will not write another graph checkpoint before
        terminal reporting, so claim-time reconciliation must use the durable
        snapshot and its checkpoint id directly.
        """

        guidance_ids = sorted(
            {
                str(value)
                for value in values.get("delivered_guidance_ids") or []
                if value is not None
            }
            - self._acked_guidance_ids
        )
        reply_keys = sorted(
            {
                str(value)
                for value in values.get("delivered_reply_keys") or []
                if value is not None
            }
            - self._acked_reply_keys
        )
        feedback_keys = sorted(
            {
                str(value)
                for value in values.get("delivered_feedback_keys") or []
                if value is not None
            }
            - self._acked_feedback_keys
        )
        delegation_keys = sorted(
            {
                str(value)
                for value in values.get("delivered_delegation_keys") or []
                if value is not None
            }
            - self._acked_delegation_keys
        )
        if (
            not guidance_ids
            and not reply_keys
            and not feedback_keys
            and not delegation_keys
        ):
            return True

        if not checkpoint_id:
            raise RuntimeError("durable steering ack requires a checkpoint id")

        acknowledged = await asyncio.wait_for(
            self.client.ack_job_guidance(
                self.job_id,
                guidance_ids=guidance_ids,
                reply_keys=reply_keys,
                feedback_keys=feedback_keys,
                delegation_keys=delegation_keys,
                checkpoint_id=checkpoint_id,
            ),
            timeout=max(0.1, float(self.timeout_seconds)),
        )
        if not acknowledged:
            logger.warning(
                "Durable steering ack failed; next checkpoint will retry "
                "(job=%s checkpoint=%s guidance=%d replies=%d feedback=%d "
                "delegations=%d)",
                self.job_id,
                checkpoint_id,
                len(guidance_ids),
                len(reply_keys),
                len(feedback_keys),
                len(delegation_keys),
            )
            return False

        self._acked_guidance_ids.update(guidance_ids)
        self._acked_reply_keys.update(reply_keys)
        self._acked_feedback_keys.update(feedback_keys)
        self._acked_delegation_keys.update(delegation_keys)
        return True


__all__ = [
    "CheckpointSteeringAcker",
    "context_delivery_key",
    "queued_reply_key",
]
