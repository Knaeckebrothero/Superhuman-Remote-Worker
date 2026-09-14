"""Request bodies for the Officer Post and the Officer's own agent routes.

Moved verbatim from ``orchestrator.main`` (R1.B07 lane O, census group
``S_OFFICER``). Field names, defaults and descriptions are the published
OpenAPI request schema, so they are part of each route's identity and none of
them is rewritten here. ``OfficerNoteRequest`` keeps ``extra="forbid"``: a
typo'd field on a note that carries command authority must fail, not be
dropped.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OfficerWakeRequest(BaseModel):
    minutes: int
    reason: str = ""


class OfficerNotifyRequest(BaseModel):
    message: str
    urgency: str = "log"  # log | digest | page
    subject: str = ""


class OfficerDecommissionRequest(BaseModel):
    """Body for POST .../officer/decommission."""

    force: bool = Field(
        False,
        description=(
            "Acknowledge the in-flight-jobs warning (jobs keep running — force "
            "never cancels them) and end a mid-turn session."
        ),
    )
    reason: str | None = Field(
        None, description="Recorded on the incarnation entry (default 'decommissioned')"
    )


class OfficerHoldRequest(BaseModel):
    """Body for POST .../officer/hold."""

    note: str | None = Field(None, description="Shown on the card's held badge")


class OfficerNoteRequest(BaseModel):
    """Body for POST .../officer/note."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., description="What the Legate wants him to know or do")


__all__ = [
    "OfficerDecommissionRequest",
    "OfficerHoldRequest",
    "OfficerNoteRequest",
    "OfficerNotifyRequest",
    "OfficerWakeRequest",
]
