"""The truthful-read envelope for supervision reads (officer_supervision_surface §4).

Every supervision/evidence read handler assembles the same structured envelope
before formatting::

    {"scope": {...}, "observed_at": "...", "sources": [...], "data": ...}

and the formatters preserve four distinctions in compact text:

- ``empty``       — the source was reached and no rows/items exist;
- ``unavailable`` — the source could not be reached or the transport is
                    unsupported (NEVER rendered as an empty result);
- ``stale``       — the source was reached, but its last known revision or
                    observation is older than the declared freshness window
                    (Gitea-backed reads carry the repo-head line for this);
- ``partial``     — one section failed while the others remain usable.

No handler manufactures progress or converts missing telemetry into zero.

Pure stdlib + httpx (the shared-surface import contract), no framework types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Literal

import httpx

SourceStatus = Literal["fresh", "empty", "stale", "unavailable", "partial"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Source:
    """One consulted backend and how trustworthy its answer is."""

    name: str
    status: SourceStatus = "fresh"
    as_of: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.as_of:
            payload["as_of"] = self.as_of
        if self.reason:
            payload["reason"] = self.reason
        return payload


def build_envelope(
    *,
    scope: dict[str, Any],
    sources: list[Source],
    data: Any,
) -> dict[str, Any]:
    """Assemble the §4 truthful-read envelope."""
    return {
        "scope": {key: value for key, value in scope.items() if value},
        "observed_at": now_iso(),
        "sources": [source.as_dict() for source in sources],
        "data": data,
    }


def overall_status(sources: list[Source]) -> SourceStatus:
    """Collapse per-source statuses into the envelope-level distinction."""
    if not sources:
        return "fresh"
    statuses = {source.status for source in sources}
    if statuses <= {"fresh", "empty"}:
        return "empty" if statuses == {"empty"} else "fresh"
    if statuses == {"unavailable"}:
        return "unavailable"
    if "unavailable" in statuses or "partial" in statuses:
        return "partial"
    return "stale"


def render_source_notes(sources: list[Source]) -> list[str]:
    """Compact honesty lines for every source that is not simply fresh.

    ``empty`` is not rendered here — the data section already says
    "No X found." and doubling it would be noise. The notes exist so an
    unavailable/stale/partial read can never masquerade as an empty one.
    """
    lines: list[str] = []
    for source in sources:
        if source.status in ("fresh", "empty"):
            continue
        note = f"[{source.name}: {source.status}"
        if source.reason:
            note += f" — {source.reason}"
        if source.as_of:
            note += f" (as of {source.as_of})"
        lines.append(note + "]")
    return lines


def is_empty_payload(data: Any) -> bool:
    """Whether a successfully fetched payload counts as ``empty``."""
    if data is None:
        return True
    if isinstance(data, (list, tuple, set, dict, str)):
        return len(data) == 0
    return False


async def observe(
    name: str,
    awaitable: Awaitable[Any],
    *,
    empty_check: bool = True,
) -> tuple[Any, Source]:
    """Await one backend call and classify the outcome as a Source.

    Failures become ``unavailable`` with a sanitized reason — the raw
    exception (httpx errors embed internal URLs) never reaches tool output.
    """
    try:
        data = await awaitable
    except Exception as error:  # noqa: BLE001 — classified, never re-raised
        return None, Source(
            name=name, status="unavailable", reason=friendly_reason(error)
        )
    status: SourceStatus = (
        "empty" if (empty_check and is_empty_payload(data)) else "fresh"
    )
    return data, Source(name=name, status=status, as_of=now_iso())


# ---------------------------------------------------------------------------
# Sanitized error rendering (F6)
# ---------------------------------------------------------------------------
#
# str(httpx.HTTPStatusError) embeds the full request URL ("... for url
# 'http://srw-orchestrator:8085/api/...'"), which is an internal address the
# model has no business seeing. Extract the server's own detail instead, and
# keep transport failures generic.


def http_status_of(error: Exception) -> int | None:
    """The HTTP status code carried by an httpx error, if any."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return int(status) if isinstance(status, int) else None


def response_detail(error: Exception, *, limit: int = 300) -> str | None:
    """The server-supplied error body of an httpx status error, bounded.

    Prefers the FastAPI ``{"detail": ...}`` field; falls back to the raw
    response text (the 409 reason contract for steer_job — F6). Returns
    None when the error carries no response.
    """
    response = getattr(error, "response", None)
    if response is None:
        return None
    detail: Any = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("detail")
    except Exception:  # noqa: BLE001 — non-JSON body
        detail = None
    if detail is None:
        try:
            detail = response.text
        except Exception:  # noqa: BLE001
            detail = None
    if detail is None:
        return None
    text = detail if isinstance(detail, str) else str(detail)
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def friendly_reason(error: Exception) -> str:
    """One-line sanitized failure reason: no raw httpx text, no internal URLs."""
    status = http_status_of(error)
    if status is not None:
        detail = response_detail(error)
        if detail:
            return f"HTTP {status}: {detail}"
        return f"HTTP {status}"
    if isinstance(error, httpx.TimeoutException):
        return "orchestrator request timed out"
    if isinstance(error, httpx.RequestError):
        return "could not connect to the orchestrator"
    if isinstance(error, ValueError):
        return str(error)
    return f"{type(error).__name__}: {error}"


__all__ = [
    "Source",
    "SourceStatus",
    "build_envelope",
    "friendly_reason",
    "http_status_of",
    "is_empty_payload",
    "now_iso",
    "observe",
    "overall_status",
    "render_source_notes",
    "response_detail",
]
