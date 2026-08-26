"""Legacy notification producer manifest — the "one fewer system" gate.

Re-runs ``scripts/check_notification_producers.py`` against the live
orchestrator tree and asserts the result matches the committed manifest at
``policy/notification_producers.txt``. Modelled on ``test_endpoint_inventory``.

The unified notification system (knowledge-base/knowledge/features/
unified_notification_system.md §6) is done only when this manifest is empty.
Until then two things must hold: the manifest reflects the code, and the
number of legacy call sites never goes up. Migrating a producer to
``notification_service.record()`` shrinks the list; run ``--write`` and commit.
Adding a new caller of a legacy path fails the second test on purpose.
"""

import difflib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_notification_producers.py"
MANIFEST = REPO_ROOT / "policy" / "notification_producers.txt"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "check_notification_producers", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_notification_producers"] = module
    spec.loader.exec_module(module)
    return module


def _manifest_sites() -> list[str]:
    return [
        line
        for line in MANIFEST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_manifest_matches_code():
    """The committed manifest must reflect the current tree."""
    script = _load_script()
    rendered = script.render_manifest(script.collect_sites())
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
            "Legacy notification producer manifest is stale. Run:\n\n"
            "    python scripts/check_notification_producers.py --write\n\n"
            "and commit the result. Diff:\n\n" + diff
        )


def test_legacy_call_sites_do_not_increase():
    """A new caller of a retired path is a regression, not a feature.

    Record it through ``notification_service.record()`` instead — every
    category, action and delivery rule lives in
    ``orchestrator/services/notification_catalog.py``.
    """
    script = _load_script()
    current = script.collect_sites()
    grandfathered = _manifest_sites()
    assert len(current) <= len(grandfathered), (
        f"Legacy notification call sites grew: {len(current)} now vs "
        f"{len(grandfathered)} grandfathered. Use notification_service.record() "
        "for new notifications; do not add callers of dispatch()/log_message "
        "outbound/the officer digest ring/the quiet-hours queue."
    )


def test_migrated_slice_one_paths_are_gone():
    """Slice 1 retired these; a reappearance means a producer regressed."""
    script = _load_script()
    kinds = {s.kind for s in script.collect_sites()}
    assert "notify_freeze" not in kinds, (
        "a _notify_operator_freeze caller lost its dedup_key"
    )
    manifest_text = MANIFEST.read_text()
    assert "_dispatch_officer_page  dispatch" not in manifest_text
    assert "_dispatch_officer_page  log_outbound" not in manifest_text
