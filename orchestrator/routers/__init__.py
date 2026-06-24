"""Domain-scoped FastAPI ``APIRouter`` modules.

First slice of the ``orchestrator/main.py`` split (see
``docs/issues/orchestrator_main_py_monolith.md``). New domains land here
as their own router file; ``main.py`` includes them via
``app.include_router(...)``. Legacy ``@app.<method>`` handlers in
``main.py`` migrate over per domain as part of that refactor.
"""

from routers.automations import router as automations_router
from routers.project_loops import router as project_loops_router

__all__ = ["automations_router", "project_loops_router"]
