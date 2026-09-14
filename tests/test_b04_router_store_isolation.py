"""Two applications, two stores: a router serves the one handling the request.

R1.B04 replaced ``from orchestrator.main import postgres_db`` in the Canvas,
WOPI and shared-browser routers with ``request.app.state.store``. The point of
that move is not tidiness: while the store was a module singleton, two FastAPI
applications in one process shared one store no matter which one was answering.
These cases mount the same router objects on two applications carrying distinct
stores and interleave requests, so a regression to any process-wide lookup
fails here rather than in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from orchestrator.routers import canvases, shared_browser, wopi
from orchestrator.services.canvas_office import CanvasOfficeError
from orchestrator.services.shared_browser_canvas import BrowserCapabilityResponse

from ._mounted_router import mount_router


_THREAD_ID = "a1111111-1111-1111-1111-111111111111"
_FILE_ID = "b2222222-2222-2222-2222-222222222222"


class _AbsentCanvasService:
    """Enough of ``CanvasService`` for the read route to answer 204."""

    async def get(self, thread_id: str) -> None:
        del thread_id
        return None


def _two_apps(router: Any) -> tuple[Any, Any, object, object]:
    """Mount one router twice, on applications holding two distinct stores."""

    first: object = object()
    second: object = object()
    app_a = mount_router(router)
    app_b = mount_router(router)
    app_a.state.store = first
    app_b.state.store = second
    return app_a, app_b, first, second


def test_canvas_read_route_uses_the_store_of_its_own_application(monkeypatch) -> None:
    seen: list[object] = []

    async def owner(request, db, thread_id):
        del request
        seen.append(db)
        return {"id": "user-1"}, {"id": thread_id, "user_id": "user-1"}

    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    monkeypatch.setattr(
        canvases, "_get_canvas_service", lambda db: _AbsentCanvasService()
    )

    app_a, app_b, store_a, store_b = _two_apps(canvases.router)
    url = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"

    assert TestClient(app_a).get(url).status_code == 204
    assert TestClient(app_b).get(url).status_code == 204
    assert TestClient(app_a).get(url).status_code == 204

    assert seen == [store_a, store_b, store_a]


def test_shared_browser_capability_uses_the_store_of_its_own_application(
    monkeypatch,
) -> None:
    seen: list[object] = []

    async def owner(request, db, thread_id):
        del request
        seen.append(db)
        return {"id": "user-1"}, {"id": thread_id, "user_id": "user-1"}

    monkeypatch.setattr(shared_browser, "require_thread_owner", owner)
    monkeypatch.setattr(
        shared_browser,
        "browser_capability",
        lambda thread: BrowserCapabilityResponse(
            feature_enabled=False,
            can_open_browser=False,
            workspace_ready=False,
            reason="feature_disabled",
        ),
    )

    app_a, app_b, store_a, store_b = _two_apps(shared_browser.router)
    url = f"/api/persistent/threads/{_THREAD_ID}/browser/capability"

    assert TestClient(app_a).get(url).status_code == 200
    assert TestClient(app_b).get(url).status_code == 200
    assert TestClient(app_a).get(url).status_code == 200

    assert seen == [store_a, store_b, store_a]


def test_wopi_token_admission_uses_the_store_of_its_own_application(
    monkeypatch,
) -> None:
    seen: list[object] = []

    class _RejectingTokens:
        async def authenticate(self, token, *, file_id, require_write=False):
            del token, file_id, require_write
            raise CanvasOfficeError(401, "wopi_token_invalid", "no")

    def token_service(db):
        seen.append(db)
        return _RejectingTokens()

    monkeypatch.setattr(wopi, "_get_token_service", token_service)
    monkeypatch.setattr(
        wopi, "_get_collabora_config", lambda: SimpleNamespace(require_enabled=dict)
    )

    app_a, app_b, store_a, store_b = _two_apps(wopi.router)
    url = f"/wopi/files/{_FILE_ID}?access_token=wopi-token"

    assert TestClient(app_a).get(url).status_code == 401
    assert TestClient(app_b).get(url).status_code == 401
    assert TestClient(app_a).get(url).status_code == 401

    assert seen == [store_a, store_b, store_a]


@pytest.mark.parametrize(
    "module", [canvases, wopi, shared_browser], ids=lambda m: m.__name__
)
def test_router_store_lookup_never_reaches_the_application_module(module) -> None:
    """The helper resolves per call, off the request, and imports nothing."""

    import ast
    import inspect

    helper = ast.parse(inspect.getsource(module._get_db)).body[0]
    assert isinstance(helper, ast.FunctionDef)
    assert [argument.arg for argument in helper.args.args] == ["request"]
    assert not [
        node
        for node in ast.walk(helper)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert ast.unparse(helper.body[-1]) == "return request.app.state.store"
