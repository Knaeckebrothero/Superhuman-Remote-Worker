#!/usr/bin/env python3
"""Inventory legacy source-tree migration inputs without importing application code.

This tool only understands the pre-flattening layout: src/ is the agent package,
orchestrator/ is separate, and vm/controller/ owns the VM controller. Run it with
--repo pointing to a frozen pre-move worktree, not the flattened checkout.

Only tracked application/test/tool sources are read by default. Additional files
must be named with --include. The candidate closure is evidence, NOT a move map.
Use --output to save deterministic JSON; the source hashes identify dirty inputs
even when the recorded Git revision has not changed.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any


APP_OWNERS = frozenset({"agent", "orchestrator", "mcp_server", "vm_controller"})
PYTHON_TREES = (
    "src/",
    "orchestrator/",
    "vm/controller/",
    "tests/",
    "scripts/",
    "eval/",
    "bench/",
)
CONSUMER_TREES = ("docker/", "helm/", ".github/workflows/", "policy/", "scripts/")
CONSUMER_FILES = frozenset(
    {
        "Tiltfile",
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".env.example",
        ".dockerignore",
        ".gitignore",
        ".squawk.toml",
    }
)


def owner(path: str) -> str:
    if path.startswith("src/shared/"):
        return "shared"
    if path.startswith("src/") or path == "agent.py":
        return "agent"
    if path.startswith("orchestrator/mcp/"):
        return "mcp_server"
    if path.startswith("orchestrator/"):
        return "orchestrator"
    if path.startswith("vm/controller/"):
        return "vm_controller"
    return "consumer"


def module_for_path(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def aliases_for_path(path: str) -> list[str]:
    aliases = [module_for_path(path)]
    if path.startswith("orchestrator/mcp/"):
        aliases.append(module_for_path(path.removeprefix("orchestrator/mcp/")))
    if path.startswith("orchestrator/"):
        aliases.append(module_for_path(path.removeprefix("orchestrator/")))
    if path.startswith("vm/controller/"):
        aliases.append(module_for_path(path.removeprefix("vm/controller/")))
    return aliases


def absolute_from(
    module: str, is_package: bool, imported: str | None, level: int
) -> str:
    if not level:
        return imported or ""
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".") if package else []
    if level > len(parts):
        raise ValueError(f"relative import escapes package: {module}, level={level}")
    return ".".join(parts[: len(parts) - level + 1] + ([imported] if imported else []))


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


class ImportVisitor(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool):
        self.module = module
        self.is_package = is_package
        self.deferred = 0
        self.type_only = 0
        self.imports: list[dict[str, Any]] = []
        self.hazards: list[dict[str, Any]] = []

    def _record(self, node: ast.AST, name: str, kind: str, level: int = 0) -> None:
        self.imports.append(
            {
                "name": name,
                "line": node.lineno,
                "kind": kind,
                "deferred": bool(self.deferred),
                "type_checking": bool(self.type_only),
                "relative_level": level,
            }
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(node, alias.name, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = absolute_from(self.module, self.is_package, node.module, node.level)
        self._record(node, base, "from", node.level)
        for alias in node.names:
            if alias.name != "*":
                self._record(node, f"{base}.{alias.name}", "from-member", node.level)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators/defaults execute eagerly; the function body is deferred.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        self.deferred += 1
        for statement in node.body:
            self.visit(statement)
        self.deferred -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        guarded = _dotted(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        self.type_only += guarded
        for statement in node.body:
            self.visit(statement)
        self.type_only -= guarded
        for statement in node.orelse:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        if name.endswith(
            (
                "import_module",
                "__import__",
                "spec_from_file_location",
                "SourceFileLoader",
            )
        ) or name.startswith("sys.path."):
            value = (
                node.args[0].value
                if node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                else None
            )
            self.hazards.append(
                {
                    "line": node.lineno,
                    "kind": name,
                    "module_argument": value
                    if value
                    and all(p.isidentifier() for p in value.lstrip(".").split("."))
                    else None,
                }
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__file__":
            self.hazards.append({"line": node.lineno, "kind": "file-relative-resource"})

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _dotted(node) == "sys.modules":
            self.hazards.append({"line": node.lineno, "kind": "sys.modules"})
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        value = node.value
        if "\n" not in value and value.startswith(
            (
                "src.",
                "orchestrator.",
                "main.",
                "services.",
                "database.",
                "security.",
                "mcp.",
            )
        ):
            self.hazards.append(
                {"line": node.lineno, "kind": "dotted-literal", "value": value}
            )
        elif "\n" not in value and value.startswith(
            ("src/", "orchestrator/", "/app/src/", "/app/database/")
        ):
            self.hazards.append(
                {"line": node.lineno, "kind": "path-literal", "value": value}
            )


def tracked_paths(repo: Path) -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=repo)
    return sorted(set(output.decode().strip("\0").split("\0")) - {""})


def census(repo: Path, paths: list[str], revision: str | None = None) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    imports: dict[str, list[dict[str, Any]]] = {}
    index: dict[str, list[str]] = defaultdict(list)
    hazards: list[dict[str, Any]] = []
    consumers: list[dict[str, Any]] = []
    for rel in sorted(set(paths)):
        path = repo / rel
        if not path.is_file():
            continue
        is_python = rel.endswith(".py") and (
            rel.startswith(PYTHON_TREES) or "/" not in rel
        )
        is_consumer = rel in CONSUMER_FILES or rel.startswith(CONSUMER_TREES)
        if not is_python and not is_consumer:
            continue
        content = path.read_bytes()
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if is_consumer:
            for lineno, line in enumerate(source.splitlines(), 1):
                if any(
                    term in line
                    for term in (
                        "orchestrator/",
                        "src/",
                        "agent.py",
                        "vm/controller",
                        "database.migrate",
                        "main:app",
                        "seed.llm_config",
                    )
                ):
                    # Inventory location, not potentially sensitive line contents.
                    consumers.append({"path": rel, "line": lineno})
        if not is_python:
            continue
        module = module_for_path(rel)
        aliases = aliases_for_path(rel)
        tree = ast.parse(source, filename=rel)
        visitor = ImportVisitor(module, path.name == "__init__.py")
        visitor.visit(tree)
        sources[rel] = {
            "path": rel,
            "module": module,
            "aliases": aliases,
            "owner": owner(rel),
            "is_package": path.name == "__init__.py",
            "lines": len(source.splitlines()),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        imports[rel] = visitor.imports
        hazards.extend({"path": rel, **hazard} for hazard in visitor.hazards)
        for alias in aliases:
            index[alias].append(rel)

    def resolve(name: str, importer: str) -> str | None:
        # Bare MCP imports in application code name the third-party SDK. The
        # legacy test harness deliberately gives our package that name instead.
        if name.split(".")[0] == "mcp" and not importer.startswith("tests/"):
            return None
        while name:
            if name in index:
                choices = index[name]
                if not name.startswith(("src.", "orchestrator.", "vm.")):
                    local = [
                        choice for choice in choices if owner(choice) == owner(importer)
                    ]
                    if local:
                        return local[0]
                canonical = [
                    choice for choice in choices if module_for_path(choice) == name
                ]
                return canonical[0] if canonical else choices[0]
            name = name.rpartition(".")[0]
        return None

    edges: list[dict[str, Any]] = []
    graph: dict[str, set[str]] = defaultdict(set)
    users: dict[str, set[str]] = defaultdict(set)
    for rel, records in imports.items():
        external: set[str] = set()
        for parent in PurePosixPath(rel).parents:
            init = str(parent / "__init__.py")
            if init in sources and init != rel:
                graph[rel].add(init)
                edges.append(
                    {
                        "source": rel,
                        "target": init,
                        "name": sources[init]["module"],
                        "line": 0,
                        "kind": "parent-initializer",
                        "deferred": False,
                        "type_checking": False,
                        "relative_level": 0,
                    }
                )
        for record in records:
            target = resolve(record["name"], rel)
            if target is None:
                external.add(record["name"].split(".")[0])
                continue
            if rel != target:
                edges.append({"source": rel, "target": target, **record})
                graph[rel].add(target)
                if owner(rel) in APP_OWNERS:
                    users[target].add(owner(rel))
            # Python executes every maintained parent initializer on import.
            for parent in PurePosixPath(target).parents:
                init = str(parent / "__init__.py")
                if init in sources and init not in {rel, target}:
                    graph[rel].add(init)
                    edges.append(
                        {
                            "source": rel,
                            "target": init,
                            **record,
                            "kind": "parent-initializer",
                        }
                    )
        sources[rel]["external_import_roots"] = sorted(external)
    candidates = []
    for target, importers in users.items():
        if owner(target) in APP_OWNERS:
            importers = importers | {owner(target)}
        if len(importers) >= 2 or owner(target) == "shared":
            candidates.append(
                {
                    "path": target,
                    "imported_by": sorted(importers),
                    "existing_shared": owner(target) == "shared",
                }
            )
    closure: set[str] = set()
    stack = [candidate["path"] for candidate in candidates]
    while stack:
        current = stack.pop()
        if current in closure:
            continue
        closure.add(current)
        stack.extend(graph.get(current, ()))
    cross = [
        edge
        for edge in edges
        if edge["kind"] != "parent-initializer"
        and owner(edge["source"]) in APP_OWNERS
        and owner(edge["target"]) in APP_OWNERS | {"shared"}
        and owner(edge["source"]) != owner(edge["target"])
    ]
    return {
        "version": 1,
        "revision": revision,
        "sources": list(sources.values()),
        "edges": edges,
        "cross_owner_edges": cross,
        "ambiguous_aliases": {
            name: choices for name, choices in index.items() if len(choices) > 1
        },
        "shared_candidates": sorted(candidates, key=lambda item: item["path"]),
        "candidate_closure_with_initializers": sorted(closure),
        "hazards": hazards,
        "consumers": consumers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Explicit additional repo-relative file",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if (repo / "src/agent/__init__.py").is_file() and not (
        repo / "src/__init__.py"
    ).is_file():
        parser.error(
            "census requires the legacy source layout; use --repo to select a "
            "frozen pre-move worktree instead of the flattened checkout"
        )
    for rel in args.include:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            parser.error("--include must stay inside the repository")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    result = census(repo, tracked_paths(repo) + args.include, revision)
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
