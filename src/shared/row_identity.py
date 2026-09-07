"""Stable transcript-row identity shared by persistence and API projections."""

import uuid
from typing import Optional

# Fixed namespace for deriving a UUID primary key from a non-UUID message id.
# In-memory message ids are provider-issued (``chatcmpl-…``, ``resp_…``) or
# locally minted (``msg_…``) — none are valid UUIDs, but ``thread_messages.id``
# is a UUID column. uuid5 maps an id deterministically, so re-saving the same
# message lands on the same row (``ON CONFLICT (id)`` dedup).
_THREAD_MSG_ID_NS = uuid.UUID("4b9d8f7e-2c3a-5d6b-8e1f-0a1b2c3d4e5f")


def _coerce_row_id(raw_id: Optional[str]) -> str:
    """Map a caller-supplied message id to a valid UUID for ``thread_messages.id``.

    Already-valid UUIDs (restored rows, the user-message fallback) pass through;
    provider/minted ids are derived deterministically via uuid5 so the upsert is
    idempotent across a message's incremental write and its turn-complete
    reconciliation; ``None`` mints a fresh UUID for single-shot rows (user
    message, summary). The DB row id has always been independent of the in-memory
    message id (restore assigns a fresh uuid4 either way), so deriving it changes
    no correlation — it only makes the key stable.
    """
    if not raw_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(raw_id)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_THREAD_MSG_ID_NS, str(raw_id)))
