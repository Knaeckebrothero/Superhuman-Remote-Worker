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

from agent.tools.context import ToolContext

from shared.tool_catalog.definitions import (
    CANVAS_TOOLS_METADATA as CANVAS_TOOLS_METADATA,
)

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
    renderer: Literal[
        "auto", "markdown", "text", "html", "html-interactive", "image", "office"
    ] = Field(
        default="auto",
        description=(
            "Trusted installed renderer. 'auto' detects strict HTML, which strips "
            "style blocks and scripts; explicitly choose 'html-interactive' for a "
            "self-contained HTML artifact needing full CSS or inline JavaScript."
        ),
    )
    editable: bool = Field(
        default=False,
        description=(
            "Allow coordinated user saves for validated writable text, HTML, "
            "or Office sources. Office editing additionally requires the "
            "deployment's Collabora service; images and read-only workspaces "
            "are rejected."
        ),
    )
    alt_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description=(
            "Concise meaningful alternative text; required for images and not "
            "used for Office documents."
        ),
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
    renderer: Literal[
        "auto", "markdown", "text", "html", "html-interactive", "image", "office"
    ] = Field(
        default="auto",
        description=(
            "File renderer. 'html' is strict and inert; 'html-interactive' preserves "
            "self-contained CSS and inline scripts in a no-network sandbox. Live "
            "applications require 'auto'."
        ),
    )
    editable: bool = Field(
        default=False,
        description=(
            "Coordinated file editing for validated writable text, HTML, or "
            "Office sources. Office requires Collabora; live applications are "
            "never source-editable."
        ),
    )
    alt_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description=(
            "Meaningful image description; workspace_file only and unused for Office."
        ),
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


class _SetBrowserCanvasArguments(BaseModel):
    """Flat file-or-browser schema exposed by a browser-capable backend."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: Literal["workspace_file", "browser"] = Field(
        description="Present an existing workspace file or the current shared browser."
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Workspace-relative file path; workspace_file only.",
    )
    browser_id: Literal["current"] | None = Field(
        default=None,
        description=(
            "Browser selector; browser requires 'current' and resolves it at call time."
        ),
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional descriptive stage title.",
    )
    renderer: Literal[
        "auto", "markdown", "text", "html", "html-interactive", "image", "office"
    ] = Field(
        default="auto",
        description=(
            "File renderer. 'html' is strict and inert; 'html-interactive' preserves "
            "self-contained CSS and inline scripts in a no-network sandbox. Browser "
            "presentation requires 'auto'."
        ),
    )
    editable: bool = Field(
        default=False,
        description=(
            "Coordinated file editing for validated writable text, HTML, or "
            "Office sources. Office requires Collabora; browser presentation "
            "is never editable."
        ),
    )
    alt_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description=(
            "Meaningful image description; workspace_file only and unused for Office."
        ),
    )

    @model_validator(mode="after")
    def _source_fields_do_not_mix(self) -> "_SetBrowserCanvasArguments":
        if self.source_type == "workspace_file":
            if self.path is None:
                raise ValueError("workspace_file requires path")
            if self.browser_id is not None:
                raise ValueError("workspace_file does not accept browser fields")
            return self

        if self.browser_id != "current":
            raise ValueError("browser requires browser_id='current'")
        if self.path is not None or self.alt_text is not None:
            raise ValueError("browser does not accept file fields")
        if self.renderer != "auto" or self.editable:
            raise ValueError("browser requires renderer='auto' and editable=false")
        return self


class _SetLiveBrowserCanvasArguments(BaseModel):
    """Flat schema exposed by a backend attested for apps and the browser."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: Literal["workspace_file", "workspace_port", "browser"] = Field(
        description=(
            "Present an existing file, an attested loopback port, or the current browser."
        )
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
        description="Absolute application entry path; workspace_port only.",
    )
    browser_id: Literal["current"] | None = Field(
        default=None,
        description=(
            "Browser selector; browser requires 'current' and resolves it at call time."
        ),
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional descriptive stage title.",
    )
    renderer: Literal[
        "auto", "markdown", "text", "html", "html-interactive", "image", "office"
    ] = Field(
        default="auto",
        description=(
            "File renderer. 'html' is strict and inert; 'html-interactive' preserves "
            "self-contained CSS and inline scripts in a no-network sandbox. Live "
            "applications and browser require 'auto'."
        ),
    )
    editable: bool = Field(
        default=False,
        description=(
            "Coordinated editing for validated writable text, HTML, or Office "
            "files; Office requires Collabora. Applications and browser are "
            "never editable."
        ),
    )
    alt_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description=(
            "Meaningful image description; workspace_file only and unused for Office."
        ),
    )
    new_app: bool = Field(
        default=False,
        description=(
            "Rotate isolated app storage when unrelated code takes over the same port. "
            "workspace_port only."
        ),
    )

    @model_validator(mode="after")
    def _source_fields_do_not_mix(self) -> "_SetLiveBrowserCanvasArguments":
        if self.source_type == "workspace_file":
            if self.path is None:
                raise ValueError("workspace_file requires path")
            if (
                self.port is not None
                or self.entry_path is not None
                or self.browser_id is not None
                or self.new_app
            ):
                raise ValueError("workspace_file does not accept app or browser fields")
            return self

        if self.source_type == "workspace_port":
            if self.port is None:
                raise ValueError("workspace_port requires port")
            if (
                self.path is not None
                or self.alt_text is not None
                or self.browser_id is not None
            ):
                raise ValueError(
                    "workspace_port does not accept file or browser fields"
                )
            if self.renderer != "auto" or self.editable:
                raise ValueError(
                    "workspace_port requires renderer='auto' and editable=false"
                )
            return self

        if self.browser_id != "current":
            raise ValueError("browser requires browser_id='current'")
        if (
            self.path is not None
            or self.port is not None
            or self.entry_path is not None
            or self.alt_text is not None
            or self.new_app
        ):
            raise ValueError("browser does not accept file or live-app fields")
        if self.renderer != "auto" or self.editable:
            raise ValueError("browser requires renderer='auto' and editable=false")
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
    from agent.api.orchestrator_client import create_orchestrator_client_from_env

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
    supports_live_apps = getattr(backend, "supports_canvas_live_apps", False)
    supports_shared_browser = getattr(backend, "supports_canvas_shared_browser", False)
    if supports_live_apps and supports_shared_browser:
        set_arguments = _SetLiveBrowserCanvasArguments
    elif supports_live_apps:
        set_arguments = _SetLiveCanvasArguments
    elif supports_shared_browser:
        set_arguments = _SetBrowserCanvasArguments
    else:
        set_arguments = _SetFileCanvasArguments

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
        source_type: Literal["workspace_file", "workspace_port", "browser"],
        path: str | None = None,
        title: str | None = None,
        renderer: Literal[
            "auto",
            "markdown",
            "text",
            "html",
            "html-interactive",
            "image",
            "office",
        ] = "auto",
        editable: bool = False,
        alt_text: str | None = None,
        port: int | None = None,
        entry_path: str | None = None,
        browser_id: Literal["current"] | None = None,
        new_app: bool = False,
    ) -> dict[str, Any]:
        """Present a file, attested app, or current browser when advertised.

        ``browser_id='current'`` resolves at call time. Presenting it does not
        take the control baton away from a user who already holds it. Strict
        ``html`` strips style blocks and scripts; ``html-interactive`` runs a
        self-contained document in an opaque, no-network sandbox.
        """
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
        elif source_type == "workspace_port":
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
        else:
            if browser_id != "current":
                raise ValueError("browser requires browser_id='current'")
            payload = {
                "source_type": "browser",
                "browser_id": "current",
                "renderer": "auto",
                "editable": False,
            }
        if title is not None:
            payload["title"] = title

        client = _client_for(context, user_id)
        try:
            result = await client.set_thread_canvas(thread_id, payload)
        finally:
            await _close_client(client)
        state = _logical_canvas_state(result.state)
        if result.changed:
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
