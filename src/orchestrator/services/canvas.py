"""Dynamic Canvas presentation-state service.

Slice 0 deliberately owns only the durable control-plane state.  It does not
read workspace files, connect to workspace ports, or expose model tools.  A
future source adapter must validate the runtime source first and then pass the
server-normalized logical source to :class:`CanvasService`.

The service keeps presentation revisions atomic in PostgreSQL and computes the
security-relevant source fingerprint itself.  Public response serialization is
also centralized here so ``If-Match`` is checked against the exact authorized
bytes represented by the strong state ETag.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from orchestrator.services.stateless_workspace_gate import (
    stateless_session_workspace_check,
)

logger = logging.getLogger(__name__)

MAIN_CANVAS_ID = "main"
SOURCE_FINGERPRINT_SCHEMA_VERSION = 1
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_EDIT_COORDINATOR_SEMAPHORE = asyncio.Semaphore(
    max(1, int(os.getenv("CANVAS_MAX_EDIT_COORDINATORS", "4")))
)
_EDIT_COORDINATOR_QUEUE_TIMEOUT = float(
    os.getenv("CANVAS_EDIT_COORDINATOR_QUEUE_TIMEOUT", "5")
)

CanvasRenderer = Literal[
    "auto", "markdown", "text", "html", "html-interactive", "image", "office"
]
CanvasStatus = Literal[
    "ready",
    "starting",
    "source_changed",
    "unavailable",
    "ended",
    "error",
    "cleared",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceFileSource(_StrictFrozenModel):
    """A source already normalized and runtime-validated by a file adapter."""

    type: Literal["workspace_file"] = "workspace_file"
    path: str = Field(min_length=1, max_length=4096)
    workspace_generation: UUID


class WorkspaceAppRoute(_StrictFrozenModel):
    path_prefix: str = Field(min_length=1, max_length=2048)
    port: int = Field(ge=1, le=65535)


class WorkspaceAppSource(_StrictFrozenModel):
    """Normalized app source; ``workspace_port`` is converted to this shape."""

    type: Literal["workspace_app"] = "workspace_app"
    entry_port: int = Field(ge=1, le=65535)
    entry_path: str = Field(default="/", min_length=1, max_length=2048)
    routes: tuple[WorkspaceAppRoute, ...] = Field(default=(), max_length=8)
    manifest_path: str | None = Field(default=None, max_length=4096)
    manifest_version: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    workspace_generation: UUID

    @field_validator("routes")
    @classmethod
    def _sort_routes(
        cls, routes: tuple[WorkspaceAppRoute, ...]
    ) -> tuple[WorkspaceAppRoute, ...]:
        # Source adapters reject duplicate/ambiguous routes.  Sorting here is a
        # final normalization step so persistence and fingerprints never depend
        # on YAML/list input ordering.
        return tuple(sorted(routes, key=lambda route: (route.path_prefix, route.port)))


class BrowserSource(_StrictFrozenModel):
    """A concrete browser generation, never the moving alias ``current``."""

    type: Literal["browser"] = "browser"
    browser_generation: UUID


NormalizedCanvasSource = Annotated[
    WorkspaceFileSource | WorkspaceAppSource | BrowserSource,
    Field(discriminator="type"),
]
_SOURCE_ADAPTER = TypeAdapter(NormalizedCanvasSource)


class CanvasSetInput(_StrictFrozenModel):
    """Validated input to the shared state action.

    Structural validation here is intentionally not a runtime capability
    check.  Callers must first prove that a file/app/browser exists and is
    authorized.  Slice 0 has no public or internal HTTP adapter that accepts
    this model.
    """

    source: NormalizedCanvasSource
    title: str = Field(min_length=1, max_length=200)
    renderer: CanvasRenderer = "auto"
    editable: bool = False
    alt_text: str | None = Field(default=None, max_length=1000)
    source_version: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    new_app: bool = False

    @field_validator("title")
    @classmethod
    def _title_is_descriptive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _source_specific_fields(self) -> "CanvasSetInput":
        is_file = isinstance(self.source, WorkspaceFileSource)
        is_app = isinstance(self.source, WorkspaceAppSource)
        if is_file and self.source_version is None:
            raise ValueError("workspace_file requires a strong source_version")
        if not is_file and self.source_version is not None:
            raise ValueError("source_version is supported only for workspace_file")
        if self.new_app and not is_app:
            raise ValueError("new_app is supported only for workspace_app")
        if self.editable and (
            not is_file
            or self.renderer
            not in {"markdown", "text", "html", "html-interactive", "office"}
        ):
            raise ValueError(
                "editable Canvas sources require a validated text, HTML, or "
                "Office renderer"
            )
        return self


class CanvasCapabilities(_StrictFrozenModel):
    can_edit: bool = False
    can_pop_out: bool = False
    can_take_control: bool = False
    can_create_viewer_session: bool = False
    can_stream_browser: bool = False
    can_view_office: bool = False


class PublicWorkspaceFileSource(_StrictFrozenModel):
    type: Literal["workspace_file"] = "workspace_file"
    path: str


class PublicWorkspaceAppSource(_StrictFrozenModel):
    type: Literal["workspace_app"] = "workspace_app"
    manifest_path: str | None = None
    entry_path: str = "/"


class PublicBrowserSource(_StrictFrozenModel):
    type: Literal["browser"] = "browser"


PublicCanvasSource = Annotated[
    PublicWorkspaceFileSource | PublicWorkspaceAppSource | PublicBrowserSource,
    Field(discriminator="type"),
]


class CanvasPublicState(_StrictFrozenModel):
    canvas_id: Literal["main"] = MAIN_CANVAS_ID
    source: PublicCanvasSource | None
    title: str | None
    renderer: CanvasRenderer
    editable: bool
    alt_text: str | None
    presentation_revision: int = Field(ge=0)
    source_version: str | None
    status: CanvasStatus
    capabilities: CanvasCapabilities
    updated_at: str
    content_url: str | None = None
    # Where the bytes behind `content_url` come from. Deliberately an additive
    # optional FIELD rather than a new `status` value: the cockpit type guard
    # drops the whole state object on an unknown status (blank pane) but
    # ignores unknown fields, so an older cockpit against a newer orchestrator
    # renders a snapshot-backed Canvas instead of breaking. Absent means
    # "workspace" — never treat absence as unknown-and-blocked.
    content_origin: Literal["workspace", "snapshot"] | None = None
    # When `content_origin` is "snapshot": when those bytes were captured.
    content_captured_at: str | None = None


class CanvasInvalidationParams(_StrictFrozenModel):
    canvas_id: Literal["main"] = MAIN_CANVAS_ID
    presentation_revision: int = Field(ge=1)
    source_type: Literal["workspace_file", "workspace_app", "browser"] | None
    updated_at: str


class CanvasInvalidation(_StrictFrozenModel):
    method: Literal["canvas.updated", "canvas.cleared"]
    params: CanvasInvalidationParams


@dataclass(frozen=True, slots=True)
class CanvasRecord:
    thread_id: str
    canvas_id: str
    source: WorkspaceFileSource | WorkspaceAppSource | BrowserSource | None
    title: str | None
    renderer: CanvasRenderer
    editable: bool
    alt_text: str | None
    presentation_revision: int
    source_fingerprint: str | None
    source_version: str | None
    origin_generation: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "CanvasRecord":
        values = dict(row)
        raw_source = values.get("source")
        if isinstance(raw_source, str):
            raw_source = json.loads(raw_source)
        source = (
            _SOURCE_ADAPTER.validate_python(raw_source)
            if raw_source is not None
            else None
        )
        return cls(
            thread_id=str(values["thread_id"]),
            canvas_id=str(values["canvas_id"]),
            source=source,
            title=values.get("title"),
            renderer=values.get("renderer") or "auto",
            editable=bool(values.get("editable", False)),
            alt_text=values.get("alt_text"),
            presentation_revision=int(values.get("presentation_revision") or 0),
            source_fingerprint=values.get("source_fingerprint"),
            source_version=values.get("source_version"),
            origin_generation=(
                UUID(str(values["origin_generation"]))
                if values.get("origin_generation") is not None
                else None
            ),
            created_at=_coerce_datetime(values.get("created_at")),
            updated_at=_coerce_datetime(values.get("updated_at")),
        )


@dataclass(frozen=True, slots=True)
class CanvasRepresentation:
    state: CanvasPublicState
    payload: bytes
    etag: str


@dataclass(frozen=True, slots=True)
class CanvasMutation:
    changed: bool
    record: CanvasRecord | None


class CanvasError(Exception):
    """Base class for typed Canvas application-action errors."""


class CanvasPreconditionRequired(CanvasError):
    pass


class CanvasPreconditionFailed(CanvasError):
    pass


class CanvasEditError(CanvasError):
    """Typed conditional-edit failure shared by the HTTP adapter and tests."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


_STATELESS_CANVAS_WRITE_STATUSES = frozenset(
    {"created", "active", "idle", "awaiting_user"}
)


def _require_stateless_canvas_workspace_writer(thread: Any) -> None:
    """Fence orchestrator-owned workspace I/O against stateless retirement.

    The caller holds ``threads FOR SHARE`` for the whole remote operation. An
    End/reaper writer that published its marker first is rejected here; one
    arriving after this check waits for the share lock until the remote writer
    and Canvas revision commit finish. Pinned rows deliberately bypass this
    policy and retain their historical behavior.
    """

    if str(thread.get("execution_lane") or "") != "stateless":
        return

    refused = str(thread.get("status") or "") not in _STATELESS_CANVAS_WRITE_STATUSES
    _, session_workspace_refusal = stateless_session_workspace_check(thread)
    if session_workspace_refusal is not None:
        refused = True
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = None
    if not isinstance(metadata, dict):
        refused = True
        metadata = {}

    # Presence of either retirement marker is authority to stop admitting new
    # workspace I/O, even if a malformed/legacy value is falsey.  The loss
    # ledger is unresolved-only and its writer removes the key when settled,
    # so every present value remains authority to stop.
    if "_stateless_workspace_retirement_pending" in metadata:
        refused = True
    if "_stateless_claim_retirement" in metadata:
        refused = True
    if "_stateless_claim_loss_hold" in metadata:
        refused = True
    if "_stateless_claim_losses" in metadata:
        refused = True
    if "protected_cloud" in metadata and metadata["protected_cloud"] is not False:
        refused = True

    if refused:
        raise CanvasEditError(
            409,
            "canvas_editing_unavailable",
            "This stateless session is not accepting Canvas workspace writes",
        )


def canvas_file_lock_key(thread_id: str, canonical_path: str) -> int:
    """Return a stable signed PostgreSQL advisory-lock key for one file."""

    material = (
        b"srw-canvas-file-edit-v1\x00"
        + thread_id.encode("utf-8")
        + b"\x00"
        + canonical_path.encode("utf-8")
    )
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


def canvas_presentation_lock_key(thread_id: str, canvas_id: str) -> int:
    """Return a domain-separated transaction lock for one presentation slot."""

    material = (
        b"srw-canvas-presentation-v1\x00"
        + thread_id.encode("utf-8")
        + b"\x00"
        + canvas_id.encode("utf-8")
    )
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


@asynccontextmanager
async def _canvas_mutation_admission():
    admitted = False
    try:
        try:
            await asyncio.wait_for(
                _EDIT_COORDINATOR_SEMAPHORE.acquire(),
                timeout=max(0.05, _EDIT_COORDINATOR_QUEUE_TIMEOUT),
            )
            admitted = True
        except TimeoutError as exc:
            raise CanvasEditError(
                503,
                "canvas_edit_capacity_exhausted",
                "Canvas edit capacity is currently exhausted",
            ) from exc
        yield
    finally:
        if admitted:
            _EDIT_COORDINATOR_SEMAPHORE.release()


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif value is None:
        # Rows always carry timestamps.  Keeping this fallback deterministic
        # makes a malformed test double fail visibly at serialization rather
        # than yielding an attribute error deep inside a response.
        parsed = datetime.fromtimestamp(0, tz=timezone.utc)
    else:
        raise TypeError(f"unsupported Canvas timestamp: {type(value).__name__}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return _coerce_datetime(value).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode the constrained source-fingerprint domain as RFC 8785 JSON.

    Fingerprint material contains only fixed ASCII object keys, strings,
    bounded integers, nulls, lists, and dictionaries.  In that constrained
    domain Python's sorted, compact JSON form is RFC 8785-compatible.  Reject
    floats and lone surrogate code points explicitly so future source fields
    cannot silently widen the domain into Python/ECMAScript number or Unicode
    edge cases.
    """

    def validate(item: Any) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            if abs(item) > 2**53 - 1:
                raise ValueError("canonical Canvas integers must be IEEE-754 safe")
            return
        if isinstance(item, float):
            raise TypeError("floats are not valid Canvas fingerprint material")
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ValueError("lone surrogate in Canvas fingerprint material")
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError("Canvas fingerprint object keys must be strings")
                validate(key)
                validate(child)
            return
        raise TypeError(f"unsupported Canvas fingerprint value: {type(item).__name__}")

    validate(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_source_fingerprint(source: NormalizedCanvasSource) -> str:
    """Return the shared ``sha256:`` identity for a normalized source."""

    if isinstance(source, WorkspaceFileSource):
        identity: dict[str, Any] = {
            "type": "workspace_file",
            "workspace_generation": str(source.workspace_generation),
            "path": source.path,
        }
    elif isinstance(source, WorkspaceAppSource):
        identity = {
            "type": "workspace_app",
            "workspace_generation": str(source.workspace_generation),
            "entry_port": source.entry_port,
            "entry_path": source.entry_path,
            "routes": [
                {"prefix": route.path_prefix, "port": route.port}
                for route in sorted(
                    source.routes,
                    key=lambda route: (route.path_prefix, route.port),
                )
            ],
            "manifest_hash": source.manifest_version,
        }
    elif isinstance(source, BrowserSource):
        identity = {
            "type": "browser",
            "browser_generation": str(source.browser_generation),
        }
    else:  # pragma: no cover - closed Pydantic union, defensive for future edits
        raise TypeError(f"unsupported Canvas source: {type(source).__name__}")

    material = {
        "schema_version": SOURCE_FINGERPRINT_SCHEMA_VERSION,
        "source": identity,
    }
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _public_source(source: NormalizedCanvasSource | None) -> PublicCanvasSource | None:
    if isinstance(source, WorkspaceFileSource):
        return PublicWorkspaceFileSource(path=source.path)
    if isinstance(source, WorkspaceAppSource):
        return PublicWorkspaceAppSource(
            manifest_path=source.manifest_path,
            entry_path=source.entry_path,
        )
    if isinstance(source, BrowserSource):
        return PublicBrowserSource()
    return None


def build_public_canvas_representation(
    record: CanvasRecord,
    *,
    status: CanvasStatus | None = None,
    capabilities: CanvasCapabilities | None = None,
    content_url: str | None = None,
    content_origin: Literal["workspace", "snapshot"] | None = None,
    # Accepts a datetime or an already-serialized ISO string, like every other
    # timestamp here. Callers that rebuild a previously-served representation
    # for an If-Match comparison pass the string straight back, which is what
    # keeps their ETag byte-identical to the one the client holds.
    content_captured_at: datetime | str | None = None,
) -> CanvasRepresentation:
    """Serialize one exact caller-visible state and derive its strong ETag."""

    derived_status: CanvasStatus = status or (
        "cleared" if record.source is None else "unavailable"
    )
    state = CanvasPublicState(
        canvas_id=MAIN_CANVAS_ID,
        source=_public_source(record.source),
        title=record.title,
        renderer=record.renderer,
        editable=record.editable,
        alt_text=record.alt_text,
        presentation_revision=record.presentation_revision,
        source_version=record.source_version,
        status=derived_status,
        capabilities=capabilities or CanvasCapabilities(),
        updated_at=_utc_iso(record.updated_at),
        content_url=content_url,
        content_origin=content_origin,
        content_captured_at=(
            _utc_iso(content_captured_at) if content_captured_at is not None else None
        ),
    )
    payload = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    etag = f'"canvas:{record.presentation_revision}:{digest}"'
    return CanvasRepresentation(state=state, payload=payload, etag=etag)


def strong_state_precondition(header_value: str) -> str:
    """Return the strong form of one inbound Canvas state ``If-Match``.

    A compressing CDN in front of the API rewrites the strong ``ETag`` it
    forwards into the weak form, so ``W/"canvas:…"`` is the only value a
    browser can echo back for a state it read through one. The digest still
    names the exact representation we served — weakening marks the transfer
    encoding, not the state — so drop the marker and leave the caller
    comparing full digests, exactly as the conditional GET already does in
    ``_if_none_match_matches``. Never expands ``*``: these preconditions name
    one state, not "whatever exists now".
    """

    value = header_value.strip()
    if value.startswith("W/"):
        value = value[2:].lstrip()
    return value


def canvas_invalidation(
    record: CanvasRecord, *, method: Literal["canvas.updated", "canvas.cleared"]
) -> CanvasInvalidation:
    source_type = record.source.type if record.source is not None else None
    return CanvasInvalidation(
        method=method,
        params=CanvasInvalidationParams(
            presentation_revision=record.presentation_revision,
            source_type=source_type,
            updated_at=_utc_iso(record.updated_at),
        ),
    )


CanvasEventCallback = Callable[[CanvasInvalidation], Awaitable[None] | None]
CanvasRepresentationBuilder = Callable[[CanvasRecord], CanvasRepresentation]
CanvasFileWriter = Callable[[CanvasRecord, dict[str, Any]], Awaitable[str]]
# Returns the workspace generation the pinned bytes were re-verified under, or
# None when the file is gone, unreadable, or no longer byte-identical.
CanvasGenerationVerifier = Callable[
    [CanvasRecord, dict[str, Any]], Awaitable[UUID | None]
]


class CanvasService:
    """Atomic persistence actions for the one v1 Canvas slot per thread."""

    def __init__(
        self,
        db: Any,
        *,
        event_callback: CanvasEventCallback | None = None,
        snapshot_store: Any | None = None,
    ) -> None:
        self._db = db
        self._event_callback = event_callback
        # Optional durable-copy store. Only `clear` needs it: the stored copy
        # has to disappear in the same transaction that empties the Canvas,
        # because clearing nulls the source rather than deleting the row and
        # the FK cascade therefore never fires.
        self._snapshot_store = snapshot_store

    @staticmethod
    def _require_main(canvas_id: str) -> None:
        if canvas_id != MAIN_CANVAS_ID:
            raise ValueError("Dynamic Canvas v1 supports only canvas_id='main'")

    async def get(
        self, thread_id: str, *, canvas_id: str = MAIN_CANVAS_ID
    ) -> CanvasRecord | None:
        self._require_main(canvas_id)
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT thread_id, canvas_id, source, title, renderer, editable,
                       alt_text, presentation_revision, source_fingerprint,
                       source_version, origin_generation, created_at, updated_at
                FROM canvases
                WHERE thread_id = $1 AND canvas_id = $2
                """,
                thread_id,
                canvas_id,
            )
        return CanvasRecord.from_row(row) if row is not None else None

    async def set(
        self,
        thread_id: str,
        presentation: CanvasSetInput,
        *,
        canvas_id: str = MAIN_CANVAS_ID,
    ) -> CanvasMutation:
        """Set/republish validated state and advance its revision exactly once.

        This is an internal application action, not an HTTP/model boundary.
        Slice 1 source adapters will call it only after runtime validation.
        """

        self._require_main(canvas_id)
        source = presentation.source
        source_fingerprint = canonical_source_fingerprint(source)
        source_json = json.dumps(
            source.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        origin_candidate = uuid4() if isinstance(source, WorkspaceAppSource) else None

        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO canvases (
                    thread_id, canvas_id, source, title, renderer, editable,
                    alt_text, presentation_revision, source_fingerprint,
                    source_version, origin_generation
                )
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, 1, $8, $9, $10)
                ON CONFLICT ON CONSTRAINT uq_canvases_thread_canvas DO UPDATE SET
                    source = EXCLUDED.source,
                    title = EXCLUDED.title,
                    renderer = EXCLUDED.renderer,
                    editable = EXCLUDED.editable,
                    alt_text = EXCLUDED.alt_text,
                    presentation_revision = canvases.presentation_revision + 1,
                    source_fingerprint = EXCLUDED.source_fingerprint,
                    source_version = EXCLUDED.source_version,
                    origin_generation = CASE
                        WHEN EXCLUDED.origin_generation IS NOT NULL
                         AND canvases.origin_generation IS NOT NULL
                         AND canvases.source_fingerprint = EXCLUDED.source_fingerprint
                         AND NOT $11
                        THEN canvases.origin_generation
                        ELSE EXCLUDED.origin_generation
                    END,
                    updated_at = now()
                RETURNING thread_id, canvas_id, source, title, renderer, editable,
                          alt_text, presentation_revision, source_fingerprint,
                          source_version, origin_generation, created_at, updated_at
                """,
                thread_id,
                canvas_id,
                source_json,
                presentation.title,
                presentation.renderer,
                presentation.editable,
                presentation.alt_text,
                source_fingerprint,
                presentation.source_version,
                origin_candidate,
                presentation.new_app,
            )

        record = CanvasRecord.from_row(row)
        await self._emit(record, method="canvas.updated")
        return CanvasMutation(changed=True, record=record)

    async def set_if_changed(
        self,
        thread_id: str,
        presentation: CanvasSetInput,
        *,
        canvas_id: str = MAIN_CANVAS_ID,
        expected_presentation_revision: int | None = None,
    ) -> CanvasMutation:
        """Idempotently and conditionally stage one Browser presentation."""

        self._require_main(canvas_id)
        if (
            not isinstance(presentation.source, BrowserSource)
            or presentation.source_version is not None
            or presentation.new_app
            or presentation.renderer != "auto"
            or presentation.editable
            or presentation.alt_text is not None
        ):
            raise ValueError("set_if_changed is restricted to Browser presentations")

        source = presentation.source
        source_fingerprint = canonical_source_fingerprint(source)
        source_json = json.dumps(
            source.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        lock_key = canvas_presentation_lock_key(thread_id, canvas_id)
        changed = False
        result: CanvasRecord | None = None

        async with self._db.acquire() as conn:
            async with conn.transaction():
                # This precedes the row lookup intentionally. A row lock cannot
                # serialize two concurrent first presentations when no row exists.
                await conn.fetchval("SELECT pg_advisory_xact_lock($1)", lock_key)
                row = await conn.fetchrow(
                    """
                    SELECT thread_id, canvas_id, source, title, renderer, editable,
                           alt_text, presentation_revision, source_fingerprint,
                           source_version, origin_generation, created_at, updated_at
                    FROM canvases
                    WHERE thread_id = $1 AND canvas_id = $2
                    FOR UPDATE
                    """,
                    thread_id,
                    canvas_id,
                )
                current = CanvasRecord.from_row(row) if row is not None else None
                if expected_presentation_revision is not None and (
                    current is None
                    or current.presentation_revision != expected_presentation_revision
                ):
                    raise CanvasPreconditionFailed(
                        "Canvas presentation changed while the browser was starting"
                    )
                if (
                    current is not None
                    and current.source == source
                    and current.source_fingerprint == source_fingerprint
                    and current.title == presentation.title
                    and current.renderer == presentation.renderer
                    and current.editable is presentation.editable
                    and current.alt_text == presentation.alt_text
                    and current.source_version == presentation.source_version
                    and current.origin_generation is None
                ):
                    result = current
                else:
                    updated = await conn.fetchrow(
                        """
                        INSERT INTO canvases (
                            thread_id, canvas_id, source, title, renderer, editable,
                            alt_text, presentation_revision, source_fingerprint,
                            source_version, origin_generation
                        )
                        VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, 1, $8, $9, $10)
                        ON CONFLICT ON CONSTRAINT uq_canvases_thread_canvas
                        DO UPDATE SET
                            source = EXCLUDED.source,
                            title = EXCLUDED.title,
                            renderer = EXCLUDED.renderer,
                            editable = EXCLUDED.editable,
                            alt_text = EXCLUDED.alt_text,
                            presentation_revision = canvases.presentation_revision + 1,
                            source_fingerprint = EXCLUDED.source_fingerprint,
                            source_version = EXCLUDED.source_version,
                            origin_generation = EXCLUDED.origin_generation,
                            updated_at = now()
                        RETURNING thread_id, canvas_id, source, title, renderer,
                                  editable, alt_text, presentation_revision,
                                  source_fingerprint, source_version,
                                  origin_generation, created_at, updated_at
                        """,
                        thread_id,
                        canvas_id,
                        source_json,
                        presentation.title,
                        presentation.renderer,
                        presentation.editable,
                        presentation.alt_text,
                        source_fingerprint,
                        presentation.source_version,
                        None,
                    )
                    result = CanvasRecord.from_row(updated)
                    changed = True

        assert result is not None
        if changed:
            await self._emit(result, method="canvas.updated")
        return CanvasMutation(changed=changed, record=result)

    async def edit_file(
        self,
        thread_id: str,
        *,
        expected_presentation_revision: int,
        expected_source_fingerprint: str,
        expected_source_version: str,
        expected_thread_user_id: str,
        writer: CanvasFileWriter,
        canvas_id: str = MAIN_CANVAS_ID,
        lock_timeout: float | None = None,
    ) -> CanvasMutation:
        """Conditionally write an editable file and advance Canvas state once.

        The PostgreSQL session-level advisory lock serializes cooperating
        writers across replicas. The Canvas row lock is taken only after that
        coordinator and is held across the bounded workspace write/readback,
        preventing a concurrent clear or replacement from redirecting a save.
        """

        self._require_main(canvas_id)
        initial = await self.get(thread_id, canvas_id=canvas_id)
        initial_source = self._validate_edit_record(
            initial,
            expected_presentation_revision=expected_presentation_revision,
            expected_source_fingerprint=expected_source_fingerprint,
            expected_source_version=expected_source_version,
        )
        lock_key = canvas_file_lock_key(thread_id, initial_source.path)
        timeout = (
            float(os.getenv("CANVAS_EDIT_LOCK_TIMEOUT", "5"))
            if lock_timeout is None
            else lock_timeout
        )
        deadline = asyncio.get_running_loop().time() + max(0.05, timeout)
        updated_record: CanvasRecord | None = None

        coordinator_admitted = False
        try:
            try:
                await asyncio.wait_for(
                    _EDIT_COORDINATOR_SEMAPHORE.acquire(),
                    timeout=max(0.05, _EDIT_COORDINATOR_QUEUE_TIMEOUT),
                )
                coordinator_admitted = True
            except TimeoutError as exc:
                raise CanvasEditError(
                    503,
                    "canvas_edit_capacity_exhausted",
                    "Canvas edit capacity is currently exhausted",
                ) from exc

            async with self._db.acquire() as conn:
                acquired = False
                try:
                    while True:
                        acquired = bool(
                            await conn.fetchval(
                                "SELECT pg_try_advisory_lock($1)", lock_key
                            )
                        )
                        if acquired:
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            raise CanvasEditError(
                                423,
                                "canvas_edit_busy",
                                "Another Canvas save is currently in progress",
                            )
                        await asyncio.sleep(0.05)

                    async with conn.transaction():
                        thread_row = await conn.fetchrow(
                            """
                            SELECT id, user_id, status, execution_lane, metadata
                            FROM threads
                            WHERE id = $1
                            FOR SHARE
                            """,
                            thread_id,
                        )
                        if thread_row is None or str(
                            thread_row.get("user_id") or ""
                        ) != str(expected_thread_user_id):
                            raise CanvasEditError(
                                403,
                                "canvas_not_authorized",
                                "Canvas thread authorization changed",
                            )
                        _require_stateless_canvas_workspace_writer(thread_row)
                        row = await conn.fetchrow(
                            """
                            SELECT thread_id, canvas_id, source, title, renderer,
                                   editable, alt_text, presentation_revision,
                                   source_fingerprint, source_version,
                                   origin_generation, created_at, updated_at
                            FROM canvases
                            WHERE thread_id = $1 AND canvas_id = $2
                            FOR UPDATE
                            """,
                            thread_id,
                            canvas_id,
                        )
                        current = (
                            CanvasRecord.from_row(row) if row is not None else None
                        )
                        self._validate_edit_record(
                            current,
                            expected_presentation_revision=(
                                expected_presentation_revision
                            ),
                            expected_source_fingerprint=expected_source_fingerprint,
                            expected_source_version=expected_source_version,
                        )
                        assert current is not None

                        resulting_version = await writer(current, dict(thread_row))
                        if not isinstance(resulting_version, str) or not re.fullmatch(
                            _SHA256_PATTERN, resulting_version
                        ):
                            raise CanvasEditError(
                                500,
                                "canvas_write_failed",
                                "Canvas workspace write returned an invalid "
                                "content version",
                            )

                        updated = await conn.fetchrow(
                            """
                            UPDATE canvases
                            SET source_version = $3,
                                presentation_revision = presentation_revision + 1,
                                updated_at = now()
                            WHERE thread_id = $1 AND canvas_id = $2
                            RETURNING thread_id, canvas_id, source, title, renderer,
                                      editable, alt_text, presentation_revision,
                                      source_fingerprint, source_version,
                                      origin_generation, created_at, updated_at
                            """,
                            thread_id,
                            canvas_id,
                            resulting_version,
                        )
                        updated_record = CanvasRecord.from_row(updated)
                finally:
                    if acquired:
                        try:
                            released = await conn.fetchval(
                                "SELECT pg_advisory_unlock($1)", lock_key
                            )
                            if released is not True:
                                logger.error(
                                    "Canvas advisory lock was not held at release "
                                    "for thread=%s",
                                    thread_id,
                                )
                        except Exception:
                            # Returning a connection with a session lock would
                            # poison the pool. Terminate it if explicit unlock
                            # fails.
                            logger.exception(
                                "Canvas advisory lock release failed for thread=%s",
                                thread_id,
                            )
                            terminate = getattr(conn, "terminate", None)
                            if callable(terminate):
                                terminate()
        finally:
            if coordinator_admitted:
                _EDIT_COORDINATOR_SEMAPHORE.release()

        assert updated_record is not None
        await self._emit(updated_record, method="canvas.updated")
        return CanvasMutation(changed=True, record=updated_record)

    @staticmethod
    def _validate_edit_record(
        record: CanvasRecord | None,
        *,
        expected_presentation_revision: int,
        expected_source_fingerprint: str,
        expected_source_version: str,
    ) -> WorkspaceFileSource:
        """Apply conflict precedence before any ordinary hash mismatch."""

        if record is None or record.source is None:
            raise CanvasEditError(409, "canvas_cleared", "Canvas is cleared")
        if not isinstance(record.source, WorkspaceFileSource):
            raise CanvasEditError(409, "canvas_replaced", "Canvas source was replaced")
        if record.source_fingerprint != expected_source_fingerprint:
            raise CanvasEditError(409, "canvas_replaced", "Canvas source was replaced")
        if record.presentation_revision != expected_presentation_revision:
            raise CanvasEditError(
                409,
                "canvas_presentation_changed",
                "Canvas presentation changed; reload its current state",
            )
        if not record.editable:
            raise CanvasEditError(
                409,
                "canvas_not_editable",
                "The current Canvas presentation is not editable",
            )
        if record.renderer not in {
            "markdown",
            "text",
            "html",
            "html-interactive",
            "office",
        }:
            raise CanvasEditError(
                409,
                "canvas_not_editable",
                "The current Canvas renderer does not support editing",
            )
        if record.source_version != expected_source_version:
            raise CanvasEditError(
                412,
                "canvas_content_precondition_failed",
                "Canvas content changed; reload before saving",
            )
        return record.source

    async def refresh_file(
        self,
        thread_id: str,
        *,
        expected_etag: str | None,
        expected_thread_user_id: str,
        refresher: CanvasFileWriter,
        canvas_id: str = MAIN_CANVAS_ID,
        representation_builder: CanvasRepresentationBuilder = (
            build_public_canvas_representation
        ),
    ) -> CanvasMutation:
        """Adopt the current validated workspace bytes for a file Canvas."""

        self._require_main(canvas_id)
        refreshed_record: CanvasRecord | None = None
        async with _canvas_mutation_admission():
            async with self._db.acquire() as conn:
                async with conn.transaction():
                    thread_row = await conn.fetchrow(
                        """
                        SELECT id, user_id, status, execution_lane, metadata
                        FROM threads
                        WHERE id = $1
                        FOR SHARE
                        """,
                        thread_id,
                    )
                    if thread_row is None or str(
                        thread_row.get("user_id") or ""
                    ) != str(expected_thread_user_id):
                        raise CanvasEditError(
                            403,
                            "canvas_not_authorized",
                            "Canvas thread authorization changed",
                        )
                    _require_stateless_canvas_workspace_writer(thread_row)
                    row = await conn.fetchrow(
                        """
                        SELECT thread_id, canvas_id, source, title, renderer,
                               editable, alt_text, presentation_revision,
                               source_fingerprint, source_version,
                               origin_generation, created_at, updated_at
                        FROM canvases
                        WHERE thread_id = $1 AND canvas_id = $2
                        FOR UPDATE
                        """,
                        thread_id,
                        canvas_id,
                    )
                    if row is None or row.get("source") is None:
                        raise CanvasEditError(
                            409, "canvas_cleared", "Canvas is cleared"
                        )
                    current = CanvasRecord.from_row(row)
                    if not isinstance(current.source, WorkspaceFileSource):
                        raise CanvasEditError(
                            409,
                            "canvas_not_file",
                            "The current Canvas is not file-backed",
                        )
                    if expected_etag is None:
                        raise CanvasPreconditionRequired(
                            "If-Match is required to refresh a Canvas"
                        )
                    current_etag = representation_builder(current).etag
                    if not secrets.compare_digest(
                        strong_state_precondition(expected_etag), current_etag
                    ):
                        raise CanvasPreconditionFailed("Canvas state ETag is stale")

                    resulting_version = await refresher(current, dict(thread_row))
                    if not isinstance(resulting_version, str) or not re.fullmatch(
                        _SHA256_PATTERN, resulting_version
                    ):
                        raise CanvasEditError(
                            500,
                            "canvas_refresh_failed",
                            "Canvas refresh returned an invalid content version",
                        )
                    updated = await conn.fetchrow(
                        """
                        UPDATE canvases
                        SET source_version = $3,
                            presentation_revision = presentation_revision + 1,
                            updated_at = now()
                        WHERE thread_id = $1 AND canvas_id = $2
                        RETURNING thread_id, canvas_id, source, title, renderer,
                                  editable, alt_text, presentation_revision,
                                  source_fingerprint, source_version,
                                  origin_generation, created_at, updated_at
                        """,
                        thread_id,
                        canvas_id,
                        resulting_version,
                    )
                    refreshed_record = CanvasRecord.from_row(updated)

        assert refreshed_record is not None
        await self._emit(refreshed_record, method="canvas.updated")
        return CanvasMutation(changed=True, record=refreshed_record)

    async def repin_workspace_generation(
        self,
        thread_id: str,
        *,
        expected_source_version: str,
        verifier: "CanvasGenerationVerifier",
        canvas_id: str = MAIN_CANVAS_ID,
    ) -> CanvasMutation:
        """Re-bind a file Canvas to the workspace generation now current.

        A workspace that is rebuilt — from a PVC reattach or from an S3
        snapshot — always mints a new generation, because its SSH host key is
        pod-private. The pinned generation is then permanently stale and the
        Canvas reports `unavailable` forever, even though the file it names is
        still there and still identical. This repairs that.

        The predicate is byte identity, not workspace identity: ``verifier``
        returns the current generation only when the file at the pinned path
        hashes to the pinned ``source_version``. That preserves the property
        the generation pin exists for — a stale presentation can never serve
        *different* bytes under its old rendering context — by a stronger
        mechanism than trusting where the bytes came from. Live-app and browser
        sources are never re-pinned; a port on a rebuilt workspace is a
        genuinely different thing and no hash can say otherwise.

        Server-initiated repair, so there is no ``If-Match``: no client
        authored it. Callers must have already authorized the thread.

        The revision bump is mandatory, not cosmetic. ``source_fingerprint``
        covers the generation, so re-pinning necessarily changes it, and that
        fingerprint is embedded in every issued ``content_url``. Without the
        bump and its invalidation, mounted clients would be stranded on URLs
        the identity check now rejects.
        """

        self._require_main(canvas_id)
        repinned_record: CanvasRecord | None = None
        async with _canvas_mutation_admission():
            async with self._db.acquire() as conn:
                async with conn.transaction():
                    thread_row = await conn.fetchrow(
                        """
                        SELECT id, user_id, status, execution_lane, metadata
                        FROM threads
                        WHERE id = $1
                        FOR SHARE
                        """,
                        thread_id,
                    )
                    if thread_row is None:
                        return CanvasMutation(changed=False, record=None)
                    _require_stateless_canvas_workspace_writer(thread_row)
                    row = await conn.fetchrow(
                        """
                        SELECT thread_id, canvas_id, source, title, renderer,
                               editable, alt_text, presentation_revision,
                               source_fingerprint, source_version,
                               origin_generation, created_at, updated_at
                        FROM canvases
                        WHERE thread_id = $1 AND canvas_id = $2
                        FOR UPDATE
                        """,
                        thread_id,
                        canvas_id,
                    )
                    if row is None:
                        return CanvasMutation(changed=False, record=None)
                    current = CanvasRecord.from_row(row)
                    source = current.source
                    if not isinstance(source, WorkspaceFileSource):
                        return CanvasMutation(changed=False, record=current)
                    # The presentation moved while this read was in flight.
                    # Whatever is there now is somebody else's to repair.
                    if current.source_version != expected_source_version:
                        return CanvasMutation(changed=False, record=current)

                    generation = await verifier(current, dict(thread_row))
                    if generation is None or generation == source.workspace_generation:
                        # Either the bytes differ (an honest `source_changed`,
                        # not something to paper over) or a concurrent request
                        # already re-pinned this row. Exactly one bump either
                        # way.
                        return CanvasMutation(changed=False, record=current)

                    repinned_source = WorkspaceFileSource(
                        path=source.path, workspace_generation=generation
                    )
                    source_json = json.dumps(
                        repinned_source.model_dump(mode="json"),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    updated = await conn.fetchrow(
                        """
                        UPDATE canvases
                        SET source = $3::jsonb,
                            source_fingerprint = $4,
                            presentation_revision = presentation_revision + 1,
                            updated_at = now()
                        WHERE thread_id = $1 AND canvas_id = $2
                        RETURNING thread_id, canvas_id, source, title, renderer,
                                  editable, alt_text, presentation_revision,
                                  source_fingerprint, source_version,
                                  origin_generation, created_at, updated_at
                        """,
                        thread_id,
                        canvas_id,
                        source_json,
                        canonical_source_fingerprint(repinned_source),
                    )
                    repinned_record = CanvasRecord.from_row(updated)

        assert repinned_record is not None
        await self._emit(repinned_record, method="canvas.updated")
        return CanvasMutation(changed=True, record=repinned_record)

    async def reset_origin(
        self,
        thread_id: str,
        *,
        expected_etag: str | None,
        expected_thread_user_id: str,
        canvas_id: str = MAIN_CANVAS_ID,
        representation_builder: CanvasRepresentationBuilder = (
            build_public_canvas_representation
        ),
    ) -> CanvasMutation:
        """Rotate one live application's isolated browser origin.

        The source pointer is preserved, while the origin generation and
        presentation revision advance atomically.  Migration ``0061`` revokes
        sessions for the retired generation in the same transaction and emits
        the cross-replica cancellation notification.
        """

        self._require_main(canvas_id)
        reset_record: CanvasRecord | None = None
        async with _canvas_mutation_admission():
            async with self._db.acquire() as conn:
                async with conn.transaction():
                    thread_row = await conn.fetchrow(
                        """
                        SELECT id, user_id, metadata
                        FROM threads
                        WHERE id = $1
                        FOR SHARE
                        """,
                        thread_id,
                    )
                    if thread_row is None or str(
                        thread_row.get("user_id") or ""
                    ) != str(expected_thread_user_id):
                        raise CanvasEditError(
                            403,
                            "canvas_not_authorized",
                            "Canvas thread authorization changed",
                        )

                    row = await conn.fetchrow(
                        """
                        SELECT thread_id, canvas_id, source, title, renderer,
                               editable, alt_text, presentation_revision,
                               source_fingerprint, source_version,
                               origin_generation, created_at, updated_at
                        FROM canvases
                        WHERE thread_id = $1 AND canvas_id = $2
                        FOR UPDATE
                        """,
                        thread_id,
                        canvas_id,
                    )
                    if row is None or row.get("source") is None:
                        raise CanvasEditError(
                            409, "canvas_cleared", "Canvas is cleared"
                        )
                    current = CanvasRecord.from_row(row)
                    if not isinstance(current.source, WorkspaceAppSource):
                        raise CanvasEditError(
                            409,
                            "canvas_not_live_app",
                            "The current Canvas is not a live workspace application",
                        )
                    if expected_etag is None:
                        raise CanvasPreconditionRequired(
                            "If-Match is required to reset a Canvas origin"
                        )
                    current_etag = representation_builder(current).etag
                    if not secrets.compare_digest(
                        strong_state_precondition(expected_etag), current_etag
                    ):
                        raise CanvasPreconditionFailed("Canvas state ETag is stale")

                    updated = await conn.fetchrow(
                        """
                        UPDATE canvases
                        SET origin_generation = $3,
                            presentation_revision = presentation_revision + 1,
                            updated_at = now()
                        WHERE thread_id = $1 AND canvas_id = $2
                        RETURNING thread_id, canvas_id, source, title, renderer,
                                  editable, alt_text, presentation_revision,
                                  source_fingerprint, source_version,
                                  origin_generation, created_at, updated_at
                        """,
                        thread_id,
                        canvas_id,
                        uuid4(),
                    )
                    reset_record = CanvasRecord.from_row(updated)

        assert reset_record is not None
        await self._emit(reset_record, method="canvas.updated")
        return CanvasMutation(changed=True, record=reset_record)

    async def clear(
        self,
        thread_id: str,
        *,
        expected_etag: str | None,
        canvas_id: str = MAIN_CANVAS_ID,
        representation_builder: CanvasRepresentationBuilder = (
            build_public_canvas_representation
        ),
        require_precondition: bool = True,
    ) -> CanvasMutation:
        """Conditionally clear a populated presentation.

        An absent or already-cleared state is an idempotent no-op and does not
        require a precondition.  A populated state is compared while its row is
        locked, so a stale browser cannot clear a source replaced between GET
        and DELETE.
        """

        self._require_main(canvas_id)
        cleared_record: CanvasRecord | None = None
        freed_object_key: str | None = None
        async with self._db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT thread_id, canvas_id, source, title, renderer, editable,
                           alt_text, presentation_revision, source_fingerprint,
                           source_version, origin_generation, created_at, updated_at
                    FROM canvases
                    WHERE thread_id = $1 AND canvas_id = $2
                    FOR UPDATE
                    """,
                    thread_id,
                    canvas_id,
                )
                if row is None:
                    return CanvasMutation(changed=False, record=None)

                current = CanvasRecord.from_row(row)
                if current.source is None:
                    return CanvasMutation(changed=False, record=current)

                if expected_etag is None and require_precondition:
                    raise CanvasPreconditionRequired(
                        "If-Match is required to clear a populated Canvas"
                    )
                if require_precondition:
                    assert expected_etag is not None
                    current_etag = representation_builder(current).etag
                    if not secrets.compare_digest(
                        strong_state_precondition(expected_etag), current_etag
                    ):
                        raise CanvasPreconditionFailed("Canvas state ETag is stale")

                cleared_row = await conn.fetchrow(
                    """
                    UPDATE canvases
                    SET source = NULL,
                        title = NULL,
                        renderer = 'auto',
                        editable = FALSE,
                        alt_text = NULL,
                        presentation_revision = presentation_revision + 1,
                        source_fingerprint = NULL,
                        source_version = NULL,
                        origin_generation = NULL,
                        updated_at = now()
                    WHERE thread_id = $1 AND canvas_id = $2
                    RETURNING thread_id, canvas_id, source, title, renderer,
                              editable, alt_text, presentation_revision,
                              source_fingerprint, source_version,
                              origin_generation, created_at, updated_at
                    """,
                    thread_id,
                    canvas_id,
                )
                cleared_record = CanvasRecord.from_row(cleared_row)

                # Clearing the stage is the user's delete affordance for the
                # durable copy. It rides this transaction so a clear can never
                # commit while the remembered bytes stay addressable.
                if self._snapshot_store is not None:
                    freed_object_key = await self._snapshot_store.delete_row(
                        conn, thread_id, canvas_id=canvas_id
                    )

        # Object storage cannot join the transaction, so the blob delete
        # follows the commit. A failure here leaks one bounded object; it never
        # resurrects a cleared presentation.
        if self._snapshot_store is not None and freed_object_key:
            await self._snapshot_store.discard_object(freed_object_key)

        # The callback is deliberately after transaction exit.  Event delivery
        # is not part of state commit; REST remains authoritative.
        await self._emit(cleared_record, method="canvas.cleared")
        return CanvasMutation(changed=True, record=cleared_record)

    async def _emit(
        self,
        record: CanvasRecord,
        *,
        method: Literal["canvas.updated", "canvas.cleared"],
    ) -> None:
        if self._event_callback is None:
            return
        event = canvas_invalidation(record, method=method)
        try:
            result = self._event_callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # State is already committed.  The ordered event writer/caller logs
            # its own terminal reconciliation signal; do not turn a durable
            # successful mutation into a misleading 500/retry.
            logger.exception(
                "Canvas invalidation callback failed for thread=%s revision=%d",
                record.thread_id,
                record.presentation_revision,
            )


__all__ = [
    "BrowserSource",
    "CanvasCapabilities",
    "CanvasEditError",
    "CanvasError",
    "CanvasInvalidation",
    "CanvasMutation",
    "CanvasPreconditionFailed",
    "CanvasPreconditionRequired",
    "CanvasPublicState",
    "CanvasRecord",
    "CanvasRepresentation",
    "CanvasService",
    "CanvasSetInput",
    "MAIN_CANVAS_ID",
    "WorkspaceAppRoute",
    "WorkspaceAppSource",
    "WorkspaceFileSource",
    "build_public_canvas_representation",
    "canvas_file_lock_key",
    "canvas_presentation_lock_key",
    "canonical_source_fingerprint",
    "canvas_invalidation",
]
