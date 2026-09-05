"""Authoritative capability and Canvas actions for the shared browser."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from orchestrator.services import resolve_ssh_key_path
from orchestrator.services.browser_stream_broker import (
    BrowserStreamUnavailable,
    exec_stream_info,
)
from orchestrator.services.browser_stream_config import (
    BrowserStreamConfigurationError,
    browser_stream_config,
)
from orchestrator.services.canvas import (
    BrowserSource,
    CanvasMutation,
    CanvasPreconditionFailed,
    CanvasService,
    CanvasSetInput,
)
from orchestrator.services.canvas_ssh import (
    CanvasSSHError,
    GenerationResolver,
    RemoteWorkspaceTarget,
    asyncssh,
    bound_workspace_generation,
    require_same_remote_workspace,
    resolve_remote_workspace_target,
)
from orchestrator.services.ssh_helpers import orchestrator_can_reach
from orchestrator.services.workspace_binding import remote_canvas_presentation_available
from shared.backend_kinds import LITE_BACKENDS

BrowserCapabilityReason = Literal[
    "feature_disabled",
    "workspace_required",
    "workspace_unattested",
    "workspace_unroutable",
    "transport_unavailable",
]


class BrowserCapabilityResponse(BaseModel):
    """Closed owner-facing pre-source capability without private identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_enabled: bool
    can_open_browser: bool
    workspace_ready: bool
    reason: BrowserCapabilityReason | None


class BrowserCanvasError(Exception):
    """Typed shared-browser action failure safe for an HTTP boundary."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PreparedBrowser:
    """Private browser identity paired to the workspace target that minted it."""

    browser_generation: UUID
    workspace_target: RemoteWorkspaceTarget


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _thread_backend(thread: dict[str, Any]) -> str:
    """Resolve current metadata plus the legacy direct fixture field."""

    metadata = _mapping(thread.get("metadata"))
    direct = metadata.get("workspace_backend")
    if isinstance(direct, str) and direct:
        return direct
    config_override = _mapping(metadata.get("config_override"))
    workspace = _mapping(config_override.get("workspace"))
    backend = workspace.get("backend")
    return backend if isinstance(backend, str) and backend else "sandbox"


def _active_workspace_context(metadata: dict[str, Any]) -> dict[str, Any]:
    vm = _mapping(metadata.get("vm"))
    if vm.get("status") == "ready":
        return vm
    return _mapping(metadata.get("workspace_container"))


def _capability(
    *,
    feature_enabled: bool,
    can_open_browser: bool,
    workspace_ready: bool,
    reason: BrowserCapabilityReason | None,
) -> BrowserCapabilityResponse:
    return BrowserCapabilityResponse(
        feature_enabled=feature_enabled,
        can_open_browser=can_open_browser,
        workspace_ready=workspace_ready,
        reason=reason,
    )


def browser_capability(thread: dict[str, Any]) -> BrowserCapabilityResponse:
    """Evaluate the exact owner-facing ability to create or recover a browser."""

    enabled = os.getenv("CANVAS_SHARED_BROWSER_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return _capability(
            feature_enabled=False,
            can_open_browser=False,
            workspace_ready=False,
            reason="feature_disabled",
        )
    try:
        browser_stream_config()
        transport_configured = True
    except BrowserStreamConfigurationError:
        transport_configured = False
    transport_available = (
        transport_configured and asyncssh is not None and bool(resolve_ssh_key_path())
    )

    backend = _thread_backend(thread)
    if backend in LITE_BACKENDS:
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=False,
            reason="workspace_required",
        )

    metadata = _mapping(thread.get("metadata"))
    context = _active_workspace_context(metadata)
    ready = context.get("status") == "ready"
    if backend not in {"sandbox", "container", "vm", "remote"}:
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=ready,
            reason="workspace_unattested",
        )
    if not ready:
        if backend in {"sandbox", "container"}:
            if not transport_available:
                return _capability(
                    feature_enabled=True,
                    can_open_browser=False,
                    workspace_ready=False,
                    reason="transport_unavailable",
                )
            return _capability(
                feature_enabled=True,
                can_open_browser=True,
                workspace_ready=False,
                reason=None,
            )
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=False,
            reason="workspace_unattested",
        )

    if not remote_canvas_presentation_available(metadata, context):
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=True,
            reason="workspace_unattested",
        )
    try:
        target = resolve_remote_workspace_target(
            thread,
            bound_workspace_generation(thread),
        )
    except CanvasSSHError:
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=True,
            reason="workspace_unattested",
        )
    if not orchestrator_can_reach(target.host):
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=True,
            reason="workspace_unroutable",
        )
    if not transport_available:
        return _capability(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=True,
            reason="transport_unavailable",
        )
    return _capability(
        feature_enabled=True,
        can_open_browser=True,
        workspace_ready=True,
        reason=None,
    )


def require_browser_capability(
    capability: BrowserCapabilityResponse,
    *,
    require_ready: bool,
) -> None:
    if capability.can_open_browser and (
        capability.workspace_ready or not require_ready
    ):
        return
    reason = capability.reason
    if reason == "feature_disabled":
        raise BrowserCanvasError(404, reason, "Shared browser is not enabled")
    if reason == "workspace_required":
        raise BrowserCanvasError(
            409,
            reason,
            "The shared browser requires a full workspace",
        )
    if reason == "workspace_unattested":
        raise BrowserCanvasError(
            409,
            reason,
            "The workspace is not attested for shared-browser access",
        )
    if reason == "workspace_unroutable":
        raise BrowserCanvasError(
            503,
            reason,
            "The workspace is not reachable from the browser broker",
        )
    if reason == "transport_unavailable":
        raise BrowserCanvasError(
            503,
            reason,
            "The shared-browser transport is unavailable",
        )
    raise BrowserCanvasError(
        503,
        "workspace_unavailable",
        "The workspace is still being prepared",
    )


async def prepare_browser_canvas(
    thread: dict[str, Any],
    *,
    initial_baton: Literal["agent", "user"],
    generation_resolver: GenerationResolver,
) -> PreparedBrowser:
    """Start/reuse the current browser without committing public Canvas state."""

    capability = browser_capability(thread)
    require_browser_capability(capability, require_ready=True)
    if initial_baton not in {"agent", "user"}:
        raise ValueError("initial_baton must be 'agent' or 'user'")
    try:
        target = resolve_remote_workspace_target(
            thread,
            bound_workspace_generation(thread),
        )
    except CanvasSSHError as exc:
        raise BrowserCanvasError(
            409,
            "workspace_generation_changed",
            "The workspace changed while the browser was starting",
        ) from exc
    try:
        info = await exec_stream_info(
            thread,
            initial_baton=initial_baton,
            generation_resolver=generation_resolver,
        )
    except BrowserStreamUnavailable as exc:
        raise BrowserCanvasError(
            exc.status,
            "transport_unavailable",
            exc.detail,
        ) from exc
    raw_generation = info.get("generation")
    try:
        generation = UUID(raw_generation)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BrowserCanvasError(
            502,
            "browser_identity_invalid",
            "browser-exec returned an invalid browser identity",
        ) from exc
    if raw_generation != str(generation):
        raise BrowserCanvasError(
            502,
            "browser_identity_invalid",
            "browser-exec returned an invalid browser identity",
        )
    return PreparedBrowser(
        browser_generation=generation,
        workspace_target=target,
    )


async def commit_browser_canvas(
    db: Any,
    thread_id: str,
    prepared: PreparedBrowser,
    *,
    title: str,
    expected_presentation_revision: int | None = None,
) -> CanvasMutation:
    """Revalidate the selected target and idempotently stage its browser."""

    current = await db.get_thread(thread_id)
    if current is None:
        raise BrowserCanvasError(404, "thread_unavailable", "Thread is unavailable")
    capability = browser_capability(dict(current))
    require_browser_capability(capability, require_ready=True)
    try:
        require_same_remote_workspace(dict(current), prepared.workspace_target)
    except CanvasSSHError as exc:
        raise BrowserCanvasError(
            409,
            "workspace_generation_changed",
            "The workspace changed while the browser was starting",
        ) from exc

    normalized_title = title.strip() or "Shared browser"
    try:
        return await CanvasService(db).set_if_changed(
            thread_id,
            CanvasSetInput(
                source=BrowserSource(
                    browser_generation=prepared.browser_generation,
                ),
                title=normalized_title,
                renderer="auto",
                editable=False,
                alt_text=None,
                source_version=None,
                new_app=False,
            ),
            expected_presentation_revision=expected_presentation_revision,
        )
    except CanvasPreconditionFailed as exc:
        raise BrowserCanvasError(
            409,
            "canvas_presentation_changed",
            "The Canvas presentation changed while the browser was starting",
        ) from exc


__all__ = [
    "BrowserCanvasError",
    "BrowserCapabilityReason",
    "BrowserCapabilityResponse",
    "PreparedBrowser",
    "_thread_backend",
    "browser_capability",
    "commit_browser_canvas",
    "prepare_browser_canvas",
    "require_browser_capability",
]
