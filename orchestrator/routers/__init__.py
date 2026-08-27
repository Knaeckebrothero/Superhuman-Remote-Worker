"""Domain-scoped FastAPI ``APIRouter`` modules.

First slice of the ``orchestrator/main.py`` split (see
``knowledge-base/knowledge/issues/orchestrator_main_py_monolith.md``). New domains land here
as their own router file; ``main.py`` includes them via
``app.include_router(...)``. Legacy ``@app.<method>`` handlers in
``main.py`` migrate over per domain as part of that refactor.
"""

from routers.automations import router as automations_router
from routers.canvases import internal_router as internal_canvases_router
from routers.canvases import router as canvases_router
from routers.project_loops import router as project_loops_router
from routers.product_capabilities import router as product_capabilities_router
from routers.shared_browser import router as shared_browser_router
from routers.wopi import router as wopi_router
from routers.vm_guest import router as vm_guest_router

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
