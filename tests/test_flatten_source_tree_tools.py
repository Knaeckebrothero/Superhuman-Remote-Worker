"""Behavioral fixtures for the one-time source-tree migration tools.

Can run before installing the application: pytest --noconftest THIS_FILE.
"""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


flatten = _load("_srw_flatten_tool", ROOT / "scripts/flatten_source_tree.py")
census = _load("_srw_flatten_census", ROOT / "scripts/flatten_census.py")


def _manifest(**options):
    return flatten.Manifest(
        {
            "version": 1,
            "module_map": {
                "src": "agent",
                "src.shared": "shared",
                "src.core": "agent.core",
                "src.core.loader": "shared.runtime.loader",
                "src.core.state": "agent.core.state",
            },
            "files": [
                {
                    "old_path": "src/core/sample.py",
                    "new_path": "src/agent/core/sample.py",
                    "old_module": "src.core.sample",
                    "new_module": "agent.core.sample",
                    **options,
                }
            ],
        }
    )


def rewrite(source, **options):
    manifest = _manifest(**options)
    return flatten.transform_source(source, manifest.files[0], manifest)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "from src.shared.contract import Contract\n",
            "from shared.contract import Contract\n",
        ),
        (
            "from ..shared.contract import Contract\n",
            "from shared.contract import Contract\n",
        ),
        (
            "from .loader import Config as C\n",
            "from shared.runtime.loader import Config as C\n",
        ),
        ("from . import loader\n", "from shared.runtime import loader\n"),
        ("from src import shared\n", "import shared\n"),
        (
            "from src.core import loader, state\n",
            "from shared.runtime import loader; from agent.core import state\n",
        ),
        (
            "import src.core.loader as loader\nvalue = loader.Config\n",
            "import shared.runtime.loader as loader\nvalue = loader.Config\n",
        ),
        (
            "import src.core.loader\nvalue = src.core.loader.Config\n",
            "import shared.runtime.loader\nvalue = shared.runtime.loader.Config\n",
        ),
        (
            "def f():\n    return src.core.loader.Config\nimport src.core.loader\n",
            "def f():\n    return shared.runtime.loader.Config\nimport shared.runtime.loader\n",
        ),
        (
            "if TYPE_CHECKING:\n    from src.core.loader import Config\n",
            "if TYPE_CHECKING:\n    from shared.runtime.loader import Config\n",
        ),
        (
            "def f():\n    from src.core.loader import Config\n    return Config\n",
            "def f():\n    from shared.runtime.loader import Config\n    return Config\n",
        ),
    ],
)
def test_import_forms_preserve_binding_and_execution_location(before, after):
    assert flatten.ast_equivalent(rewrite(before), after)


def test_shadowed_local_and_explicit_aliases_keep_their_meaning(monkeypatch):
    before = """import src.core.loader
import src.core.loader as my_loader
def local(src):
    return src.core.loader.VALUE
result = src.core.loader.VALUE + my_loader.VALUE
"""
    rewritten = rewrite(before)
    assert "return src.core.loader.VALUE" in rewritten
    shared = ModuleType("shared")
    runtime = ModuleType("shared.runtime")
    loader = ModuleType("shared.runtime.loader")
    loader.VALUE = 21
    shared.runtime = runtime
    runtime.loader = loader
    for module in (shared, runtime, loader):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = {}
    exec(rewritten, namespace)
    assert namespace["result"] == 42
    assert (
        namespace["local"](
            SimpleNamespace(core=SimpleNamespace(loader=SimpleNamespace(VALUE=7)))
        )
        == 7
    )


def test_new_import_root_cannot_capture_an_existing_local():
    with pytest.raises(ValueError, match="existing binding shared"):
        rewrite(
            "shared = object()\nimport src.core.loader\nx = src.core.loader.Config\n"
        )


def test_keyword_names_are_not_imported_expressions():
    result = rewrite(
        'import src\ncall(src="unchanged keyword", component="agent", value=src.core.loader)\n'
    )
    assert flatten.ast_equivalent(
        result,
        'import agent\ncall(src="unchanged keyword", component="agent", value=agent.core.loader)\n',
    )


def test_existing_canonical_import_roots_are_compatible():
    result = rewrite(
        "import orchestrator.services\nimport main\nx = main.VALUE\n",
        module_map={"main": "orchestrator.main"},
    )
    assert flatten.ast_equivalent(
        result,
        "import orchestrator.services\nimport orchestrator.main\nx = orchestrator.main.VALUE\n",
    )
    with pytest.raises(ValueError, match="existing binding orchestrator"):
        rewrite(
            "import unrelated as orchestrator\nimport main\nx = main.VALUE\n",
            module_map={"main": "orchestrator.main"},
        )


def test_unrelated_reassigned_import_does_not_trigger_migration_collision():
    result = rewrite("import json\njson = 4\nresult = json\n")
    assert flatten.ast_equivalent(result, "import json\njson = 4\nresult = json\n")


def test_from_imports_and_strings_need_no_scope_metadata(monkeypatch):
    def unexpected_scope(_self, _node):
        raise AssertionError("scope analysis was unnecessary for this source")

    monkeypatch.setattr(flatten.ScopeProvider, "visit_Module", unexpected_scope)
    result = rewrite(
        "from .loader import Config\nimport src.core.loader as loader\n"
        'import json\njson = 4\ntarget = "src.core.loader.Config"\n'
        "def f():\n    return loader.Config, Config, json\n"
    )
    assert flatten.ast_equivalent(
        result,
        "from shared.runtime.loader import Config\nimport shared.runtime.loader as loader\n"
        'import json\njson = 4\ntarget = "shared.runtime.loader.Config"\n'
        "def f():\n    return loader.Config, Config, json\n",
    )


def test_cache_reuses_output_but_rejects_source_manifest_and_tool_drift(
    tmp_path, monkeypatch
):
    cache = flatten.TransformCache(tmp_path / "cache")
    original = flatten.transform_source
    calls = []

    def track(source, entry, manifest):
        calls.append(source)
        return original(source, entry, manifest)

    monkeypatch.setattr(flatten, "transform_source", track)
    manifest = _manifest()
    source = "from src.core.loader import Config\nVALUE = 1\n"
    first = cache.transform(source, manifest.files[0], manifest)
    assert cache.transform(source, manifest.files[0], manifest) == first
    assert len(calls) == 1 and cache.hits == 1
    # A broken/incomplete cache record cannot be used to approve changed code.
    cache_file = next(cache.directory.glob("*.json"))
    payload = json.loads(cache_file.read_text())
    payload["output"] += "UNREVIEWED = True\n"
    cache_file.write_text(json.dumps(payload))
    assert cache.transform(source, manifest.files[0], manifest) == first
    assert len(calls) == 2
    cache.transform(source.replace("1", "2"), manifest.files[0], manifest)
    assert len(calls) == 3
    changed = _manifest(module_map={"src.core.loader": "shared.other_loader"})
    assert "shared.other_loader" in cache.transform(source, changed.files[0], changed)
    assert len(calls) == 4
    cache.fingerprint["tool_sha256"] = "a different tool revision"
    cache.transform(source, manifest.files[0], manifest)
    assert len(calls) == 5
    changed = _manifest(source_sha256="incorrect frozen source hash")
    with pytest.raises(ValueError, match="frozen source hash"):
        cache.transform(source, changed.files[0], changed)
    assert not list(cache.directory.glob(".flatten-*"))


def test_comments_parentheses_and_mixed_destination_imports_survive():
    before = """# source import rationale
from src.core import (
    loader,  # expensive config
    state as state_module,  # graph state
)
"""
    after = rewrite(before)
    ast.parse(after)
    for comment in ("# source import rationale", "# expensive config", "# graph state"):
        assert after.count(comment) == 1
    assert flatten.ast_equivalent(
        after,
        "from shared.runtime import loader\nfrom agent.core import state as state_module\n",
    )


def test_quoted_module_refs_are_distinct_from_paths_and_prose():
    source = '''"""Do not change prose about src.core.loader."""
target = "src.core.loader.Config"
lazy = ".loader"
extension = ".json"
filename = "src.txt"
component = "src"
path = "src/core/loader.py"
'''
    output = rewrite(
        source, literal_map={"src/core/loader.py": "src/shared/runtime/loader.py"}
    )
    namespace = {}
    exec(output, namespace)
    assert namespace["target"] == "shared.runtime.loader.Config"
    assert namespace["lazy"] == ".loader"
    assert namespace["filename"] == "src.txt"
    assert namespace["extension"] == ".json"
    assert namespace["component"] == "src"
    assert namespace["path"] == "src/shared/runtime/loader.py"
    assert namespace["__doc__"] == "Do not change prose about src.core.loader."


def test_relative_data_strings_stay_literal_even_when_matching_known_modules():
    source = """extension = ".pdf"
if suffix == ".pdf":
    kind = "pdf"
ordinary = ".loader"
unrelated_table = {"Config": (".loader", "Config")}
_LAZY_IMPORTS = {".loader": (".state", ".loader")}
module = importlib.import_module("external", package=".loader")
fallback = sys.modules.get("external", ".loader")
other = my_import_module(".loader")
"""
    output = rewrite(source, module_map={"src.core.pdf": "agent.core.pdf"})
    assert flatten.ast_equivalent(
        output,
        source.replace('(".state", ".loader")', '("agent.core.state", ".loader")'),
    )


@pytest.mark.parametrize("annotated", [False, True])
def test_relative_lazy_exports_and_import_module_keep_value_identity(
    monkeypatch, annotated
):
    declaration = "_LAZY_IMPORTS"
    if annotated:
        declaration += ": dict[str, tuple[str, str]]"
    source = f"""import importlib
{declaration} = {{"Config": (".loader", "Config")}}
def __getattr__(name):
    module_path, attribute = _LAZY_IMPORTS[name]
    return getattr(importlib.import_module(module_path, package=__name__), attribute)
direct = importlib.import_module(".loader", package=__name__).Config
keyword = importlib.import_module(name=".loader", package=__name__).Config
"""
    output = rewrite(
        source,
        old_path="src/core/__init__.py",
        old_module="src.core",
        new_path="src/agent/core/__init__.py",
        new_module="agent.core",
    )
    loader = ModuleType("shared.runtime.loader")
    loader.Config = object()
    monkeypatch.setitem(sys.modules, loader.__name__, loader)
    namespace = {"__name__": "agent.core"}
    exec(output, namespace)
    assert namespace["_LAZY_IMPORTS"] == {"Config": (loader.__name__, "Config")}
    assert namespace["__getattr__"]("Config") is loader.Config
    assert namespace["direct"] is loader.Config
    assert namespace["keyword"] is loader.Config


def test_flat_names_are_scoped_and_mcp_sdk_stays_separate():
    source = 'import mcp\nimport importlib\nimport sys\nmod = importlib.import_module("main")\nsys.modules["main"] = mod\nkind = "main"\n'
    output = rewrite(source, module_map={"main": "orchestrator.main"})
    assert "import mcp\n" in output
    assert 'import_module("orchestrator.main")' in output
    assert 'sys.modules["orchestrator.main"]' in output
    assert 'kind = "main"' in output


def test_flat_module_aliases_do_not_rewrite_source_filenames():
    source = (
        'script = "run.py"\ncontroller = "controller.py"\nschema = "main.sql"\n'
        'path = "src.core.loader.py"\nmodule = import_module("run.entry")\n'
        'explicit = "main.py"\n'
    )
    output = rewrite(
        source,
        module_map={
            "run": "mcp_server.run",
            "main": "orchestrator.main",
            "controller": "vm_controller.controller",
        },
        literal_map={"main.py": "src/orchestrator/main.py"},
    )
    assert flatten.ast_equivalent(
        output,
        source.replace('"run.entry"', '"mcp_server.run.entry"').replace(
            '"main.py"', '"src/orchestrator/main.py"'
        ),
    )


def test_exact_symbol_move_preserves_original_bound_name():
    result = rewrite(
        "from src.core.loader import OldName\nvalue = OldName\n",
        module_map={"src.core.loader.OldName": "shared.runtime.loader.NewName"},
    )
    assert flatten.ast_equivalent(
        result,
        "from shared.runtime.loader import NewName as OldName\nvalue = OldName\n",
    )


def test_bounded_exception_and_source_hash_reject_drift():
    before = "ROOT = path.parents[2]\n"
    replacement = {
        "old": "path.parents[2]",
        "new": "path.parents[3]",
        "count": 1,
        "reason": "one added source directory",
    }
    output = rewrite(
        before,
        replacements=[replacement],
        source_sha256=hashlib.sha256(before.encode()).hexdigest(),
    )
    assert flatten.ast_equivalent(output, "ROOT = path.parents[3]\n")
    with pytest.raises(ValueError, match="frozen source hash"):
        rewrite(
            before + "OTHER = 1\n",
            source_sha256=hashlib.sha256(before.encode()).hexdigest(),
        )
    with pytest.raises(ValueError, match="expected 1 occurrence"):
        rewrite(before * 2, replacements=[replacement])
    with pytest.raises(ValueError, match="needs old text, reason"):
        rewrite(before, replacements=[{**replacement, "reason": ""}])
    assert not flatten.ast_equivalent(output, output + "SIDE_EFFECT = True\n")


def test_manifest_rejects_path_escape_duplicate_destinations_and_empty_scope():
    with pytest.raises(ValueError, match="repo-relative"):
        _manifest(new_path="../outside.py")
    with pytest.raises(ValueError, match="no files"):
        flatten.Manifest({"version": 1, "files": []})
    with pytest.raises(ValueError, match="duplicate"):
        flatten.Manifest(
            {
                "version": 1,
                "files": [
                    {"old_path": "a.py", "new_path": "b.py"},
                    {"old_path": "c.py", "new_path": "b.py"},
                ],
            }
        )


def test_cli_dry_run_write_verify_and_unrelated_edit(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    (old / "src/core").mkdir(parents=True)
    (old / "src/core/sample.py").write_text(
        "from src.core.loader import Config\nresult = 42\n"
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "module_map": {"src.core.loader": "shared.runtime.loader"},
                "files": [
                    {
                        "old_path": "src/core/sample.py",
                        "new_path": "src/agent/core/sample.py",
                        "old_module": "src.core.sample",
                        "new_module": "agent.core.sample",
                    }
                ],
            }
        )
    )
    arguments = [
        "--manifest",
        str(manifest),
        "--source-root",
        str(old),
        "--repo",
        str(new),
        "--cache-dir",
        str(tmp_path / "cache"),
    ]
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/flatten_source_tree.py"), *arguments],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not new.exists()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/flatten_source_tree.py"),
            *arguments,
            "--write",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "1 cached" in result.stderr
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_ast_equiv.py"), *arguments],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    destination = new / "src/agent/core/sample.py"
    destination.write_text(destination.read_text().replace("42", "43"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_ast_equiv.py"), *arguments],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "AST differs" in result.stderr


def test_census_handles_initializers_deferred_type_only_and_cross_app(tmp_path):
    content = {
        "src/__init__.py": "",
        "src/core/__init__.py": "from .heavy import Client\n",
        "src/core/heavy.py": "import langgraph\nClient = object\n",
        "src/core/model.py": "VALUE = 1\n",
        "src/shared/__init__.py": "",
        "src/shared/contract.py": "VALUE = 2\n",
        "orchestrator/__init__.py": "",
        "orchestrator/main.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from src.shared.contract import VALUE\ndef lazy():\n    from src.core.model import VALUE\n    return VALUE\n",
        "orchestrator/mcp/__init__.py": "",
        "orchestrator/mcp/server.py": "import mcp.types\n",
        "docker/Dockerfile.agent": "COPY src/ ./src/\n",
    }
    for name, source in content.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    result = census.census(tmp_path, list(content), "fixture")
    assert result == census.census(tmp_path, list(reversed(content)), "fixture")
    edge = next(
        item
        for item in result["edges"]
        if item["source"] == "orchestrator/main.py"
        and item["target"] == "src/core/model.py"
    )
    assert edge["deferred"] is True
    typed = next(
        item for item in result["edges"] if item["target"] == "src/shared/contract.py"
    )
    assert typed["type_checking"] is True
    assert "src/core/heavy.py" in result["candidate_closure_with_initializers"]
    assert not any(
        item["source"] == "orchestrator/mcp/server.py"
        and item["kind"] != "parent-initializer"
        for item in result["edges"]
    )
    assert result["consumers"] == [{"path": "docker/Dockerfile.agent", "line": 1}]


def test_package_relative_resolution_does_not_drop_the_package_name():
    assert census.absolute_from("src.core", True, "model", 1) == "src.core.model"
    assert (
        census.absolute_from("src.core.x", False, "shared.contract", 2)
        == "src.shared.contract"
    )
    with pytest.raises(ValueError, match="escapes"):
        census.absolute_from("entry", False, "other", 1)


def test_census_cli_rejects_flattened_layout_before_producing_evidence(tmp_path):
    package = tmp_path / "src/agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    output = tmp_path / "census.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/flatten_census.py"),
            "--repo",
            str(tmp_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "legacy source layout" in result.stderr
    assert "--repo" in result.stderr and "frozen pre-move worktree" in result.stderr
    assert result.stdout == ""
    assert not output.exists()
