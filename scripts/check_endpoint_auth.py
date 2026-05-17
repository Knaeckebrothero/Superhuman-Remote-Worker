#!/usr/bin/env python3
"""Walk orchestrator/main.py and classify every /api/* endpoint by access gate.

Output: one line per endpoint in stable sort order:

    METHOD  /api/path/{param}  classification[:detail]

Classifications:
  gated:<gate>     — at least one known gate call in the body or signature
  admin:_require_admin
  public:<reason>  — opt-out via `# nosec: public <reason>` comment on the
                     line immediately above the decorator
  unscoped         — no gate found; would fail the C2 snapshot test

The CI snapshot lives at docs/security/endpoint_inventory.txt; a mismatch
fails the regression test and forces a manual review of any new endpoint.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = REPO_ROOT / "orchestrator" / "main.py"
MANIFEST = REPO_ROOT / "docs" / "security" / "endpoint_inventory.txt"

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Functions that gate access. `_require_admin` and `require_approved_user`
# are also gates — even an auth-only gate beats no gate.
GATE_NAMES = {
    "_require_admin",
    "require_approved_user",
    "require_job_access",
    "require_project_member",
    "require_project_owner",
    "require_thread_owner",
    "require_datasource_access",
    "require_datasource_owner",
    "require_sudo_request_authority",
    "require_builder_session_owner",
    "user_can_access_any_job",
    "user_can_access_job",
    "user_can_access_datasource",
    "user_can_access_ide_entity",
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


def _route_info(decorator: ast.Call) -> tuple[str, str] | None:
    """Return (method, path) if this is `app.METHOD("/api/...")`."""
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    if not (isinstance(func.value, ast.Name) and func.value.id == "app"):
        return None
    method = func.attr
    if method not in HTTP_METHODS:
        return None
    if not decorator.args:
        return None
    path_node = decorator.args[0]
    if not (isinstance(path_node, ast.Constant) and isinstance(path_node.value, str)):
        return None
    path = path_node.value
    if not path.startswith("/api/"):
        return None
    return method, path


def _gate_calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Names of gate functions invoked in the body or as Depends() in signature."""
    found: list[str] = []
    # Body: look for Call nodes whose func name is a gate
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            name: str | None = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if name in GATE_NAMES:
                found.append(name)
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
        if isinstance(dep_target, ast.Name):
            dep_name = dep_target.id
        elif isinstance(dep_target, ast.Attribute):
            dep_name = dep_target.attr
        if dep_name in GATE_NAMES:
            found.append(dep_name)
    return found


def _classify(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    public_reason: str | None,
) -> str:
    if public_reason is not None:
        return f"public:{public_reason}" if public_reason else "public"
    gates = _gate_calls(func)
    if not gates:
        return "unscoped"
    # Pick the label that describes the typical-caller path. Resource-bound
    # gates beat the admin gate because admin is usually a fallback when the
    # endpoint also exposes a per-resource path. Endpoints that ONLY check
    # admin still get labeled "admin:_require_admin".
    priority = {
        "require_job_access": 0,
        "require_project_owner": 0,
        "require_project_member": 0,
        "require_thread_owner": 0,
        "require_datasource_owner": 0,
        "require_datasource_access": 0,
        "require_sudo_request_authority": 0,
        "require_builder_session_owner": 0,
        "user_can_access_any_job": 1,
        "user_can_access_job": 1,
        "user_can_access_datasource": 1,
        "user_can_access_ide_entity": 1,
        "_require_admin": 2,
        "require_approved_user": 3,
    }
    primary = sorted(gates, key=lambda g: priority.get(g, 99))[0]
    if primary == "_require_admin":
        return f"admin:{primary}"
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


def collect_endpoints(main_path: Path = MAIN_PY) -> list[Endpoint]:
    source = main_path.read_text()
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(main_path))
    endpoints: list[Endpoint] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = _decorator_call(decorator)
            if call is None:
                continue
            info = _route_info(call)
            if info is None:
                continue
            method, path = info
            reason = _public_reason(source_lines, decorator.lineno)
            classification = _classify(node, reason)
            endpoints.append(
                Endpoint(
                    method=method,
                    path=path,
                    func_name=node.name,
                    classification=classification,
                )
            )
    endpoints.sort(key=lambda e: (e.path, e.method))
    return endpoints


def render_manifest(endpoints: list[Endpoint]) -> str:
    header = (
        "# orchestrator endpoint inventory — generated by scripts/check_endpoint_auth.py\n"
        "# DO NOT EDIT BY HAND. Regenerate with `python scripts/check_endpoint_auth.py --write`.\n"
        "#\n"
        "# Classifications:\n"
        "#   gated:<gate>           — protected by a require_* / user_can_access_* helper\n"
        "#   admin:_require_admin   — admin-only\n"
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
        help="Write the manifest to docs/security/endpoint_inventory.txt",
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
