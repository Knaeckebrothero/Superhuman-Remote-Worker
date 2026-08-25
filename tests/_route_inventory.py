"""Route inventory that survives FastAPI's include_router representation change.

FastAPI < 0.139 flattened ``app.include_router(...)`` into ``app.routes``: every
mounted endpoint appeared there as its own ``APIRoute``. FastAPI >= 0.139
(Starlette >= 1.3) inserts one ``_IncludedRouter`` wrapper per include instead.
The wrapper carries no ``path`` or ``methods`` of its own and exposes the router
through ``original_router``, so the obvious comprehension

    {(r.path, m) for r in app.routes for m in getattr(r, "methods", ())}

silently drops every router-mounted endpoint. Silently is the problem: an
assertion that a route is *registered* still fails loudly, but one that asserts a
route is *absent*, or that scans the surface for a property, quietly stops
looking at ~15 routers' worth of the API.

``requirements.txt`` pins ``fastapi>=0.109.0``, so CI resolves the newest release
while a long-lived local venv can sit years behind. A route-introspecting test
written against the old shape therefore passes locally and fails only in CI.

Both readings are handled here: recurse through ``original_router`` for the new
shape, through ``.routes`` for Starlette ``Mount`` sub-apps, and yield plain
routes directly for the old one. Every router in this repo carries its own
``prefix=``, and nothing passes ``prefix=`` to ``include_router``, so the paths
on ``original_router`` are already the paths the app serves.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_mounted_routes(routes: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(method, path)`` for every route reachable from ``routes``."""
    seen: set[int] = set()

    def walk(current: Any) -> Iterator[tuple[str, str]]:
        if id(current) in seen:
            return
        seen.add(id(current))
        for route in current:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                for method in methods:
                    yield method, path
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)
            sub_routes = getattr(route, "routes", None)
            if sub_routes:
                yield from walk(sub_routes)

    yield from walk(routes)


def mounted_routes(app: Any) -> set[tuple[str, str]]:
    """``(method, path)`` pairs an app actually serves, mounted routers included."""
    return set(iter_mounted_routes(app.routes))
