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

import hashlib
import inspect
import json
import logging
import secrets
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

logger = logging.getLogger(__name__)

MAIN_CANVAS_ID = "main"
SOURCE_FINGERPRINT_SCHEMA_VERSION = 1
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"

CanvasRenderer = Literal["auto", "markdown", "text", "html", "image"]
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
        return self


class CanvasCapabilities(_StrictFrozenModel):
    can_edit: bool = False
    can_pop_out: bool = False
    can_take_control: bool = False


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


class CanvasService:
    """Atomic persistence actions for the one v1 Canvas slot per thread."""

    def __init__(
        self,
        db: Any,
        *,
        event_callback: CanvasEventCallback | None = None,
    ) -> None:
        self._db = db
        self._event_callback = event_callback

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

    async def clear(
        self,
        thread_id: str,
        *,
        expected_etag: str | None,
        canvas_id: str = MAIN_CANVAS_ID,
        representation_builder: CanvasRepresentationBuilder = (
            build_public_canvas_representation
        ),
    ) -> CanvasMutation:
        """Conditionally clear a populated presentation.

        An absent or already-cleared state is an idempotent no-op and does not
        require a precondition.  A populated state is compared while its row is
        locked, so a stale browser cannot clear a source replaced between GET
        and DELETE.
        """

        self._require_main(canvas_id)
        cleared_record: CanvasRecord | None = None
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

                if expected_etag is None:
                    raise CanvasPreconditionRequired(
                        "If-Match is required to clear a populated Canvas"
                    )
                current_etag = representation_builder(current).etag
                if not secrets.compare_digest(expected_etag.strip(), current_etag):
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
    "canonical_source_fingerprint",
    "canvas_invalidation",
]
