"""Structured logging for the orchestrator.

Slice 0 of ``docs/features/centralized_logging.md``: emit JSON logs carrying
correlation ids (request_id / job_id / thread_id / agent_id) with secret
redaction, so the future log-aggregation pipeline (Grafana Alloy -> Loki) can
index and query them — and so ``kubectl logs`` is greppable by ``job_id`` today.

``LOG_FORMAT=json`` selects structured output (set in the cluster ConfigMap);
the default is ``text`` so local/dev terminals and tests stay readable.

This module is intentionally self-contained — it shares no code with the
agent's ``src/core/logging_config.py`` because the orchestrator and agent ship
as separate images with no common import path. The two ``redact()``
implementations must stay in sync; both are covered by tests
(``tests/test_logging_config.py``).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

COMPONENT = "orchestrator"

# ---------------------------------------------------------------------------
# Correlation context (propagates across await/tasks via contextvars)
# ---------------------------------------------------------------------------

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "srw_log_context", default={}
)

# Promoted to stable top-level keys, in this order, when present.
_CORRELATION_KEYS = ("request_id", "job_id", "thread_id", "agent_id", "phase")


def bind_log_context(**fields: Any) -> contextvars.Token:
    """Merge ``fields`` into the current logging context; return a reset token.

    ``None`` and empty-string values are skipped so callers can pass optional
    ids unconditionally (k8s often sets unused env vars to "" rather than unset).
    """
    merged = dict(_log_context.get())
    for key, value in fields.items():
        if value not in (None, ""):
            merged[key] = value
    return _log_context.set(merged)


def reset_log_context(token: contextvars.Token) -> None:
    """Restore the context saved by the matching :func:`bind_log_context`."""
    try:
        _log_context.reset(token)
    except (ValueError, LookupError):
        # Token created in a different context (crossed task boundary) — best effort.
        pass


def set_log_context(**fields: Any) -> None:
    """Bind fields without a reset token — for process-global ids (e.g. agent_id)."""
    bind_log_context(**fields)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Scope correlation fields to a ``with`` block."""
    token = bind_log_context(**fields)
    try:
        yield
    finally:
        reset_log_context(token)


# ---------------------------------------------------------------------------
# Secret redaction (defense-in-depth — code should never log raw secrets)
# ---------------------------------------------------------------------------

_REDACTED = "***REDACTED***"

# key: <value> / key=<value> for secret-ish key names.
_KV_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|secret|client[_-]?secret|password|passwd|"
    r"token|access[_-]?key|private[_-]?key|refresh[_-]?token)"
    r"(\"?\s*[:=]\s*\"?)"
    r"([^\s\"',}{)]+)"
)
# Standalone secret-shaped tokens.
_STANDALONE = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),  # bearer <opaque/jwt>
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # OpenAI / Anthropic style
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),  # Slack
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub
]


def redact(text: str) -> str:
    """Mask secret-shaped substrings. Introduces no quotes/newlines, so the
    result stays valid inside a JSON string."""
    if not text:
        return text
    text = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    for pattern in _STANDALONE:
        text = pattern.sub(_REDACTED, text)
    return text


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

# Intrinsic LogRecord attributes; anything else on the record is treated as
# structured ``extra=`` and included in the JSON output.
_RESERVED = set(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn passes an ANSI-laden duplicate of the message
}


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line, with correlation context and redaction."""

    def __init__(self, component: str = COMPONENT) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        rec: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "component": self.component,
            "message": redact(record.getMessage()),
            "file": f"{record.filename}:{record.lineno}",
        }
        ctx = _log_context.get()
        for key in _CORRELATION_KEYS:
            if key in ctx:
                rec[key] = ctx[key]
        for key, value in ctx.items():
            rec.setdefault(key, value)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in rec:
                rec[key] = value
        if record.exc_info:
            rec["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            rec["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(rec, default=str, ensure_ascii=False)


class TextLogFormatter(logging.Formatter):
    """Human-readable format for local/dev; still redacts as defense-in-depth."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_TEXT_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def build_formatter(component: str = COMPONENT) -> logging.Formatter:
    """Return a JSON or text formatter per the ``LOG_FORMAT`` env var."""
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        return JsonLogFormatter(component=component)
    return TextLogFormatter(_TEXT_FMT, datefmt=_TEXT_DATEFMT)


def configure_logging(
    *,
    component: str = COMPONENT,
    app_namespaces: tuple[str, ...] = (),
    disable_uvicorn_access: bool = False,
) -> None:
    """Install the root handler with JSON or text formatting + redaction.

    ``LOG_LEVEL`` sets the level; when ``DEBUG`` (and ``DEBUG_ALL`` unset) only
    ``app_namespaces`` get DEBUG so third-party noise stays at INFO — preserving
    the prior behaviour of the inline ``basicConfig`` blocks this replaces.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler()  # stderr (matches basicConfig default)
    handler.setFormatter(build_formatter(component))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)

    if level <= logging.DEBUG and not os.getenv("DEBUG_ALL"):
        root.setLevel(logging.INFO)
        for namespace in app_namespaces:
            logging.getLogger(namespace).setLevel(logging.DEBUG)
    else:
        root.setLevel(level)

    if disable_uvicorn_access:
        logging.getLogger("uvicorn.access").disabled = True

    # Route Python warnings (warnings.warn) through logging so they're JSON too
    # (via the root handler) instead of raw stderr. See centralized_logging.md.
    logging.captureWarnings(True)


# ---------------------------------------------------------------------------
# Request correlation (ASGI middleware)
# ---------------------------------------------------------------------------

# Sanitise an inbound X-Request-ID so a hostile value can't inject newlines /
# break a log line; keep it id-shaped and bounded.
_RID_SANITISE = re.compile(r"[^A-Za-z0-9._-]")


def _request_id_from_headers(scope: dict) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"x-request-id":
            cleaned = _RID_SANITISE.sub("", value.decode("latin-1"))[:64]
            return cleaned or None
    return None


class CorrelationIdMiddleware:
    """Pure-ASGI middleware that binds ``request_id`` for the whole request.

    A ``BaseHTTPMiddleware`` runs the route handler in a *separate* task, so a
    contextvar set there never reaches the handler. This middleware sets the
    contextvar in the same task the downstream app is awaited in — and the
    handler sub-task copies that context at spawn — so ``request_id`` tags both
    the access-log line and the route handler's own logs. Register it
    **outermost** (add it last) so it wraps the access-logging middleware.

    Honors an inbound ``X-Request-ID`` for cross-service trace continuity, else
    generates one. Never lets request-id logic break a request.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            request_id = _request_id_from_headers(scope) or uuid4().hex[:12]
        except Exception:
            request_id = uuid4().hex[:12]
        token = bind_log_context(request_id=request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_log_context(token)
