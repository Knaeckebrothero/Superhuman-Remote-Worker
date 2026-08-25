#!/usr/bin/env python3
"""Walk the orchestrator's routes and classify every endpoint by access gate.

Output: one line per endpoint in stable sort order:

    METHOD  /api/path/{param}  classification[:detail]

Classifications:
  gated:<gate>     — at least one known gate call in the body or signature
  admin:<gate>     — admin-only (`_require_admin`, or the fleet-scoped
                     `_require_infrastructure_fleet_admin` wrapper)
  internal:<gate>  — authenticated non-user service boundary
  public:<reason>  — opt-out via `# nosec: public <reason>` comment on the
                     line immediately above the decorator
  unscoped         — no gate found; would fail the C2 snapshot test

The CI snapshot lives at policy/endpoint_inventory.txt; a mismatch
fails the regression test and forces a manual review of any new endpoint.

Two route sources are walked:

  * `@app.METHOD(...)` in orchestrator/main.py — restricted to /api/* paths,
    which is every route main.py serves that this inventory cares about.
  * `@<router>.METHOD(...)` in any module that builds an `APIRouter`, with the
    router's `prefix=` folded into the path. These are mounted by main.py's
    `app.include_router(...)` block and were invisible here until 2026-07-31;
    the /auth/* and /wopi/* paths among them are listed in full rather than
    filtered to /api/*, since those are the ones worth eyeballing.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = REPO_ROOT / "orchestrator"
MAIN_PY = ORCHESTRATOR / "main.py"
MANIFEST = REPO_ROOT / "policy" / "endpoint_inventory.txt"

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Functions that gate access. `_require_admin` and `require_approved_user`
# are also gates — even an auth-only gate beats no gate. P4b added
# ``require_internal`` (shared-secret bootstrap for agent ↔ orchestrator)
# and ``require_internal_or_job_access`` (hybrid: agent key bypasses user
# auth, otherwise normal job access check). Infrastructure-metering routes use
# a separate HMAC-signed collector credential; their shared dispatch boundary
# authenticates the exact method, path, body digest, timestamp, and nonce before
# invoking an ingestion operation. The metering *admin* routes wrap
# ``_require_admin`` in ``_require_infrastructure_fleet_admin``, which
# additionally rejects project-scoped MCP admins (403) so activation boundaries
# can only be moved by a real fleet admin; main.py does not follow local
# helpers (see _collect_module), so that wrapper has to be named here.
GATE_NAMES = {
    "_dispatch_infrastructure_ingestion",
    "_require_admin",
    "_require_infrastructure_fleet_admin",
    "is_internal_call",
    "require_approved_user",
    "require_internal",
    "require_internal_or_job_access",
    "require_job_access",
    "require_project_member",
    "require_project_owner",
    "require_thread_owner",
    "require_datasource_access",
    "require_datasource_owner",
    "require_sudo_request_authority",
    # Per-VM bearer + provision-generation check for guest->orchestrator calls
    # (orchestrator/security/vm_guest.py). Authenticated service boundary, no
    # user identity involved, so it classifies alongside require_internal.
    "require_vm_guest",
    "user_can_access_any_job",
    "user_can_access_job",
    # job-first then thread-owner resolver — gates session citations whose
    # ``job_id`` is actually a thread id with no ``jobs`` row (citation panel).
    "user_can_access_job_or_thread",
    "user_can_access_datasource",
    "user_can_access_ide_entity",
    # BFF cookie-session resolver — raises 401 when there is no valid session.
    # Auth-only, same tier as require_approved_user.
    "get_current_user",
}


# main.py does not follow local helpers in general (see _collect_module for
# why). These names are the audited exception: each is a thin boundary shared
# by a small family of routes, each calls its gate as the first statement, and
# none reaches a second gate — so following it reports that family's real gate
# rather than conflating it with something else downstream. Following only
# these — never every module-level def — keeps the conflation risk the blanket
# rule guards against, because a bare call to anything not listed here is still
# not followed.
#
# Membership rule, when you are tempted to add one: the helper must call
# exactly one GATE_NAMES function, unconditionally, before any other work.
FOLLOW_LOCAL_MAIN = {
    "_enter_infrastructure_storage_source_shadow",
    "_schedule_infrastructure_storage_source_activation",
    # Officer message actions (officer-reply/-escalate/-ack): `await
    # require_internal(request)` is its first statement, and every later check
    # is officer-identity, not a second gate.
    "_require_officer_route_actor",
}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    func_name: str
    classification: (
        str  # "gated:<name>" | "admin:_require_admin" | "public:<reason>" | "unscoped"
    )

    def render(self) -> str:
        return f"{self.method.upper():<6} {self.path:<80} {self.classification}"


def _decorator_call(decorator: ast.expr) -> ast.Call | None:
    return decorator if isinstance(decorator, ast.Call) else None


def _route_info(
    decorator: ast.Call, route_vars: dict[str, str]
) -> tuple[str, str] | None:
    """Return (method, path) if this decorator mounts a route on a known object.

    ``route_vars`` maps the decorated variable to the prefix its routes hang
    off — ``{"app": ""}`` for main.py, or one entry per ``APIRouter`` in a
    router module (``{"router": "/api/contacts", ...}``).
    """
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    if not (isinstance(func.value, ast.Name) and func.value.id in route_vars):
        return None
    method = func.attr
    if method not in HTTP_METHODS:
        return None
    if not decorator.args:
        return None
    path_node = decorator.args[0]
    if not (isinstance(path_node, ast.Constant) and isinstance(path_node.value, str)):
        return None
    return method, route_vars[func.value.id] + path_node.value


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    """Map each `x = APIRouter(prefix=...)` variable to its prefix."""
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        name = (
            callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        )
        if name != "APIRouter":
            continue
        prefix = ""
        for keyword in node.value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                prefix = keyword.value.value
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _local_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Module-level defs, so `_gate_calls` can follow a handler into its helper."""
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _gate_calls(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    local_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    _seen: set[str] | None = None,
) -> list[str]:
    """Names of gate functions invoked in the body or as Depends() in signature.

    Calls into a module-local helper are followed (cycle-guarded), because the
    routers routinely wrap their gate in a private one — contacts'
    ``_owned_contact``, canvases' ``_require_delegated_owner`` — and reporting
    those handlers as ungated would be a false positive, not a finding.
    """
    local_funcs = local_funcs or {}
    _seen = _seen if _seen is not None else set()
    found: list[str] = []

    def _resolve(name: str | None, *, bare: bool) -> None:
        """``bare`` = called as `helper(...)`, not `something.helper(...)`.

        Only bare names may be followed into a local def. `db.get_project_contacts()`
        is a database method that happens to share a name with the route handler
        of the same resource — recursing on attribute calls picks up that
        handler's gate and reports it as this endpoint's.
        """
        if name is None:
            return
        if name in GATE_NAMES:
            found.append(name)
        elif bare and name in local_funcs and name not in _seen:
            _seen.add(name)
            found.extend(_gate_calls(local_funcs[name], local_funcs, _seen))

    # Body: look for Call nodes whose func name is a gate
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            name: str | None = None
            bare = False
            if isinstance(target, ast.Name):
                name, bare = target.id, True
            elif isinstance(target, ast.Attribute):
                name = target.attr
            _resolve(name, bare=bare)
    # Signature: Depends(gate) markers in default values
    for default in func.args.defaults + func.args.kw_defaults:
        if not isinstance(default, ast.Call):
            continue
        target = default.func
        if not (isinstance(target, ast.Name) and target.id == "Depends"):
            continue
        if not default.args:
            continue
        dep_target = default.args[0]
        dep_name: str | None = None
        dep_bare = False
        if isinstance(dep_target, ast.Name):
            dep_name, dep_bare = dep_target.id, True
        elif isinstance(dep_target, ast.Attribute):
            dep_name = dep_target.attr
        _resolve(dep_name, bare=dep_bare)
    return found


def _classify(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    public_reason: str | None,
    local_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
) -> str:
    if public_reason is not None:
        return f"public:{public_reason}" if public_reason else "public"
    gates = _gate_calls(func, local_funcs)
    if not gates:
        return "unscoped"
    # Pick the label that describes the typical-caller path. Resource-bound
    # gates beat the admin gate because admin is usually a fallback when the
    # endpoint also exposes a per-resource path. Endpoints that ONLY check
    # admin still get labeled "admin:_require_admin".
    priority = {
        "require_internal_or_job_access": 0,
        "require_job_access": 0,
        "require_project_owner": 0,
        "require_project_member": 0,
        "require_thread_owner": 0,
        "require_datasource_owner": 0,
        "require_datasource_access": 0,
        "require_sudo_request_authority": 0,
        "user_can_access_any_job": 1,
        "user_can_access_job": 1,
        "user_can_access_job_or_thread": 1,
        "user_can_access_datasource": 1,
        "user_can_access_ide_entity": 1,
        "_require_admin": 2,
        "_require_infrastructure_fleet_admin": 2,
        "_dispatch_infrastructure_ingestion": 2,
        "require_internal": 2,
        "require_vm_guest": 2,
        "is_internal_call": 2,
        "require_approved_user": 3,
    }
    primary = sorted(gates, key=lambda g: priority.get(g, 99))[0]
    if primary in ("_require_admin", "_require_infrastructure_fleet_admin"):
        return f"admin:{primary}"
    if primary in (
        "_dispatch_infrastructure_ingestion",
        "require_internal",
        "require_vm_guest",
        "is_internal_call",
    ):
        return f"internal:{primary}"
    return f"gated:{primary}"


def _public_reason(source_lines: list[str], decorator_lineno: int) -> str | None:
    """Read `# nosec: public <reason>` on the line directly above the decorator.

    The decorator's lineno is 1-based; the comment, if present, is on the
    previous source line. Stops at the first non-comment/blank line.
    """
    idx = decorator_lineno - 2  # line above decorator (0-based)
    while idx >= 0:
        line = source_lines[idx].strip()
        if not line:
            idx -= 1
            continue
        if not line.startswith("#"):
            return None
        if line.startswith("# nosec: public"):
            reason = line[len("# nosec: public") :].strip(": ").strip()
            return reason
        idx -= 1
    return None


def _collect_module(
    path: Path, *, app_var: str | None = None, api_only: bool = False
) -> list[Endpoint]:
    """Endpoints declared in one module.

    ``app_var`` walks a single FastAPI instance (main.py); otherwise every
    ``APIRouter`` in the module is walked with its prefix applied.

    Local-helper following is router-first. The routers are small,
    single-purpose modules where a private wrapper around a gate is
    unambiguous, so every module-level def is followable. main.py is not: its
    handlers call large shared helpers that reach a gate somewhere downstream,
    and following those conflates "this handler's gate" with "a gate reachable
    from here" — which then outranks the real label and reports admin-only and
    internal-only endpoints as ordinary resource-gated ones. main.py therefore
    follows only the audited ``FOLLOW_LOCAL_MAIN`` names, which preserves every
    classification the manifest already carried while letting a thin
    delegating route report the gate its dispatch target enforces.
    """
    source = path.read_text()
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    route_vars = {app_var: ""} if app_var else _router_prefixes(tree)
    if not route_vars:
        return []
    local_funcs = _local_functions(tree)
    if app_var:
        local_funcs = {
            name: node
            for name, node in local_funcs.items()
            if name in FOLLOW_LOCAL_MAIN
        }
    endpoints: list[Endpoint] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = _decorator_call(decorator)
            if call is None:
                continue
            info = _route_info(call, route_vars)
            if info is None:
                continue
            method, route = info
            if api_only and not route.startswith("/api/"):
                continue
            reason = _public_reason(source_lines, decorator.lineno)
            endpoints.append(
                Endpoint(
                    method=method,
                    path=route,
                    func_name=node.name,
                    classification=_classify(node, reason, local_funcs),
                )
            )
    return endpoints


def _router_modules() -> list[Path]:
    """Orchestrator modules that build an APIRouter, in stable order.

    Discovered rather than listed so a new router file lands in the manifest
    the day it is written — the failure mode this walk exists to close was a
    router extraction silently dropping three endpoints off the inventory.
    """
    return sorted(
        path
        for path in ORCHESTRATOR.rglob("*.py")
        if path != MAIN_PY and "APIRouter(" in path.read_text()
    )


def collect_endpoints(main_path: Path = MAIN_PY) -> list[Endpoint]:
    endpoints = _collect_module(main_path, app_var="app", api_only=True)
    for module in _router_modules():
        endpoints.extend(_collect_module(module))
    endpoints.sort(key=lambda e: (e.path, e.method))
    return endpoints


def render_manifest(endpoints: list[Endpoint]) -> str:
    header = (
        "# orchestrator endpoint inventory — generated by scripts/check_endpoint_auth.py\n"
        "# DO NOT EDIT BY HAND. Regenerate with `python scripts/check_endpoint_auth.py --write`.\n"
        "#\n"
        "# SCOPE: main.py `@app.METHOD(...)` /api/* routes, plus every APIRouter the\n"
        "# app mounts (orchestrator/routers/*, auth/bff.py, uploads.py, graph_routes.py)\n"
        "# with its prefix folded in. Router /auth/* and /wopi/* are listed in full.\n"
        "#\n"
        "# Classifications:\n"
        "#   gated:<gate>           — protected by a require_* / user_can_access_* helper\n"
        "#   admin:<gate>           — admin-only; the fleet-scoped variant also\n"
        "#                            rejects project-scoped MCP admins\n"
        "#   internal:<helper>      — authenticated non-user service boundary\n"
        "#   public:<reason>        — opt-out via `# nosec: public <reason>` on line above decorator\n"
        "#   unscoped               — no gate detected; CI snapshot test will fail\n"
        "\n"
    )
    body = "\n".join(e.render() for e in endpoints)
    return header + body + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the manifest to policy/endpoint_inventory.txt",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed manifest is stale or unscoped endpoints exist",
    )
    args = parser.parse_args()

    endpoints = collect_endpoints()
    rendered = render_manifest(endpoints)

    unscoped = [e for e in endpoints if e.classification == "unscoped"]

    if args.write:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(rendered)
        print(f"wrote {len(endpoints)} endpoints to {MANIFEST.relative_to(REPO_ROOT)}")
        if unscoped:
            print(f"WARNING: {len(unscoped)} unscoped endpoints in inventory:")
            for e in unscoped:
                print(f"  {e.render()}")
        return 0

    if args.check:
        if not MANIFEST.exists():
            print(f"ERROR: {MANIFEST} missing — run with --write", file=sys.stderr)
            return 2
        on_disk = MANIFEST.read_text()
        if on_disk != rendered:
            print(
                f"ERROR: {MANIFEST.relative_to(REPO_ROOT)} is stale.\n"
                "Run `python scripts/check_endpoint_auth.py --write` and commit.",
                file=sys.stderr,
            )
            return 1
        if unscoped:
            print(
                f"ERROR: {len(unscoped)} unscoped endpoints detected:",
                file=sys.stderr,
            )
            for e in unscoped:
                print(f"  {e.render()}", file=sys.stderr)
            print(
                "\nAdd a `Depends(require_*)` / in-body gate, or mark the endpoint\n"
                "`# nosec: public <reason>` on the line above the decorator.",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {len(endpoints)} endpoints, all gated.")
        return 0

    # Default: print to stdout
    sys.stdout.write(rendered)
    if unscoped:
        print(
            f"\n# {len(unscoped)} unscoped endpoint(s) above — would fail --check",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
