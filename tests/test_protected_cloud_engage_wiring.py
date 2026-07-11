"""Unit tests for protected-cloud engage wiring in ``orchestrator/main.py``.

Task B8 ties Slice A's fail-closed ``engage_ro_mount`` gate to thread create:
a protected thread with a Nextcloud project mount gets engaged once, and a
refusal is recorded on the thread's metadata WITHOUT raising — the session
must still boot (with no cloud mount), never fall back to a live mount.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import main

# NOTE: import via the bare ``services.cloud...`` path, matching main.py's own
# import (``from services.cloud.ro_engage import ...`` — see main.py's cloud
# imports and the module-identity note in the docstring below). ``orchestrator``
# is on sys.path as its own root (conftest.py) AND importable as a package
# (``orchestrator.services.cloud.ro_engage``), so the two import spellings
# resolve to two DIFFERENT module objects with two different (non-`is`-equal)
# ``RoEngageRefused`` classes. Importing the "orchestrator."-prefixed spelling
# here made ``except RoEngageRefused`` in main.py never match the instance this
# test's side_effect raises, silently mis-testing the refusal path (it fell
# through to the generic ``except Exception`` branch instead).
from services.cloud.ro_engage import RoEngageRefused


@pytest.mark.asyncio
async def test_engage_called_for_protected_thread_with_project_mount():
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]
    with patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True), \
         patch.object(main, "engage_ro_mount", new=AsyncMock()) as engage, \
         patch.object(main.main_cloud_router, "for_backend") as for_backend:
        for_backend.return_value = object()
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )
    engage.assert_awaited_once()


@pytest.mark.asyncio
async def test_engage_refusal_records_error_and_does_not_raise():
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]
    recorded: list[str] = []
    with patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True), \
         patch.object(main, "engage_ro_mount", new=AsyncMock(side_effect=RoEngageRefused("floor"))), \
         patch.object(main.main_cloud_router, "for_backend", return_value=object()), \
         patch.object(
             main, "_record_protected_error",
             new=AsyncMock(side_effect=lambda tid, msg: recorded.append(msg)),
         ):
        # must NOT raise — a refusal is recorded, the session boots with no mount
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )
    assert recorded and "refused" in recorded[0]
