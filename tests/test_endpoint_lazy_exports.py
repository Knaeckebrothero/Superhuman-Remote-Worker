"""Declared lazy package aliases remain visible to static route discovery."""

import importlib

import pytest

from tests.test_endpoint_discovery import (
    application_sources as _application_sources,
    identities,
    mounted_identities,
)
from tests.test_endpoint_inventory import _load_script

application_sources = _application_sources


@pytest.mark.parametrize(
    "typing_import,guard",
    [
        ("from typing import TYPE_CHECKING", "TYPE_CHECKING"),
        ("from typing import TYPE_CHECKING as TC", "TC"),
    ],
)
@pytest.mark.parametrize("declare_map", [False, True])
def test_declared_lazy_package_aliases_match_mounted_http_ws_and_gate_labels(
    application_sources, typing_import, guard, declare_map
):
    map_declaration = (
        "_EXPORTS = {'public_router': ('leaf', 'router'), 'internal_router': ('leaf', 'internal')}"
        if declare_map
        else ""
    )
    module_expression = (
        "__name__ + '.' + _EXPORTS[name][0]" if declare_map else "__name__ + '.leaf'"
    )
    attribute_expression = "_EXPORTS[name][1]" if declare_map else "targets[name]"
    main, package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            from .routers import public_router, internal_router
            app = FastAPI()
            app.include_router(public_router, prefix='/api')
            app.include_router(internal_router, prefix='/auth', include_in_schema=False)
            """,
            "routers/__init__.py": f"""
            from importlib import import_module
            {typing_import}
            if {guard}:
                from .leaf import router as public_router
                from .leaf import internal as internal_router
                from .leaf import unused
            __all__ = ['public_router', 'internal_router']
            {map_declaration}
            def __getattr__(name):
                targets = {{'public_router': 'router', 'internal_router': 'internal'}}
                if name not in targets:
                    raise AttributeError(name)
                value = getattr(import_module({module_expression}), {attribute_expression})
                globals()[name] = value
                return value
            """,
            "routers/leaf.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix='/public')
            internal = APIRouter(prefix='/private')
            unused = APIRouter(prefix='/unused')
            def require_approved_user(): pass
            def require_internal(): pass
            @router.api_route('/item', methods=['GET', 'HEAD'])
            def item(): require_approved_user()
            @internal.websocket('/socket')
            async def socket(ws): require_internal()
            @unused.get('/ghost')
            def ghost(): pass
            """,
        }
    )
    script = _load_script()
    found = script.discover_routes(main)
    assert identities(found) == mounted_identities(
        importlib.import_module(package + ".main").app
    )
    assert identities(found) == [
        ("GET", "/api/public/item"),
        ("HEAD", "/api/public/item"),
        ("WS", "/auth/private/socket"),
    ]
    endpoints = script.classify_routes(found, main_path=main)
    assert {
        (route.method, route.path, route.classification) for route in endpoints
    } == {
        ("GET", "/api/public/item", "gated:require_approved_user"),
        ("HEAD", "/api/public/item", "gated:require_approved_user"),
        ("WS", "/auth/private/socket", "internal:require_internal"),
    }


@pytest.mark.parametrize(
    "declaration",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import router as public_router\n__all__ = ['public_router']\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import router as public_router\n__all__ = ['other']\ndef __getattr__(name): pass\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import router as public_router\n__all__ = make_exports()\ndef __getattr__(name): pass\n",
        "from typing import TYPE_CHECKING\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    from .leaf import router as public_router\n__all__ = ['public_router']\ndef __getattr__(name): pass\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import router as public_router\nelse:\n    from .other import router as public_router\n__all__ = ['public_router']\ndef __getattr__(name): pass\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import *\n__all__ = ['public_router']\ndef __getattr__(name): pass\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import router as public_router\n    public_router.get('/ghost')(lambda: None)\n__all__ = ['public_router']\ndef __getattr__(name): pass\n",
    ],
    ids=[
        "no-getattr",
        "not-public",
        "dynamic-all",
        "shadowed-guard",
        "runtime-else",
        "star-import",
        "registration-in-guard",
    ],
)
def test_type_only_or_ambiguous_exports_do_not_invent_runtime_router_bindings(
    application_sources, declaration
):
    main, _package = application_sources(
        {
            "main.py": "from fastapi import FastAPI\nfrom .routers import public_router\napp = FastAPI()\napp.include_router(public_router)\n",
            "routers/__init__.py": declaration,
            "routers/leaf.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/api/x')\ndef route(): pass\n",
        }
    )
    script = _load_script()
    with pytest.raises(
        script.UnsupportedRouteError, match="requires a named APIRouter"
    ):
        script.discover_routes(main)


@pytest.mark.parametrize(
    "runtime_import,typing_imports",
    [
        (
            "from .other import router as public_router\n",
            "    from .leaf import router as public_router\n",
        ),
        (
            "",
            "    from .leaf import router as public_router\n    from .other import router as public_router\n",
        ),
    ],
    ids=["runtime-binding-conflict", "two-declared-targets"],
)
def test_conflicting_public_aliases_fail_instead_of_selecting_one_router(
    application_sources, runtime_import, typing_imports
):
    main, _package = application_sources(
        {
            "main.py": "from fastapi import FastAPI\nfrom .routers import public_router\napp = FastAPI()\napp.include_router(public_router)\n",
            "routers/__init__.py": runtime_import
            + "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            + typing_imports
            + "__all__ = ['public_router']\ndef __getattr__(name): pass\n",
            "routers/leaf.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/api/declared')\ndef declared(): pass\n",
            "routers/other.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/api/runtime')\ndef runtime(): pass\n",
        }
    )
    script = _load_script()
    with pytest.raises(
        script.UnsupportedRouteError, match="binding 'public_router' is reassigned"
    ):
        script.discover_routes(main)


@pytest.mark.parametrize(
    "mapping",
    [
        "{'public_router': ('other', 'router')}",
        "{'public_router': ('leaf', 'other_attribute')}",
        "{'different_export': ('leaf', 'router')}",
        "{'public_router': ('leaf', 'router'), 'public_router': ('leaf', 'router')}",
        "{'public_router': ['leaf', 'router']}",
        "make_exports()",
    ],
    ids=[
        "different-module",
        "different-attribute",
        "different-public-name",
        "duplicate-key",
        "unsupported-shape",
        "dynamic-map",
    ],
)
def test_literal_runtime_export_mapping_must_match_declared_targets(
    application_sources, mapping
):
    main, _package = application_sources(
        {
            "main.py": "from fastapi import FastAPI\nfrom .routers import public_router\napp = FastAPI()\napp.include_router(public_router)\n",
            "routers/__init__.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .leaf import router as public_router\n__all__ = ['public_router']\n_EXPORTS = "
            + mapping
            + "\ndef __getattr__(name): pass\n",
            "routers/leaf.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/api/x')\ndef route(): pass\n",
        }
    )
    script = _load_script()
    with pytest.raises(
        script.UnsupportedRouteError,
        match="lazy _EXPORTS must be a literal mapping matching TYPE_CHECKING exports",
    ):
        script.discover_routes(main)
