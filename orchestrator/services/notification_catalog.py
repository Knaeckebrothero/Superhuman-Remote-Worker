"""Pure catalog for the unified notification system.

Everything here is data or pure functions: the category → (severity, steps,
actions) table, the severity classes, deterministic id minting, row
serialisation, and the three registries ``main.py`` fills at startup (action
handlers, source loaders, source probes). No I/O and no ``main`` imports, so
``main.py`` can register into it without an import cycle and tests can use it
without a database.

Design: knowledge-base/knowledge/features/unified_notification_system.md —
D1 (callers ``record``, never ``send``), D5 (workflow-as-data), D7 (typed
actions, server-side handlers), D8 (severity classes), D11 (recipients are
parties). Shapes deliberately mirror Knock's workflow/step/message model (D12)
so a later buy is a mapping exercise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

NAMESPACE = uuid.NAMESPACE_URL
ID_PREFIX = "srw-notification-v1"

RECIPIENT_KINDS: tuple[str, ...] = ("user", "officer")
SEVERITIES: tuple[str, ...] = ("low", "normal", "high", "critical")
SEVERITY_RANK: dict[str, int] = {"low": 0, "normal": 1, "high": 2, "critical": 3}
CHANNELS: tuple[str, ...] = (
    "in_app",
    "email",
    "ntfy",
    "slack_webhook",
    "discord_webhook",
    "push",
)
WEBHOOK_CHANNELS: tuple[str, ...] = ("ntfy", "slack_webhook", "discord_webhook")
ACTION_STYLES: tuple[str, ...] = ("default", "primary", "danger")
ACTION_INPUTS: tuple[str | None, ...] = (None, "text", "textarea")


def notification_id(
    recipient_kind: str, recipient_id: str, dedup_key: str
) -> uuid.UUID:
    """Deterministic feed-row id: the same (recipient, dedup_key) always maps
    to the same uuid, which is what makes ``record()`` idempotent under
    journal replay and dual-leader windows. Postgres can reproduce it with
    ``uuid_generate_v5(uuid_ns_url(), 'srw-notification-v1:…')`` for backfills."""
    return uuid.uuid5(
        NAMESPACE, f"{ID_PREFIX}:{recipient_kind}:{recipient_id}:{dedup_key}"
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One server-declared action on a notification.

    The cockpit renders it from ``label_key``/``style`` and, for ``input``
    actions, collects ``params[input_name]`` before POSTing
    ``{action_type, params}`` back. It never learns what the action *does* —
    that is the registered handler's business (D7).
    """

    type: str
    label_key: str
    style: str = "default"
    input: str | None = None
    input_name: str | None = None

    def __post_init__(self) -> None:
        if self.style not in ACTION_STYLES:
            raise ValueError(f"action {self.type}: unknown style {self.style!r}")
        if self.input not in ACTION_INPUTS:
            raise ValueError(f"action {self.type}: unknown input {self.input!r}")
        if (self.input is None) != (self.input_name is None):
            raise ValueError(f"action {self.type}: input and input_name go together")
        if not self.label_key.startswith("notifications.actions."):
            raise ValueError(
                f"action {self.type}: label_key must be a notifications.actions.* key"
            )

    def serialize(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": self.type,
            "label_key": self.label_key,
            "style": self.style,
            "input": self.input,
            "input_name": self.input_name,
            "params": dict(params or {}),
        }


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One channel step of a category's workflow (Knock: delay + channel).

    ``delay`` is minutes, or the symbolic ``'officer_response_minutes'`` which
    slice 2 resolves per project. A delay is not its own row — it becomes the
    ``due_at`` of this step. Zero-delay steps run inline in ``record()``.
    """

    channel: str
    delay: int | str = 0
    conditions: tuple[str, ...] = ()
    batch_key: str | None = None
    batch_window_minutes: int | None = None

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS or self.channel == "in_app":
            raise ValueError(f"step: unknown channel {self.channel!r}")

    @property
    def immediate(self) -> bool:
        return self.delay == 0


@dataclass(frozen=True, slots=True)
class CategorySpec:
    """What a category means: its default severity, its actions, and — unless
    overridden — the severity class that decides its channel steps."""

    name: str
    severity: str
    actions: tuple[ActionSpec, ...] = ()
    steps: tuple[StepSpec, ...] | None = None
    bypass_quiet_hours: bool | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"category {self.name}: unknown severity {self.severity!r}"
            )
        types = [a.type for a in self.actions]
        if len(types) != len(set(types)):
            raise ValueError(f"category {self.name}: duplicate action types")


def _immediate(*channels: str) -> tuple[StepSpec, ...]:
    return tuple(StepSpec(channel) for channel in channels)


# Slice 1 severity classes: behaviour parity — every class that mails today
# still mails immediately. Slice 2 replaces this with delay/condition/batch
# steps (`normal` waits for seen/resolved; `low` rides a daily digest).
SEVERITY_CLASSES_V1: dict[str, tuple[StepSpec, ...]] = {
    "critical": _immediate("email", "ntfy", "slack_webhook", "discord_webhook"),
    "high": _immediate("email", "ntfy", "slack_webhook", "discord_webhook"),
    "normal": _immediate("email", "ntfy", "slack_webhook", "discord_webhook"),
    "low": (),
}
SEVERITY_CLASSES: dict[str, tuple[StepSpec, ...]] = SEVERITY_CLASSES_V1


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

A = "notifications.actions."
ACTION_APPROVE = ActionSpec("approve", A + "approve", "primary")
ACTION_RESUME_WITH_FEEDBACK = ActionSpec(
    "resume", A + "resumeWithFeedback", "default", "textarea", "feedback"
)
ACTION_RESUME = ActionSpec("resume", A + "resume", "primary", "textarea", "feedback")
ACTION_DENY = ActionSpec("deny", A + "deny", "danger", "text", "reason")
ACTION_UPGRADE_TO_VM = ActionSpec("approve_upgrade", A + "upgradeToVm", "primary")
ACTION_RESUME_WITHOUT_VM = ActionSpec("resume_without_vm", A + "resumeWithoutVm")
ACTION_REPLY = ActionSpec("reply", A + "reply", "primary", "textarea", "message")
ACTION_OPEN_CONFERENCE = ActionSpec("open_conference", A + "openConference")
ACTION_OPEN_JOB = ActionSpec("open", A + "openJob")
ACTION_OPEN_THREAD = ActionSpec("open", A + "openThread")
ACTION_OPEN_SESSION = ActionSpec("open_session", A + "openSession")
ACTION_OPEN_PROJECT = ActionSpec("open", A + "openProject")
ACTION_OPEN_AUTOMATIONS = ActionSpec("open", A + "openAutomations")
ACTION_OPEN_ADMIN_USERS = ActionSpec("open", A + "openAdminUsers")


# ---------------------------------------------------------------------------
# Categories (slice 1; later slices append)
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, CategorySpec] = {
    spec.name: spec
    for spec in (
        CategorySpec(
            "review_queue",
            "normal",
            (ACTION_APPROVE, ACTION_RESUME_WITH_FEEDBACK, ACTION_OPEN_JOB),
        ),
        CategorySpec(
            "vm_upgrade",
            "high",
            (ACTION_UPGRADE_TO_VM, ACTION_RESUME_WITHOUT_VM, ACTION_DENY),
        ),
        CategorySpec("budget_exceeded", "high", (ACTION_RESUME, ACTION_OPEN_JOB)),
        CategorySpec("incident", "critical", (ACTION_OPEN_JOB,)),
        CategorySpec(
            "officer_question", "high", (ACTION_REPLY, ACTION_OPEN_CONFERENCE)
        ),
        CategorySpec("officer_runtime", "high", (ACTION_OPEN_CONFERENCE,)),
    )
}


def category_spec(name: str) -> CategorySpec:
    """Loud on an unknown category — a typo must never silently become a
    notification that reaches nobody."""
    try:
        return CATEGORIES[name]
    except KeyError:
        raise ValueError(f"unknown notification category {name!r}") from None


def normalize_severity(spec: CategorySpec, severity: str | None) -> str:
    resolved = severity or spec.severity
    if resolved not in SEVERITIES:
        raise ValueError(f"unknown severity {resolved!r}")
    return resolved


def steps_for(spec: CategorySpec, severity: str) -> tuple[StepSpec, ...]:
    if spec.steps is not None:
        return spec.steps
    return SEVERITY_CLASSES[severity]


def bypasses_quiet_hours(spec: CategorySpec, severity: str) -> bool:
    if spec.bypass_quiet_hours is not None:
        return spec.bypass_quiet_hours
    return severity == "critical"


def serialize_actions(
    spec: CategorySpec, action_params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The declared action set for a new row, with the call's params (job id,
    request id, …) merged into every action so the cockpit can POST them back."""
    return [action.serialize(action_params) for action in spec.actions]


# ---------------------------------------------------------------------------
# Row serialisation (one shape for the API and the SSE frame)
# ---------------------------------------------------------------------------

_TS_FIELDS = (
    "created_at",
    "seen_at",
    "read_at",
    "interacted_at",
    "resolved_at",
    "archived_at",
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def serialize_notification(row: dict[str, Any]) -> dict[str, Any]:
    """DB row → the wire shape (`Notification` in the cockpit)."""
    source_kind = row.get("source_kind")
    return {
        "id": str(row["id"]),
        "category": row.get("category"),
        "severity": row.get("severity"),
        "subject": row.get("subject") or "",
        "body": row.get("body") or "",
        "source_ref": (
            {"kind": source_kind, "id": str(row.get("source_id"))}
            if source_kind
            else None
        ),
        "actions": list(row.get("actions") or []),
        "payload": dict(row.get("payload") or {}),
        "resolved_by": row.get("resolved_by"),
        **{name: _iso(row.get(name)) for name in _TS_FIELDS},
    }


def cursor_for(row: dict[str, Any]) -> str:
    """Keyset cursor: ``{created_at_iso}|{id}``."""
    return f"{_iso(row['created_at'])}|{row['id']}"


# ---------------------------------------------------------------------------
# Registries — filled by main.py at startup
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ActionContext:
    notification: dict[str, Any]
    user: dict[str, Any]
    params: dict[str, Any] = field(default_factory=dict)
    db: Any = None


@dataclass(slots=True)
class ActionResult:
    result: dict[str, Any] = field(default_factory=dict)
    # For source-less categories (an officer question has no job to probe),
    # the handler resolves the row itself.
    resolve: bool = False
    resolved_by: str | None = None


ActionHandler = Callable[[ActionContext], Awaitable[ActionResult]]
SourceLoader = Callable[[Any, str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
SourceProbe = Callable[[Any, str], Awaitable[bool]]

_ACTION_HANDLERS: dict[tuple[str, str], ActionHandler] = {}
_SOURCE_LOADERS: dict[str, SourceLoader] = {}
_SOURCE_PROBES: dict[str, SourceProbe] = {}


def register_action(
    category: str, action_type: str
) -> Callable[[ActionHandler], ActionHandler]:
    """Bind a server-side effect to ``(category, action_type)``. The action must
    be declared on the category, so a handler can never exist for an action the
    cockpit could not have been offered."""
    spec = category_spec(category)
    if action_type not in {a.type for a in spec.actions}:
        raise ValueError(f"category {category!r} declares no action {action_type!r}")

    def decorator(handler: ActionHandler) -> ActionHandler:
        _ACTION_HANDLERS[(category, action_type)] = handler
        return handler

    return decorator


def action_handler(category: str, action_type: str) -> ActionHandler | None:
    return _ACTION_HANDLERS.get((category, action_type))


def register_source_loader(source_kind: str) -> Callable[[SourceLoader], SourceLoader]:
    def decorator(loader: SourceLoader) -> SourceLoader:
        _SOURCE_LOADERS[source_kind] = loader
        return loader

    return decorator


def source_loader(source_kind: str | None) -> SourceLoader | None:
    return _SOURCE_LOADERS.get(source_kind) if source_kind else None


def register_source_probe(source_kind: str) -> Callable[[SourceProbe], SourceProbe]:
    def decorator(probe: SourceProbe) -> SourceProbe:
        _SOURCE_PROBES[source_kind] = probe
        return probe

    return decorator


def source_probe(source_kind: str | None) -> SourceProbe | None:
    return _SOURCE_PROBES.get(source_kind) if source_kind else None


def clear_registries() -> None:
    """Test isolation only."""
    _ACTION_HANDLERS.clear()
    _SOURCE_LOADERS.clear()
    _SOURCE_PROBES.clear()


__all__ = [
    "ACTION_STYLES",
    "CATEGORIES",
    "CHANNELS",
    "RECIPIENT_KINDS",
    "SEVERITIES",
    "SEVERITY_CLASSES",
    "SEVERITY_CLASSES_V1",
    "SEVERITY_RANK",
    "WEBHOOK_CHANNELS",
    "ActionContext",
    "ActionHandler",
    "ActionResult",
    "ActionSpec",
    "CategorySpec",
    "SourceLoader",
    "SourceProbe",
    "StepSpec",
    "action_handler",
    "bypasses_quiet_hours",
    "category_spec",
    "clear_registries",
    "cursor_for",
    "normalize_severity",
    "notification_id",
    "register_action",
    "register_source_loader",
    "register_source_probe",
    "serialize_actions",
    "serialize_notification",
    "source_loader",
    "source_probe",
    "steps_for",
]
