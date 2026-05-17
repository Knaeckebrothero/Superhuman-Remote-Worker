"""C2 — endpoint inventory snapshot test.

Re-runs ``scripts/check_endpoint_auth.py`` against the live ``orchestrator/main.py``
and asserts the result matches the committed manifest at
``docs/security/endpoint_inventory.txt``.

When a new endpoint is added (or an existing endpoint's gate changes), this
test fails until either:

  * a gate is added (``Depends(require_*)`` or an in-body ``require_*`` call),
    and ``python scripts/check_endpoint_auth.py --write`` is re-run; **or**
  * the endpoint is deliberately public, in which case mark it with
    ``# nosec: public <reason>`` on the line immediately above the
    ``@app.METHOD(...)`` decorator, then re-run ``--write``.

The point is to make adding an unscoped endpoint an explicit, visible choice
rather than something that ships without review.
"""

import difflib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_endpoint_auth.py"
MANIFEST = REPO_ROOT / "docs" / "security" / "endpoint_inventory.txt"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_endpoint_auth", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_endpoint_auth"] = module
    spec.loader.exec_module(module)
    return module


def test_endpoint_inventory_matches_manifest():
    """The committed manifest must reflect the current state of main.py."""
    script = _load_script()
    endpoints = script.collect_endpoints()
    rendered = script.render_manifest(endpoints)
    on_disk = MANIFEST.read_text()
    if rendered != on_disk:
        diff = "\n".join(
            difflib.unified_diff(
                on_disk.splitlines(),
                rendered.splitlines(),
                fromfile=str(MANIFEST.relative_to(REPO_ROOT)),
                tofile="<regenerated>",
                lineterm="",
            )
        )
        pytest.fail(
            "Endpoint inventory is stale. Run:\n\n"
            "    python scripts/check_endpoint_auth.py --write\n\n"
            "and commit the result. Diff:\n\n" + diff
        )


def test_no_new_unscoped_endpoints():
    """The number of unscoped endpoints must not increase.

    The current backlog of ~55 unscoped endpoints is grandfathered as
    follow-up work (see docs/multi_tenancy.md P3 notes). New unscoped
    endpoints have to be acknowledged either by adding a gate or by
    marking them ``# nosec: public <reason>`` above the decorator.
    """
    script = _load_script()
    endpoints = script.collect_endpoints()
    manifest_unscoped = [
        line for line in MANIFEST.read_text().splitlines() if line.endswith("unscoped")
    ]
    current_unscoped = [e for e in endpoints if e.classification == "unscoped"]
    assert len(current_unscoped) <= len(manifest_unscoped), (
        f"New unscoped endpoint(s) added: {len(current_unscoped)} now vs "
        f"{len(manifest_unscoped)} grandfathered. Gate the new endpoint(s) "
        "with `Depends(require_*)` / in-body `require_*`, or annotate with "
        "`# nosec: public <reason>` above the decorator."
    )
