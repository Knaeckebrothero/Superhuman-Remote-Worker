"""Domain-scoped FastAPI ``APIRouter`` modules.

Keep the existing public router exports lazy so importing one domain does not
load unrelated routers or their application/provider dependencies. Application
composition still explicitly imports and mounts each required router.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.routers.automations import router as automations_router
    from orchestrator.routers.canvases import (
        internal_router as internal_canvases_router,
    )
    from orchestrator.routers.canvases import router as canvases_router
    from orchestrator.routers.project_loops import router as project_loops_router
    from orchestrator.routers.product_capabilities import (
        router as product_capabilities_router,
    )
    from orchestrator.routers.shared_browser import router as shared_browser_router
    from orchestrator.routers.wopi import router as wopi_router
    from orchestrator.routers.vm_guest import router as vm_guest_router

__all__ = [
    "automations_router",
    "canvases_router",
    "internal_canvases_router",
    "project_loops_router",
    "product_capabilities_router",
    "shared_browser_router",
    "wopi_router",
    "vm_guest_router",
]

_EXPORTS = {
    "automations_router": ("automations", "router"),
    "canvases_router": ("canvases", "router"),
    "internal_canvases_router": ("canvases", "internal_router"),
    "project_loops_router": ("project_loops", "router"),
    "product_capabilities_router": ("product_capabilities", "router"),
    "shared_browser_router": ("shared_browser", "router"),
    "wopi_router": ("wopi", "router"),
    "vm_guest_router": ("vm_guest", "router"),
}


def __getattr__(name: str) -> Any:
    """Resolve and cache existing public exports without blocking submodules."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
