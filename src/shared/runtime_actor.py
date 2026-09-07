"""Trusted runtime actor context shared by orchestrator and agent runtimes.

The public tool schemas deliberately carry none of these fields.  The
orchestrator derives the identity at a dispatch/attach boundary and delivers
an opaque credential alongside the ordinary runtime payload.  Callers may
forward that credential, but cannot choose the identity it names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Final, Mapping


RUNTIME_ACTOR_HEADER: Final = "X-SRW-Runtime-Actor"
RUNTIME_ACTOR_REFRESH_HEADER: Final = "X-SRW-Runtime-Actor-Refresh"
RUNTIME_ACTOR_BOOTSTRAP_HEADER: Final = "X-SRW-Runtime-Actor-Bootstrap"
# Hidden transport assertion used by the persistent loop's pre-provider
# maintenance callback. It selects no identity or authority: the opaque
# refresh bearer plus server-side Post/thread/agent/grant locks still do that.
RUNTIME_ACTOR_MAINTENANCE_PHASE_HEADER: Final = "X-SRW-Runtime-Actor-Maintenance-Phase"
RUNTIME_ACTOR_MAINTENANCE_PHASE_PRE_TURN: Final = "pre-turn"

# BP-09 Legate decision point.  This is the complete default human matrix for
# dispatch-authorizing machine tags AND charter writes.  Conference runtimes
# use their authenticated human role through this same table.  ``editor`` is
# intentionally denied until the Legate explicitly chooses otherwise; changing
# that policy is one edit here, not a search across write paths.
SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "admin": True,
        "owner": True,
        "editor": False,
        "viewer": False,
    }
)

RUNTIME_ACTOR_KINDS: Final = frozenset({"worker", "human", "conference", "officer"})
PROJECT_ROLES: Final = frozenset({"admin", "owner", "editor", "viewer"})


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class RuntimeActorContext:
    """Server-derived identity plus opaque, non-model-visible credentials."""

    caller_kind: str
    project_id: str | None = None
    project_role: str | None = None
    thread_id: str | None = None
    officer_incarnation: int | None = None
    user_id: str | None = None
    access_credential: str | None = None
    refresh_credential: str | None = None
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.caller_kind not in RUNTIME_ACTOR_KINDS:
            raise ValueError(f"invalid runtime actor kind: {self.caller_kind!r}")
        if self.project_role is not None and self.project_role not in PROJECT_ROLES:
            raise ValueError(
                f"invalid runtime actor project role: {self.project_role!r}"
            )
        if self.officer_incarnation is not None:
            self.officer_incarnation = int(self.officer_incarnation)
            if self.officer_incarnation < 0:
                raise ValueError("officer incarnation must not be negative")

    @classmethod
    def from_payload(cls, payload: Any) -> "RuntimeActorContext | None":
        """Parse an orchestrator-delivered payload; malformed input fails closed."""

        if payload is None:
            return None
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                caller_kind=str(payload["caller_kind"]),
                project_id=_optional_text(payload.get("project_id")),
                project_role=_optional_text(payload.get("project_role")),
                thread_id=_optional_text(payload.get("thread_id")),
                officer_incarnation=payload.get("officer_incarnation"),
                user_id=_optional_text(payload.get("user_id")),
                access_credential=_optional_text(payload.get("access_credential")),
                refresh_credential=_optional_text(payload.get("refresh_credential")),
                access_expires_at=_parse_timestamp(payload.get("access_expires_at")),
                refresh_expires_at=_parse_timestamp(payload.get("refresh_expires_at")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_payload(self, *, include_credentials: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "caller_kind": self.caller_kind,
            "project_id": self.project_id,
            "project_role": self.project_role,
            "thread_id": self.thread_id,
            "officer_incarnation": self.officer_incarnation,
            "user_id": self.user_id,
        }
        if include_credentials:
            payload.update(
                {
                    "access_credential": self.access_credential,
                    "refresh_credential": self.refresh_credential,
                    "access_expires_at": self.access_expires_at.isoformat()
                    if self.access_expires_at
                    else None,
                    "refresh_expires_at": self.refresh_expires_at.isoformat()
                    if self.refresh_expires_at
                    else None,
                }
            )
        return payload

    def audit_payload(self) -> dict[str, Any]:
        """Redacted identity suitable for tool results and security events."""

        return self.to_payload(include_credentials=False)

    def access_needs_refresh(self, *, skew_seconds: int = 30) -> bool:
        if not self.access_credential or self.access_expires_at is None:
            return True
        now = datetime.now(timezone.utc)
        return (self.access_expires_at - now).total_seconds() <= skew_seconds

    def refresh_needs_renewal(self, *, skew_seconds: int = 21600) -> bool:
        """Whether liveness maintenance should run before the idle wall.

        This is runtime scheduling only. The server remains authoritative and
        re-derives the Post/thread/agent binding on every Officer renewal.
        """

        if not self.refresh_credential or self.refresh_expires_at is None:
            return True
        now = datetime.now(timezone.utc)
        return (self.refresh_expires_at - now).total_seconds() <= skew_seconds

    def apply_refreshed_payload(self, payload: Any) -> bool:
        refreshed = self.from_payload(payload)
        if refreshed is None:
            return False
        # A refresh may replace credentials and expiries, never identity.
        if refreshed.audit_payload() != self.audit_payload():
            return False
        self.access_credential = refreshed.access_credential
        self.access_expires_at = refreshed.access_expires_at
        if refreshed.refresh_credential:
            self.refresh_credential = refreshed.refresh_credential
        if refreshed.refresh_expires_at:
            self.refresh_expires_at = refreshed.refresh_expires_at
        return bool(self.access_credential and self.access_expires_at)


def _audit_actor_identity(actor: Any) -> dict[str, Any] | None:
    if isinstance(actor, RuntimeActorContext):
        actor = actor.audit_payload()
    if not isinstance(actor, dict):
        return None
    # Validate identity leaves before construction: the transport parser's
    # permissive str()/int() coercion can serialize nested opaque values. Do
    # not parse credentials or expiry fields merely to discard them afterward.
    identity = {
        name: actor.get(name)
        for name in (
            "caller_kind",
            "project_id",
            "project_role",
            "thread_id",
            "user_id",
        )
    }
    if any(
        value is not None and not isinstance(value, str) for value in identity.values()
    ):
        return None
    incarnation = actor.get("officer_incarnation")
    if incarnation is not None and (type(incarnation) is not int or incarnation < 0):
        return None
    identity["officer_incarnation"] = incarnation
    try:
        return RuntimeActorContext(**identity).audit_payload()
    except (TypeError, ValueError):
        return None


def audit_metadata_payload(metadata: Any) -> dict[str, Any] | None:
    """Project structured audit metadata without runtime bearer credentials.

    Reuse the actor's existing identity-only audit contract. Unsupported actor
    shapes become null; unsupported metadata shapes are omitted. No recursive
    payload/text scrubbing occurs, and live metadata/actor objects are untouched.
    """
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return None
    if "runtime_actor" not in metadata or metadata["runtime_actor"] is None:
        return metadata
    identity = _audit_actor_identity(metadata["runtime_actor"])
    if metadata["runtime_actor"] == identity:
        return metadata
    return {**metadata, "runtime_actor": identity}


def redact_audit_payload_metadata(payload: Any) -> Any:
    """Project only the top-level metadata slot that shadows audit columns."""
    if not isinstance(payload, dict) or "metadata" not in payload:
        return payload
    metadata = audit_metadata_payload(payload["metadata"])
    if payload["metadata"] is metadata:
        return payload
    return {**payload, "metadata": metadata}


@dataclass(frozen=True, slots=True)
class RuntimeAuthorizationResult:
    """Explicit authorization outcome returned before any sensitive mutation."""

    authorized: bool
    code: str
    action: str
    actor: Mapping[str, Any]
    message: str

    def __bool__(self) -> bool:
        return self.authorized

    def tool_message(self) -> str:
        actor_kind = str(self.actor.get("caller_kind") or "unresolved")
        project = str(self.actor.get("project_id") or "none")
        return (
            f"Authorization denied [{self.code}] for {self.action}; "
            f"actor={actor_kind}, project={project}. No changes were made. "
            f"{self.message}"
        )
