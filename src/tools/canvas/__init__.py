"""Dynamic Canvas tools for persistent sessions.

The orchestrator owns source validation, authorization, normalization, and
durable state; these tools are thin delegated-user transport adapters plus
post-commit invalidation.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Dict, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..context import ToolContext

logger = logging.getLogger(__name__)

_LOGICAL_STATE_FIELDS = (
    "canvas_id",
    "title",
    "renderer",
    "editable",
    "alt_text",
    "presentation_revision",
    "source_version",
    "status",
    "updated_at",
)


CANVAS_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "get_canvas": {
        "module": "canvas",
        "function": "get_canvas",
        "description": (
            "Inspect the persistent thread's shared Canvas before replacing its "
            "stage or changing a file the user may be viewing. Returns logical "
            "presentation metadata, not source bytes or credentials."
        ),
        "category": "canvas",
        "short_description": "Inspect the current shared Canvas presentation.",
        "phases": ["strategic", "tactical"],
    },
    "set_canvas": {
        "module": "canvas",
        "function": "set_canvas",
        "description": (
            "Present or refresh a validated workspace file or, when the current "
            "workspace advertises live preview, one loopback workspace port on "
            "the persistent thread's shared Canvas. Never supply a hostname or "
            "URL. Re-read files the user may have changed before overwriting "
            "them, and call set_canvas again after updating a presented source."
        ),
        "category": "canvas",
        "short_description": "Present or refresh a workspace source on Canvas.",
        "phases": ["strategic", "tactical"],
    },
    "clear_canvas": {
        "module": "canvas",
        "function": "clear_canvas",
        "description": (
            "Clear the persistent thread's shared Canvas presentation. This does "
            "not delete its source file or stop any workspace process."
        ),
        "category": "canvas",
        "short_description": "Clear the shared Canvas without deleting its source.",
        "phases": ["strategic", "tactical"],
    },
}


class _NoCanvasArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SetFileCanvasArguments(BaseModel):
    """The exact flat file-only schema advertised to persistent agents."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: Literal["workspace_file"] = Field(
        description="Canvas source kind. The file stage supports 'workspace_file'."
    )
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="Workspace-relative path of the existing file to present.",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional descriptive stage title; defaults to the filename.",
    )
    renderer: Literal["auto", "markdown", "text", "html", "image"] = Field(
        default="auto",
        description="Trusted installed renderer, or 'auto' for server detection.",
    )
    editable: bool = Field(
        default=False,
        description=(
            "Allow conditional user saves for validated writable text sources. "
            "Images and unavailable/read-only workspaces are rejected."
        ),
    )
    alt_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="Concise meaningful alternative text; required for images.",
    )


class _SetLiveCanvasArguments(BaseModel):
    """Flat file-or-port schema exposed only by an attested live-app backend."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: Literal["workspace_file", "workspace_port"] = Field(
        description="Present an existing workspace file or one workspace loopback port."
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Workspace-relative file path; workspace_file only.",
    )
    port: int | None = Field(
        default=None,
        ge=1024,
        le=65535,
        description="Workspace loopback HTTP port; workspace_port only.",
    )
    entry_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="Absolute application entry path; workspace_port only, defaults to '/'.",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional descriptive stage title.",
    )
    renderer: Literal["auto", "markdown", "text", "html", "image"] = Field(
        default="auto",
        description="File renderer. Live applications require 'auto'.",
    )
    editable: bool = Field(
        default=False,
        description="Conditional file editing. Live applications are never source-editable.",
    )
    alt_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="Meaningful image description; workspace_file only.",
    )
    new_app: bool = Field(
        default=False,
        description=(
            "Rotate isolated app storage when unrelated code takes over the same port. "
            "workspace_port only."
        ),
    )

    @model_validator(mode="after")
    def _source_fields_do_not_mix(self) -> "_SetLiveCanvasArguments":
        if self.source_type == "workspace_file":
            if self.path is None:
                raise ValueError("workspace_file requires path")
            if self.port is not None or self.entry_path is not None or self.new_app:
                raise ValueError("workspace_file does not accept live-app fields")
            return self

        if self.port is None:
            raise ValueError("workspace_port requires port")
        if self.path is not None or self.alt_text is not None:
            raise ValueError("workspace_port does not accept file fields")
        if self.renderer != "auto" or self.editable:
            raise ValueError(
                "workspace_port requires renderer='auto' and editable=false"
            )
        return self


def get_canvas_metadata() -> Dict[str, Dict[str, Any]]:
    """Return registry metadata for the Canvas category."""
    return CANVAS_TOOLS_METADATA.copy()


def _require_persistent_identity(context: ToolContext) -> tuple[str, str]:
    thread_id = str(context.thread_id or "").strip()
    user_id = str(context.user_id or "").strip()
    if not thread_id or not user_id:
        raise RuntimeError(
            "Canvas tools require a persistent session with an authenticated owner"
        )
    return thread_id, user_id


def _new_orchestrator_client(config_name: str, user_id: str):
    # Lazy import avoids src.api.__init__ -> app -> agent -> tools during registry
    # initialization. Tool invocation happens only after the runtime is built.
    from src.api.orchestrator_client import create_orchestrator_client_from_env

    return create_orchestrator_client_from_env(
        config_name,
        user_id=user_id,
    )


def _client_for(context: ToolContext, user_id: str):
    return _new_orchestrator_client(
        str(context.config.get("agent_id") or "persistent"), user_id=user_id
    )


async def _close_client(client: Any) -> None:
    """Best-effort cleanup that never masks a request result or committed state."""
    try:
        await client.close()
    except Exception as exc:
        logger.debug("Canvas orchestrator client close failed: %s", exc)


def _event_params(state: dict[str, Any]) -> dict[str, Any]:
    source = state.get("source")
    source_type = source.get("type") if isinstance(source, dict) else None
    params: dict[str, Any] = {
        "canvas_id": state.get("canvas_id", "main"),
        "presentation_revision": state.get("presentation_revision"),
        "updated_at": state.get("updated_at"),
    }
    if source_type:
        params["source_type"] = source_type
    return params


def _logical_canvas_state(state: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the model-facing logical state serialization.

    Public GET legitimately adds a Cockpit ``content_url``. Future viewer or
    proxy fields belong to the browser too, so use a positive allowlist instead
    of stripping only today's URL field.
    """
    logical = {key: state.get(key) for key in _LOGICAL_STATE_FIELDS}
    source = state.get("source")
    if source is None:
        logical["source"] = None
    elif isinstance(source, dict):
        source_type = source.get("type")
        if source_type == "workspace_file":
            allowed_source_fields = ("type", "path")
        elif source_type == "workspace_app":
            # Entry path is logical presentation metadata. Ports, origin
            # generations, route tables, manifests, and internal addresses are
            # deliberately absent from the model-facing result.
            allowed_source_fields = ("type", "entry_path")
        else:
            allowed_source_fields = ("type",)
        logical["source"] = {
            key: source[key] for key in allowed_source_fields if key in source
        }
    else:
        raise RuntimeError("Orchestrator returned an invalid Canvas source")

    capabilities = state.get("capabilities")
    logical["capabilities"] = (
        {
            key: capabilities[key]
            for key in ("can_edit", "can_pop_out", "can_take_control")
            if key in capabilities
        }
        if isinstance(capabilities, dict)
        else {}
    )
    return logical


async def _emit_after_commit(
    context: ToolContext, method: str, state: dict[str, Any]
) -> None:
    callback = context.canvas_event_callback
    if callback is None:
        return
    try:
        result = callback(method, _event_params(state))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        # State is already committed. REST reconciliation is authoritative, so
        # event delivery failure must not tell the model the mutation failed.
        logger.warning("Canvas event callback failed after commit: %s", exc)


def create_canvas_tools(context: ToolContext) -> list[Any]:
    """Create capability-scoped Canvas tools for a persistent context."""

    # Every Canvas action delegates through an agent-internal orchestrator
    # route. Some Helm modes intentionally omit the generated Secret, so fail
    # registration closed instead of advertising tools that can only return
    # authorization errors. Check presence only; never log or retain the value.
    if not os.getenv("MCP_INTERNAL_KEY", "").strip():
        raise RuntimeError("Canvas internal service authentication is unavailable")

    backend = getattr(context.workspace_manager, "backend", None)
    set_arguments = (
        _SetLiveCanvasArguments
        if getattr(backend, "supports_canvas_live_apps", False)
        else _SetFileCanvasArguments
    )

    @tool(args_schema=_NoCanvasArguments)
    async def get_canvas() -> dict[str, Any] | None:
        """Inspect the current shared Canvas presentation, if one exists."""
        thread_id, user_id = _require_persistent_identity(context)
        client = _client_for(context, user_id)
        try:
            state = await client.get_thread_canvas(thread_id)
        finally:
            await _close_client(client)
        return _logical_canvas_state(state) if state is not None else None

    @tool(args_schema=set_arguments)
    async def set_canvas(
        source_type: Literal["workspace_file", "workspace_port"],
        path: str | None = None,
        title: str | None = None,
        renderer: Literal["auto", "markdown", "text", "html", "image"] = "auto",
        editable: bool = False,
        alt_text: str | None = None,
        port: int | None = None,
        entry_path: str | None = None,
        new_app: bool = False,
    ) -> dict[str, Any]:
        """Present or refresh one validated workspace source on Canvas."""
        thread_id, user_id = _require_persistent_identity(context)
        if source_type == "workspace_file":
            if path is None:  # Defensive for direct calls bypassing args_schema.
                raise ValueError("workspace_file requires path")
            payload: dict[str, Any] = {
                "source_type": source_type,
                "path": path,
                "renderer": renderer,
                "editable": editable,
            }
            if alt_text is not None:
                payload["alt_text"] = alt_text
        else:
            if port is None:
                raise ValueError("workspace_port requires port")
            payload = {
                "source_type": source_type,
                "port": port,
                "entry_path": entry_path or "/",
                "renderer": "auto",
                "editable": False,
                "new_app": new_app,
            }
        if title is not None:
            payload["title"] = title

        client = _client_for(context, user_id)
        try:
            state = await client.set_thread_canvas(thread_id, payload)
        finally:
            await _close_client(client)
        state = _logical_canvas_state(state)
        await _emit_after_commit(context, "canvas.updated", state)
        return state

    @tool(args_schema=_NoCanvasArguments)
    async def clear_canvas() -> dict[str, Any] | None:
        """Clear Canvas without deleting or stopping its underlying source."""
        thread_id, user_id = _require_persistent_identity(context)
        client = _client_for(context, user_id)
        try:
            result = await client.clear_thread_canvas(thread_id)
        finally:
            await _close_client(client)
        if result is None:
            return None
        state = _logical_canvas_state(result.state)
        if result.changed:
            await _emit_after_commit(context, "canvas.cleared", state)
        return state

    return [get_canvas, set_canvas, clear_canvas]


__all__ = [
    "CANVAS_TOOLS_METADATA",
    "create_canvas_tools",
    "get_canvas_metadata",
]
