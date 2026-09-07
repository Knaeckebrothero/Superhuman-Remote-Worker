#!/usr/bin/env python3
"""Apply a reviewed source-tree manifest, without moving/deleting source files.

Requires scripts/requirements-flatten.txt. Example:
  python scripts/flatten_source_tree.py --manifest map.json --source-root OLD \
      --repo NEW --write
  python scripts/verify_ast_equiv.py --manifest map.json --base REV --repo .

Manifest v1:
  {"version": 1, "module_map": {"src": "agent", "src.shared": "shared"},
   "literal_map": {}, "files": [{"old_path": "src/core/x.py",
     "new_path": "src/agent/core/x.py", "old_module": "src.core.x",
     "new_module": "agent.core.x", "source_sha256": "optional frozen hash",
     "module_map": {}, "literal_map": {},
     "replacements": [{"old": "exact transformed text", "new": "replacement",
                        "count": 1, "reason": "reviewed path-depth correction"}]}]}

The file map supplies exact module mappings; module_map adds package/symbol or
per-file flat-import mappings. Per-file mappings win. Names use longest-prefix
matching, never repeated cascading substitutions. Relative imports resolve from
the OLD package, then become absolute. Aliases retain their local bindings.
Quoted dotted module references are transformed separately; arbitrary prose,
filenames and embedded code are left alone unless explicitly listed/replaced.
Replacements apply AFTER mechanical rewriting and must match an exact count.

--write writes transformed files at new_path. Without it this is a dry run.
--cache-dir caches successful transformations, including during a dry run. Keys
cover the frozen source, effective manifest options, tool source and versions.
--verify compares each output AST against the declared transformation of the
frozen original, failing on unrelated changes. This is not an independent proof
of the codemod: executable, expected-output fixtures also validate its semantics.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

import libcst as cst
from libcst.helpers import get_full_name_for_node
from libcst.metadata import (
    MetadataWrapper,
    ParentNodeProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
    ScopeProvider,
)


_DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_FILE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".sql",
        ".json",
        ".yaml",
        ".yml",
        ".md",
        ".rst",
        ".txt",
        ".sh",
        ".html",
        ".htm",
        ".toml",
        ".cfg",
        ".ini",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".csv",
        ".xml",
        ".lock",
        ".env",
        ".ipynb",
        ".log",
        ".snap",
    }
)


def _safe_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"manifest path must be repo-relative: {raw!r}")
    return str(path)


def _module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute(module: str, is_package: bool, name: str, level: int) -> str:
    if not level:
        return name
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".") if package else []
    if level > len(parts):
        raise ValueError(f"relative import escapes {module!r}")
    return ".".join(parts[: len(parts) - level + 1] + ([name] if name else []))


@dataclass(frozen=True)
class FileEntry:
    old_path: str
    new_path: str
    old_module: str
    new_module: str
    options: dict[str, Any]

    @property
    def is_package(self) -> bool:
        return PurePosixPath(self.old_path).name == "__init__.py"


class Manifest:
    def __init__(self, data: dict[str, Any]):
        if data.get("version") != 1:
            raise ValueError("expected manifest version 1")
        self.files: list[FileEntry] = []
        self.module_map: dict[str, str] = dict(data.get("module_map", {}))
        self.literal_map: dict[str, str] = dict(data.get("literal_map", {}))
        destinations: set[str] = set()
        old_paths: set[str] = set()
        for raw in data.get("files", []):
            old_path, new_path = (
                _safe_path(raw["old_path"]),
                _safe_path(raw["new_path"]),
            )
            if old_path in old_paths or new_path in destinations:
                raise ValueError(
                    f"duplicate source/destination: {old_path} -> {new_path}"
                )
            old_paths.add(old_path)
            destinations.add(new_path)
            old_module = raw.get("old_module", _module(old_path))
            new_module = raw.get("new_module", _module(new_path.removeprefix("src/")))
            self.files.append(
                FileEntry(old_path, new_path, old_module, new_module, raw)
            )
            if (
                old_module in self.module_map
                and self.module_map[old_module] != new_module
            ):
                raise ValueError(f"conflicting module mapping: {old_module}")
            self.module_map[old_module] = new_module
        if not self.files:
            raise ValueError("manifest has no files")
        for old, new in self.module_map.items():
            if not _DOTTED.fullmatch(old) or not _DOTTED.fullmatch(new):
                raise ValueError(f"invalid dotted module mapping: {old!r} -> {new!r}")

    @classmethod
    def read(cls, path: Path) -> Manifest:
        return cls(json.loads(path.read_text()))


class NameMap:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.prefixes = sorted(mapping, key=lambda value: (-len(value), value))

    def match(self, name: str) -> str | None:
        return next(
            (old for old in self.prefixes if name == old or name.startswith(old + ".")),
            None,
        )

    def rename(self, name: str) -> str:
        old = self.match(name)
        return self.mapping[old] + name[len(old) :] if old is not None else name


class Rewrite(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (ParentNodeProvider,)

    def __init__(
        self,
        entry: FileEntry,
        names: NameMap,
        literals: dict[str, str],
        unaliased: dict[str, str],
    ):
        self.entry, self.names, self.literals = entry, names, literals
        self.unaliased = unaliased
        self.in_import = 0

    def visit_Import(self, node: cst.Import) -> None:
        self.in_import += 1
        for alias in node.names:
            name = get_full_name_for_node(alias.name)
            if name and alias.asname is None:
                renamed = self.names.rename(name)
                if renamed != name:
                    self.unaliased[name] = renamed

    def _is_canonical_root_import(self, assignment: Any, root: str) -> bool:
        node = getattr(assignment, "node", None)
        if isinstance(node, cst.Import):
            for alias in node.names:
                name = get_full_name_for_node(alias.name)
                if not name:
                    continue
                binding = (
                    alias.asname.name.value if alias.asname else name.split(".")[0]
                )
                target = self.names.rename(name)
                if binding == root and (
                    target == root
                    or (alias.asname is None and target.startswith(root + "."))
                ):
                    return True
        elif isinstance(node, cst.ImportFrom) and not isinstance(
            node.names, cst.ImportStar
        ):
            base = _absolute(
                self.entry.old_module,
                self.entry.is_package,
                get_full_name_for_node(node.module) or "",
                len(node.relative),
            )
            for alias in node.names:
                name = get_full_name_for_node(alias.name)
                binding = alias.asname.name.value if alias.asname else name
                if binding == root and self.names.rename(f"{base}.{name}") == root:
                    return True
        return False

    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.Import:
        self.in_import -= 1
        aliases = []
        for alias in original_node.names:
            name = get_full_name_for_node(alias.name)
            assert name
            renamed = self.names.rename(name)
            if renamed != name and alias.asname is None:
                old_root, new_root = name.split(".")[0], renamed.split(".")[0]
                scope = self.get_metadata(ScopeProvider, original_node)
                if (
                    old_root != new_root
                    and new_root in scope
                    and not all(
                        self._is_canonical_root_import(assignment, new_root)
                        for assignment in scope[new_root]
                    )
                ):
                    # Never silently introduce a binding that captures an
                    # existing local, parameter or unrelated imported name.
                    raise ValueError(
                        f"{self.entry.old_path}: import {name} introduces existing binding {new_root}; add an explicit alias in a reviewed preparation change"
                    )
            aliases.append(alias.with_changes(name=cst.parse_expression(renamed)))
        return updated_node.with_changes(names=aliases)

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self.in_import += 1

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.BaseSmallStatement | cst.FlattenSentinel[cst.BaseSmallStatement]:
        self.in_import -= 1
        old_base = _absolute(
            self.entry.old_module,
            self.entry.is_package,
            get_full_name_for_node(original_node.module) or "",
            len(original_node.relative),
        )
        if isinstance(original_node.names, cst.ImportStar):
            return updated_node.with_changes(
                module=cst.parse_expression(self.names.rename(old_base)), relative=()
            )
        groups: dict[str, list[cst.ImportAlias]] = defaultdict(list)
        for alias in original_node.names:
            old_name = get_full_name_for_node(alias.name)
            assert old_name
            full = self.names.rename(f"{old_base}.{old_name}")
            new_base, _, new_name = full.rpartition(".")
            asname = alias.asname
            if new_name != old_name and asname is None:
                asname = cst.AsName(cst.Name(old_name))
            groups[new_base].append(
                alias.with_changes(name=cst.Name(new_name), asname=asname)
            )
        statements: list[cst.BaseSmallStatement] = []
        for new_base, aliases in groups.items():
            if not original_node.lpar or not new_base:
                aliases[-1] = aliases[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
            if new_base:
                statement = updated_node.with_changes(
                    module=cst.parse_expression(new_base), relative=(), names=aliases
                )
            else:
                statement = cst.Import(names=aliases)
            statements.append(statement)
        if len(statements) == 1:
            return statements[0]
        return cst.FlattenSentinel(statements)

    def _rewrite_expression(
        self, original: cst.BaseExpression, updated: cst.BaseExpression
    ) -> cst.BaseExpression:
        if self.in_import or not self.unaliased:
            return updated
        parent = self.get_metadata(ParentNodeProvider, original, None)
        if isinstance(parent, cst.Attribute):
            return updated
        name = get_full_name_for_node(original)
        if not name:
            return updated
        prefix = next(
            (
                old
                for old in sorted(self.unaliased, key=len, reverse=True)
                if self.unaliased[old] != old
                and (name == old or name.startswith(old + "."))
            ),
            None,
        )
        if prefix is None:
            return updated
        # QualifiedNameProvider alone can report a global dotted import even
        # when a function parameter shadows its root. Resolve the actual root
        # binding as well (covered by an executable fixture).
        scope = self.get_metadata(ScopeProvider, original, None)
        # Keyword names and other syntax-only Names are not bound expressions.
        if scope is None:
            return updated
        assignments = scope[name.split(".")[0]]
        if not assignments or not any(
            isinstance(getattr(assignment, "node", None), cst.Import)
            for assignment in assignments
        ):
            return updated
        if any(
            not isinstance(getattr(assignment, "node", None), cst.Import)
            for assignment in assignments
        ):
            raise ValueError(
                f"{self.entry.old_path}: ambiguous reassigned import root in {name}"
            )
        qualified = self.get_metadata(QualifiedNameProvider, original, set())
        if not any(
            item.source == QualifiedNameSource.IMPORT and item.name == name
            for item in qualified
        ):
            return updated
        renamed = self.unaliased[prefix] + name[len(prefix) :]
        return cst.parse_expression(renamed) if renamed != name else updated

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        return self._rewrite_expression(original_node, updated_node)

    def leave_Name(
        self, original_node: cst.Name, updated_node: cst.Name
    ) -> cst.BaseExpression:
        return self._rewrite_expression(original_node, updated_node)

    def _module_string_context(self, node: cst.SimpleString) -> bool:
        # Bare module keys such as "main" are only rewritten where the syntax
        # identifies a module reference; ordinary product strings stay intact.
        parent = self.get_metadata(ParentNodeProvider, node, None)
        if isinstance(parent, cst.Arg):
            call = self.get_metadata(ParentNodeProvider, parent, None)
            if not isinstance(call, cst.Call):
                return False
            name = get_full_name_for_node(call.func) or ""
            if name == "__import__" or name.rsplit(".", 1)[-1] == "import_module":
                keyword = "name"
            elif name in {
                "sys.modules.get",
                "sys.modules.pop",
                "sys.modules.setdefault",
            }:
                keyword = "key"
            else:
                return False
            return (parent.keyword is not None and parent.keyword.value == keyword) or (
                parent.keyword is None and call.args[0] is parent
            )
        if isinstance(parent, cst.Index):
            element = self.get_metadata(ParentNodeProvider, parent, None)
            subscript = self.get_metadata(ParentNodeProvider, element, None)
            return isinstance(subscript, cst.Subscript) and (
                get_full_name_for_node(subscript.value) == "sys.modules"
            )
        return False

    def _lazy_module_string_context(self, node: cst.SimpleString) -> bool:
        # The three existing lazy package initializers declare _LAZY_IMPORTS
        # as {export_name: (module_path, attribute_name)}. Only the first tuple
        # value is a module reference; keys, attributes and other tables are not.
        element = self.get_metadata(ParentNodeProvider, node, None)
        if not isinstance(element, cst.Element):
            return False
        pair = self.get_metadata(ParentNodeProvider, element, None)
        if not (
            isinstance(pair, cst.Tuple)
            and len(pair.elements) == 2
            and pair.elements[0] is element
        ):
            return False
        item = self.get_metadata(ParentNodeProvider, pair, None)
        if not isinstance(item, cst.DictElement) or item.value is not pair:
            return False
        table = self.get_metadata(ParentNodeProvider, item, None)
        if not isinstance(table, cst.Dict):
            return False
        declaration = self.get_metadata(ParentNodeProvider, table, None)
        if isinstance(declaration, cst.AnnAssign):
            targets = [declaration.target]
        elif isinstance(declaration, cst.Assign):
            targets = [target.target for target in declaration.targets]
        else:
            return False
        return any(
            isinstance(target, cst.Name) and target.value == "_LAZY_IMPORTS"
            for target in targets
        )

    def leave_SimpleString(
        self, original_node: cst.SimpleString, updated_node: cst.SimpleString
    ) -> cst.SimpleString:
        value = original_node.evaluated_value
        if not isinstance(value, str):
            return updated_node
        if value in self.literals:
            renamed = self.literals[value]
        elif PurePosixPath(
            value
        ).suffix.lower() in _FILE_SUFFIXES and not self._module_string_context(
            original_node
        ):
            # A flat module alias (main/run/controller) must not turn a source
            # filename such as "main.py" into a dotted Python module path.
            # Reviewed file moves belong in literal_map, which takes precedence.
            return updated_node
        elif value.startswith(".") and _DOTTED.fullmatch(value.lstrip(".")):
            if not (
                self._module_string_context(original_node)
                or self._lazy_module_string_context(original_node)
            ):
                return updated_node
            level = len(value) - len(value.lstrip("."))
            try:
                absolute = _absolute(
                    self.entry.old_module, self.entry.is_package, value[level:], level
                )
            except ValueError:
                return updated_node
            prefix = self.names.match(absolute)
            package = (
                self.entry.old_module
                if self.entry.is_package
                else self.entry.old_module.rpartition(".")[0]
            )
            # Resolve only a module explicitly covered by the reviewed map.
            if prefix is None or len(prefix.split(".")) <= len(package.split(".")):
                return updated_node
            renamed = self.names.rename(absolute)
        elif _DOTTED.fullmatch(value) and (
            "." in value or self._module_string_context(original_node)
        ):
            prefix = self.names.match(value)
            if prefix in {"src", "orchestrator"} and value != prefix:
                return updated_node
            renamed = self.names.rename(value)
        else:
            return updated_node
        if renamed == value:
            return updated_node
        # Reuse ordinary quote style when possible; repr handles escaping and
        # strips raw prefixes whose semantics may change with replacement text.
        quote = original_node.quote
        if (
            len(quote) == 1
            and quote not in renamed
            and "\\" not in renamed
            and "\n" not in renamed
        ):
            return updated_node.with_changes(value=f"{quote}{renamed}{quote}")
        return updated_node.with_changes(value=repr(renamed))


class ScopedRewrite(Rewrite):
    # from-imports keep their bound names and strings only need parents. Most
    # large application modules have no renamed unaliased import expressions;
    # generating scope/qualified-name metadata for them is unnecessary.
    METADATA_DEPENDENCIES = (ParentNodeProvider, QualifiedNameProvider, ScopeProvider)

    @classmethod
    def get_inherited_dependencies(cls):
        # LibCST's class cache can otherwise inherit the previously populated
        # smaller dependency set from Rewrite when both variants run together.
        return cls.METADATA_DEPENDENCIES


def transform_source(source: str, entry: FileEntry, manifest: Manifest) -> str:
    expected_hash = entry.options.get("source_sha256")
    if expected_hash and hashlib.sha256(source.encode()).hexdigest() != expected_hash:
        raise ValueError(f"{entry.old_path}: frozen source hash does not match")
    names = NameMap({**manifest.module_map, **entry.options.get("module_map", {})})
    literals = {**manifest.literal_map, **entry.options.get("literal_map", {})}
    # Collect first: a function can refer to an import appearing later in the
    # source file. Scope metadata still decides whether a particular use is the
    # imported object or a locally shadowed name.
    unaliased = {
        alias.name: names.rename(alias.name)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.asname is None and names.rename(alias.name) != alias.name
    }
    wrapper = MetadataWrapper(cst.parse_module(source), unsafe_skip_copy=True)
    transformer = ScopedRewrite if unaliased else Rewrite
    output = wrapper.visit(transformer(entry, names, literals, unaliased)).code
    for replacement in entry.options.get("replacements", []):
        old, new = replacement["old"], replacement["new"]
        count = replacement.get("count", 1)
        if (
            not old
            or not replacement.get("reason")
            or not isinstance(count, int)
            or count < 1
        ):
            raise ValueError(
                f"{entry.old_path}: bounded replacement needs old text, reason and positive count"
            )
        if output.count(old) != count:
            raise ValueError(
                f"{entry.old_path}: replacement expected {count} occurrence(s), found {output.count(old)}: {replacement['reason']}"
            )
        output = output.replace(old, new)
    ast.parse(output, filename=entry.new_path)
    return output


class TransformCache:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.fingerprint = {
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "libcst": version("libcst"),
            "python": list(sys.version_info[:3]),
        }

    def transform(self, source: str, entry: FileEntry, manifest: Manifest) -> str:
        inputs = {
            "format": 1,
            "tool": self.fingerprint,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "old_module": entry.old_module,
            "new_module": entry.new_module,
            "is_package": entry.is_package,
            "options": entry.options,
            "module_map": {
                **manifest.module_map,
                **entry.options.get("module_map", {}),
            },
            "literal_map": {
                **manifest.literal_map,
                **entry.options.get("literal_map", {}),
            },
        }
        key = hashlib.sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        path = self.directory / f"{key}.json"
        try:
            cached = json.loads(path.read_text())
            output = cached["output"]
            if (
                cached["key"] == key
                and isinstance(output, str)
                and cached["output_sha256"]
                == hashlib.sha256(output.encode()).hexdigest()
            ):
                self.hits += 1
                return output
        except (OSError, ValueError, KeyError, TypeError):
            pass
        output = transform_source(source, entry, manifest)
        payload = {
            "key": key,
            "output": output,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        }
        # A killed run or concurrent reader must never observe half a cache
        # record. Only this explicitly selected cache directory is mutated.
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.directory, prefix=".flatten-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, sort_keys=True)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return output


def ast_equivalent(expected: str, actual: str) -> bool:
    return ast.dump(ast.parse(expected), include_attributes=False) == ast.dump(
        ast.parse(actual), include_attributes=False
    )


def _read_source(entry: FileEntry, source_root: Path, base: str | None) -> str:
    if base:
        return subprocess.check_output(
            ["git", "show", f"{base}:{entry.old_path}"], cwd=source_root
        ).decode()
    return (source_root / entry.old_path).read_text()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--cache-dir", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source-root", type=Path)
    source.add_argument("--base", help="Read frozen old sources from this Git revision")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true")
    action.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    manifest = Manifest.read(args.manifest)
    repo = args.repo.resolve()
    source_root = args.source_root.resolve() if args.source_root else repo
    cache = TransformCache(args.cache_dir.resolve()) if args.cache_dir else None
    started = time.monotonic()
    print(
        f"Preparing {len(manifest.files)} manifest files", file=sys.stderr, flush=True
    )
    # Calculate every result first: a parse/replacement failure must not leave
    # a partially rewritten tree. This command never deletes or moves sources.
    outputs: list[tuple[FileEntry, str]] = []
    errors = []
    for index, entry in enumerate(manifest.files, 1):
        try:
            original = _read_source(entry, source_root, args.base)
            if len(original) >= 100_000:
                print(
                    f"[{index}/{len(manifest.files)}] {entry.old_path} ({len(original) // 1024} KiB)",
                    file=sys.stderr,
                    flush=True,
                )
            expected = (
                cache.transform(original, entry, manifest)
                if cache
                else transform_source(original, entry, manifest)
            )
            if args.verify and not ast_equivalent(
                expected, (repo / entry.new_path).read_text()
            ):
                errors.append(
                    f"{entry.new_path}: AST differs from declared transformation"
                )
            outputs.append((entry, expected))
        except (
            ValueError,
            SyntaxError,
            OSError,
            subprocess.CalledProcessError,
            cst.ParserSyntaxError,
        ) as exc:
            errors.append(f"{entry.old_path}: {exc}")
            print(errors[-1], file=sys.stderr, flush=True)
        if index % 100 == 0 or index == len(manifest.files):
            print(
                f"Prepared {index}/{len(manifest.files)} files in {time.monotonic() - started:.1f}s"
                f" ({cache.hits if cache else 0} cached, {len(errors)} errors)",
                file=sys.stderr,
                flush=True,
            )
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    if args.write:
        for entry, output in outputs:
            destination = repo / entry.new_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(output)
    print(
        f"{'Verified' if args.verify else 'Wrote' if args.write else 'Prepared'} {len(outputs)} manifest files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
