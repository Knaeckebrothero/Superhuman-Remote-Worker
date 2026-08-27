"""A retried captured teardown must not re-capture a snapshot it already has.

The live gate saw the first teardown attempt upload a 132 MB terminal snapshot,
get a 403 on the rootdisk PVC, and then retry — SSHing into a VM that was
already shutting down and burying the good snapshot under ``capture_failed``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.services.vm_provisioner import VMProvisioner


def _provisioner(job_row: dict | None) -> VMProvisioner:
    provisioner = VMProvisioner()
    provisioner._db = AsyncMock()
    provisioner._db.get_job = AsyncMock(return_value=job_row)
    return provisioner


@pytest.mark.asyncio
async def test_terminal_snapshot_after_this_incarnation_is_reused() -> None:
    provisioner = _provisioner(
        {
            "context": {
                "vm": {"provisioned_at": 1787643693.5},
                "snapshot": {
                    "status": "available",
                    "source_type": "vm",
                    "phase_number": None,
                    "created_at": "2026-08-25T07:57:33+00:00",
                },
            }
        }
    )

    assert await provisioner._terminal_snapshot_already_captured("job-1") is True


@pytest.mark.asyncio
async def test_snapshot_from_an_earlier_incarnation_is_not_reused() -> None:
    provisioner = _provisioner(
        {
            "context": {
                # Re-provisioned after the snapshot was taken (crash recovery).
                "vm": {"provisioned_at": 1787650000.0},
                "snapshot": {
                    "status": "available",
                    "source_type": "vm",
                    "phase_number": None,
                    "created_at": "2026-08-25T07:57:33+00:00",
                },
            }
        }
    )

    assert await provisioner._terminal_snapshot_already_captured("job-1") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"status": "capture_failed", "source_type": "vm", "phase_number": None},
        {"status": "available", "source_type": "pod", "phase_number": None},
        {"status": "available", "source_type": "vm", "phase_number": 2},
    ],
)
async def test_other_snapshots_do_not_count(snapshot: dict) -> None:
    snapshot = {**snapshot, "created_at": "2026-08-25T07:57:33+00:00"}
    provisioner = _provisioner(
        {"context": {"vm": {"provisioned_at": 1.0}, "snapshot": snapshot}}
    )

    assert await provisioner._terminal_snapshot_already_captured("job-1") is False


@pytest.mark.asyncio
async def test_missing_db_or_row_means_capture() -> None:
    assert await _provisioner(None)._terminal_snapshot_already_captured("j") is False
    bare = VMProvisioner()
    assert await bare._terminal_snapshot_already_captured("j") is False
