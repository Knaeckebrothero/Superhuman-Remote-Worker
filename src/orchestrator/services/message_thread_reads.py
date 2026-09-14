"""Message-thread reads for one job: the list and one thread's ordered messages.

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane M, census group
``J_messaging``). Both reads enrich their rows with the job's freeze status, so
the authorized job row arrives from the route's own gate rather than being read
a second time here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


@dataclass
class MessageThreadReadDependencies:
    """The job store, resolved per invocation."""

    store: Any


async def list_message_threads(
    request: Request,
    job_id: str,
    *,
    dependencies: MessageThreadReadDependencies,
    job: dict[str, Any],
) -> dict[str, Any]:
    """List message threads for a job."""
    try:
        threads = await dependencies.store.get_message_threads(job_id)

        # Enrich with job freeze status
        freeze_data = job.get("freeze_data")
        if isinstance(freeze_data, str):
            try:
                freeze_data = json.loads(freeze_data)
            except json.JSONDecodeError:
                freeze_data = None

        waiting_thread = None
        if job.get("status") == "waiting_for_reply" and freeze_data:
            waiting_thread = freeze_data.get("thread_id")

        for thread in threads:
            thread["status"] = (
                "waiting_for_reply"
                if thread["thread_id"] == waiting_thread
                else "active"
            )

        return {"threads": threads}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to list message threads for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Thread Detail & Action Center Endpoints
# =============================================================================


async def get_thread_detail(
    request: Request,
    job_id: str,
    thread_id: str,
    *,
    dependencies: MessageThreadReadDependencies,
    job: dict[str, Any],
) -> dict[str, Any]:
    """Get full ordered messages within a thread."""
    try:
        thread = await dependencies.store.get_thread_messages(job_id, thread_id)
        if not thread:
            raise HTTPException(
                status_code=404,
                detail=f"Thread '{thread_id}' not found for job '{job_id}'",
            )

        # Enrich with job freeze status
        freeze_data = job.get("freeze_data")
        if isinstance(freeze_data, str):
            try:
                freeze_data = json.loads(freeze_data)
            except json.JSONDecodeError:
                freeze_data = None

        if (
            job.get("status") == "waiting_for_reply"
            and freeze_data
            and freeze_data.get("thread_id") == thread_id
        ):
            thread["status"] = "waiting_for_reply"
        else:
            thread["status"] = "active"

        return thread

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Failed to get thread detail for job {job_id} thread {thread_id}: {e}"
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


__all__ = [
    "MessageThreadReadDependencies",
    "get_thread_detail",
    "list_message_threads",
]
