"""Explicit composition bindings for existing catalogue integration tests.

These tests still exercise canonical main-owned policy with patched stores.
They call the new owner with its dependencies; no production compatibility
handlers or duplicate catalogue cache are kept merely to support the tests.
The independent HTTP tests mount their own factories instead.
"""

from collections.abc import Awaitable, Callable
from typing import Any


def catalogue_service():
    from orchestrator.main import _expert_catalog_service

    return _expert_catalog_service()


def authoring_service():
    from orchestrator.main import _expert_catalog_dependencies

    return _expert_catalog_dependencies().authoring


def catalogue_state():
    from orchestrator.main import app

    return app.state.expert_catalog_state


def catalogue_route(handler: Callable[..., Awaitable[Any]]):
    """Bind a new router handler to the current explicit application ports."""

    async def invoke(*args, **kwargs):
        from orchestrator.main import _expert_catalog_dependencies

        return await handler(*args, deps=_expert_catalog_dependencies(), **kwargs)

    return invoke


def patch_service_method(monkeypatch, service_type, name, replacement):
    """Preserve a collaborator double's arguments when patching a service method."""
    monkeypatch.setattr(
        service_type, name, lambda _self, *args, **kwargs: replacement(*args, **kwargs)
    )
