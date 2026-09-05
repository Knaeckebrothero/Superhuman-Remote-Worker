"""Compare the static inventory with small real FastAPI compositions, offline."""

import importlib
import sys
import textwrap

import pytest

from tests.test_endpoint_inventory import _load_script


@pytest.fixture
def application_sources(tmp_path, monkeypatch):
    packages = []
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(files):
        name = f"route_fixture_{len(packages)}"
        packages.append(name)
        directory = tmp_path / name
        directory.mkdir()
        (directory / "__init__.py").write_text("")
        for relative, source in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).replace("__PACKAGE__", name))
        return directory / "main.py", name

    yield write

    for key in list(sys.modules):
        if any(key == name or key.startswith(name + ".") for name in packages):
            del sys.modules[key]


def mounted_identities(app):
    """Read actual FastAPI route contexts, including schema-hidden HTTP and WS.

    New FastAPI versions retain included routers; older versions copy their
    routes directly. This compatibility stays in the test oracle, not the
    dependency-free source scanner. Framework-generated Starlette docs routes
    are intentionally outside the declared-application-route scope.
    """
    from fastapi import routing

    iterator = getattr(routing, "_iter_routes_with_context", None)
    routes = (
        iterator(app.routes) if iterator else ((route, None) for route in app.routes)
    )
    result = []
    for route, context in routes:
        effective = getattr(context, "starlette_route", None)
        if effective is not None:
            path = effective.path
        else:
            path = context.path if context is not None else route.path
        if isinstance(route, routing.APIRoute):
            result.extend((method, path) for method in route.methods)
        elif isinstance(route, routing.APIWebSocketRoute):
            result.append(("WS", path))
    return sorted(result)


def identities(routes):
    return sorted((route.method, route.path) for route in routes)


def test_composed_routers_match_real_fastapi_including_prefixes_aliases_and_ws(
    application_sources,
):
    main, package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            from .routers import mounted as api
            from . import unused

            app = FastAPI()

            @app.get('/api/direct')
            def direct():
                return None

            app.include_router(api, prefix='/api')
            app.include_router(router=api, prefix='/auth', include_in_schema=False)
        """,
            "routers/__init__.py": """
            from fastapi import APIRouter as Router
            from .leaf import router as child

            mounted: Router = Router(prefix='/v1')
            mounted.include_router(child, prefix='/nested')
        """,
            "routers/leaf.py": """
            import fastapi as api

            router = api.APIRouter(prefix='/inner', include_in_schema=False)
            unused = api.APIRouter(prefix='/unused')

            @router.get('/item')
            def item():
                return None

            @router.api_route(path='/multi', methods=('GET', 'HEAD'))
            def multi():
                return None

            @router.api_route('/default')
            def default():
                return None

            @router.websocket('/socket')
            async def socket(websocket):
                pass

            @unused.get('/not-mounted')
            def not_mounted():
                return None
        """,
            "unused.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix='/api/ghost')
            @router.get('/never-mounted')
            def ghost():
                pass
        """,
        }
    )
    script = _load_script()
    found = script.discover_routes(main)
    app = importlib.import_module(package + ".main").app

    assert identities(found) == mounted_identities(app)
    assert len(found) == 11
    assert ("WS", "/api/v1/nested/inner/socket") in identities(found)
    assert ("HEAD", "/auth/v1/nested/inner/multi") in identities(found)
    assert not any("unused" in route.path or "ghost" in route.path for route in found)
    assert not any(
        route.method == "HEAD" and route.path.endswith("/item") for route in found
    )


@pytest.mark.parametrize(
    ("import_source", "reference"),
    [
        ("from . import routes as api", "api.router"),
        ("import __PACKAGE__.routes as api", "api.router"),
        ("import __PACKAGE__.routes", "__PACKAGE__.routes.router"),
    ],
)
def test_imported_module_aliases_resolve_the_same_mounted_router(
    application_sources, import_source, reference
):
    main, package = application_sources(
        {
            "main.py": f"from fastapi import FastAPI\n{import_source}\napp = FastAPI()\napp.include_router({reference}, prefix='/api')\n",
            "routes.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/v1')\n@router.get('/x')\ndef route(): pass\n",
        }
    )
    script = _load_script()
    assert (
        identities(script.discover_routes(main))
        == mounted_identities(importlib.import_module(package + ".main").app)
        == [("GET", "/api/v1/x")]
    )


def test_route_identity_and_gate_label_survive_a_main_to_router_move(
    application_sources,
):
    before, before_package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            app = FastAPI()
            def require_approved_user():
                pass
            @app.api_route('/api/contacts', methods=['GET', 'HEAD'])
            def contacts():
                require_approved_user()
        """,
        }
    )
    after, after_package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            from .contacts import router
            app = FastAPI()
            app.include_router(router, prefix='/api')
        """,
            "contacts.py": """
            from fastapi import APIRouter
            router = APIRouter()
            def require_approved_user():
                pass
            @router.api_route('/contacts', methods=['GET', 'HEAD'])
            def contacts():
                require_approved_user()
        """,
        }
    )
    script = _load_script()
    for main, package in ((before, before_package), (after, after_package)):
        assert identities(script.discover_routes(main)) == mounted_identities(
            importlib.import_module(package + ".main").app
        )
    assert script.render_manifest(
        script.collect_endpoints(before)
    ) == script.render_manifest(script.collect_endpoints(after))


def test_discovery_does_not_import_application_code_and_policy_scope_is_explicit(
    application_sources,
):
    main, _package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            raise RuntimeError('production must never be imported by this gate')
            app = FastAPI()
            @app.get('/debug/only')
            def debug():
                pass
            @app.get('/api/public')
            def public():
                pass
        """,
        }
    )
    script = _load_script()
    routes = script.discover_routes(main)
    assert identities(routes) == [("GET", "/api/public"), ("GET", "/debug/only")]
    assert identities(script.collect_endpoints(main)) == [("GET", "/api/public")]


def test_literal_branch_exclusion_and_explicit_http_methods(application_sources):
    main, package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI, APIRouter
            app = FastAPI()
            router = APIRouter(prefix='/api')
            @router.head('/item')
            @router.options('/item')
            @router.trace('/item')
            def item():
                pass
            if False:
                @router.get('/disabled')
                def disabled():
                    pass
            if True:
                app.include_router(router)
        """,
        }
    )
    script = _load_script()
    assert (
        identities(script.discover_routes(main))
        == mounted_identities(importlib.import_module(package + ".main").app)
        == [("HEAD", "/api/item"), ("OPTIONS", "/api/item"), ("TRACE", "/api/item")]
    )


def test_classification_remains_separate_from_router_dependencies(application_sources):
    main, _package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI, Depends, APIRouter
            def require_approved_user():
                pass
            router = APIRouter(dependencies=[Depends(require_approved_user)])
            # nosec: public fixture-owned callback
            @router.api_route('/public', methods=['GET', 'POST'])
            def public():
                pass
            @router.get('/implicit')
            def implicit():
                pass
            app = FastAPI()
            app.include_router(router, prefix='/api', dependencies=[Depends(require_approved_user)])
        """,
        }
    )
    script = _load_script()
    routes = script.discover_routes(main)
    assert not hasattr(routes[0], "classification")
    endpoints = script.classify_routes(routes, main_path=main)
    assert {
        route.classification for route in endpoints if route.path == "/api/public"
    } == {"public:fixture-owned callback"}
    assert (
        next(
            route for route in endpoints if route.path == "/api/implicit"
        ).classification
        == "unscoped"
    )


def test_application_dependency_state_does_not_change_route_identity(
    application_sources,
):
    main, package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI, Depends
            app = FastAPI()
            app.state.contacts_dependencies = object()
            def original_dependency():
                return None
            def replacement_dependency():
                return None
            app.dependency_overrides[original_dependency] = replacement_dependency
            @app.get('/api/contacts')
            def contacts(dependency=Depends(original_dependency)):
                return None
        """,
        }
    )
    script = _load_script()
    assert (
        identities(script.discover_routes(main))
        == mounted_identities(importlib.import_module(package + ".main").app)
        == [("GET", "/api/contacts")]
    )


def test_repository_declarations_match_the_assembled_application():
    from orchestrator.main import app

    script = _load_script()
    assert identities(script.discover_routes()) == mounted_identities(app)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("@app.get(PATH)\ndef route(): pass", "route path must be a literal"),
        (
            "@app.api_route('/api/x', methods=METHODS)\ndef route(): pass",
            "methods must be a nonempty literal",
        ),
        (
            "@app.api_route('/api/x', methods=[])\ndef route(): pass",
            "methods must be a nonempty literal",
        ),
        ("@app.get('/api/x', **options)\ndef route(): pass", "expanded decorator"),
        (
            "router = APIRouter(prefix=PREFIX)\napp.include_router(router)",
            "router prefix must be a literal",
        ),
        (
            "router = APIRouter()\napp.include_router(router, prefix=PREFIX)",
            "router prefix must be a literal",
        ),
        ("app.include_router(make_router())", "requires a named APIRouter"),
        (
            "if ENABLED:\n    @app.get('/api/x')\n    def route(): pass",
            "conditional, looped or nested",
        ),
        (
            "for router in routers:\n    app.include_router(router)",
            "conditional, looped or nested",
        ),
        ("app.mount('/api', other_app)", "mount route mutation"),
        ("app.add_api_route('/api/x', handler)", "add_api_route route mutation"),
        (
            "@app.websocket_route('/api/x')\nasync def socket(ws): pass",
            "websocket_route registration",
        ),
        (
            "router = APIRouter()\nresult = app.include_router(router)",
            "registration inside an assignment",
        ),
        ("app.router.routes = []", "assignment to router state"),
        ("app.routes.append(route)", "append route mutation"),
        (
            "register = app.get\n@register('/api/x')\ndef route(): pass",
            "dynamic route decorator",
        ),
        ("configure_routes(app)", "registration helper"),
        ("helpers.configure_routes(app)", "registration helper"),
        (
            "router = APIRouter()\napp.include_router(router)\n@router.get('/api/late')\ndef late(): pass",
            "registration after include_router",
        ),
        (
            "router = APIRouter()\nrouter.include_router(router)\napp.include_router(router)",
            "cyclic include_router",
        ),
    ],
)
def test_unsupported_registration_fails_instead_of_silently_omitting_routes(
    application_sources, body, reason
):
    main, _package = application_sources(
        {
            "main.py": "from fastapi import FastAPI, APIRouter\napp = FastAPI()\n"
            + body
            + "\n",
        }
    )
    script = _load_script()
    with pytest.raises(script.UnsupportedRouteError, match=reason) as raised:
        script.discover_routes(main)
    assert str(main) in str(raised.value)


def test_dynamic_app_factory_is_explicitly_unsupported(application_sources):
    main, _package = application_sources(
        {
            "main.py": "app = create_app()\n@app.get('/api/x')\ndef route(): pass\n",
        }
    )
    script = _load_script()
    with pytest.raises(script.UnsupportedRouteError, match="named FastAPI instance"):
        script.discover_routes(main)


def test_delegated_browser_websocket_retains_the_audited_gate_label(
    application_sources,
):
    main, _package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            from service import relay_browser_stream
            app = FastAPI()
            @app.websocket('/api/persistent/threads/{thread_id}/browser/stream')
            async def browser(ws, thread_id):
                await relay_browser_stream(ws, thread_id, db=database)
        """,
        }
    )
    script = _load_script()
    endpoints = script.collect_endpoints(main)
    assert [(route.method, route.classification) for route in endpoints] == [
        ("WS", "gated:relay_browser_stream")
    ]


def test_registration_on_an_imported_router_is_explicitly_unsupported(
    application_sources,
):
    main, _package = application_sources(
        {
            "main.py": """
            from fastapi import FastAPI
            from .routes import router
            app = FastAPI()
            @router.get('/api/x')
            def route(): pass
            app.include_router(router)
        """,
            "routes.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
        }
    )
    script = _load_script()
    with pytest.raises(
        script.UnsupportedRouteError, match="registration on an imported router"
    ):
        script.discover_routes(main)


def test_cli_reports_excluded_routes_and_refuses_unsupported_composition(
    application_sources, monkeypatch, capsys
):
    main, _package = application_sources(
        {
            "main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/outside')\ndef outside(): pass\n",
        }
    )
    script = _load_script()
    routes = script.discover_routes(main)
    monkeypatch.setattr(script, "discover_routes", lambda: routes)
    monkeypatch.setattr(sys, "argv", ["check_endpoint_auth.py"])
    assert script.main() == 0
    captured = capsys.readouterr()
    assert "GET /outside" in captured.err
    assert "/outside" not in captured.out

    def unsupported():
        raise script.UnsupportedRouteError("fixture.py:9: dynamic prefix")

    monkeypatch.setattr(script, "discover_routes", unsupported)
    monkeypatch.setattr(sys, "argv", ["check_endpoint_auth.py", "--write"])
    manifest = main.parent / "inventory.txt"
    monkeypatch.setattr(script, "MANIFEST", manifest)
    assert script.main() == 2
    assert not manifest.exists()
    assert "fixture.py:9: dynamic prefix" in capsys.readouterr().err
