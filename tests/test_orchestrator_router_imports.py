"""Canonical domain imports stay inert; legacy router exports retain identity."""

import subprocess
import sys

import pytest


def run_fresh(tmp_path, code):
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "orchestrator.routers",
        "orchestrator.routers.tables",
        "orchestrator.routers.contacts",
    ],
)
def test_light_domain_import_avoids_unrelated_router_and_provider_startup(
    tmp_path, module
):
    run_fresh(
        tmp_path,
        f"""
import importlib
import importlib.abc
import sys

forbidden = (
    "orchestrator.main", "agent", "langgraph", "langchain", "langchain_core",
    "langchain_openai", "shared.runtime.core.loader",
    "orchestrator.routers.automations", "orchestrator.routers.canvases",
    "orchestrator.routers.project_loops", "orchestrator.routers.product_capabilities",
    "orchestrator.routers.shared_browser", "orchestrator.routers.wopi",
    "orchestrator.routers.vm_guest",
)

def blocked(name):
    return any(name == root or name.startswith(root + ".") for root in forbidden)

assert not [name for name in sys.modules if blocked(name)]

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if blocked(fullname):
            raise AssertionError("Unexpected unrelated import: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
importlib.import_module({module!r})
from orchestrator import routers
assert set(routers.__all__) <= set(dir(routers))
assert not [name for name in sys.modules if blocked(name)]
if {module!r} == "orchestrator.routers":
    assert "fastapi" not in sys.modules
""",
    )


def test_one_legacy_export_loads_only_its_domain_and_caches_identity(tmp_path):
    run_fresh(
        tmp_path,
        """
import importlib.abc
import sys
from orchestrator import routers

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"orchestrator.routers.automations", "shared.runtime.core.loader"}:
            raise AssertionError("An unrelated export was loaded: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
assert "vm_guest_router" not in vars(routers)
from orchestrator.routers import vm_guest_router
from orchestrator.routers.vm_guest import router
assert vm_guest_router is router is routers.vm_guest_router
assert vars(routers)["vm_guest_router"] is router
assert "automations_router" not in vars(routers)
assert "orchestrator.routers.automations" not in sys.modules
assert "shared.runtime.core.loader" not in sys.modules
""",
    )


def test_all_legacy_exports_star_dir_and_submodule_fallback_keep_identity(tmp_path):
    run_fresh(
        tmp_path,
        """
from orchestrator import routers

namespace = {}
exec("from orchestrator.routers import *", namespace)
from orchestrator.routers import (
    automations, canvases, project_loops, product_capabilities,
    shared_browser, wopi, vm_guest, tables, contacts,
)
expected = {
    "automations_router": automations.router,
    "canvases_router": canvases.router,
    "internal_canvases_router": canvases.internal_router,
    "project_loops_router": project_loops.router,
    "product_capabilities_router": product_capabilities.router,
    "shared_browser_router": shared_browser.router,
    "wopi_router": wopi.router,
    "vm_guest_router": vm_guest.router,
}
assert list(expected) == routers.__all__
assert set(expected) <= set(dir(routers))
for name, value in expected.items():
    assert namespace[name] is getattr(routers, name) is value
    assert vars(routers)[name] is value
assert tables.__name__ == "orchestrator.routers.tables"
assert contacts.__name__ == "orchestrator.routers.contacts"
assert routers.tables is tables
assert routers.contacts is contacts
assert "orchestrator.main" not in __import__("sys").modules
try:
    routers.not_an_export
except AttributeError as error:
    assert str(error) == "module 'orchestrator.routers' has no attribute 'not_an_export'"
else:
    raise AssertionError("Unknown package attribute was accepted")
""",
    )
