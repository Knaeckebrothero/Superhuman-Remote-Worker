#!/usr/bin/env python3
"""Read-only preflight for UID-less Kubernetes job workspace contexts.

Run in an orchestrator environment with the normal app-database variables.
The command performs no writes and prints no workspace coordinates, SSH
identity, credentials, or configuration. Exit 2 means at least one row cannot
use the automatic live-attestation adoption path and needs operator review.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_ORCH = _ROOT / "orchestrator"
if _ORCH.is_dir() and str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

from database.postgres import PostgresDB  # noqa: E402
from services.job_workspace_adoption import (  # noqa: E402
    legacy_k8s_job_runtime_adoption_candidate,
)
from src.shared.workspace_contract import WorkspaceContractError  # noqa: E402


async def main() -> int:
    db = PostgresDB()
    await db.connect()
    try:
        rows = await db.list_uidless_k8s_job_workspace_rows()
    finally:
        await db.disconnect()

    inventory: list[dict[str, str | bool | None]] = []
    refused = 0
    for row in rows:
        reason = None
        try:
            adoptable = legacy_k8s_job_runtime_adoption_candidate(row)
            if not adoptable:
                reason = "not_an_unambiguous_sandbox_candidate"
        except WorkspaceContractError as exc:
            adoptable = False
            reason = exc.code
        if not adoptable:
            refused += 1
        inventory.append(
            {
                "job_id": str(row["id"]),
                "status": str(row.get("status") or ""),
                "execution_lane": str(row.get("execution_lane") or ""),
                "workspace_owner_job_id": str(
                    row.get("parent_job_id")
                    if (
                        isinstance(row.get("context"), dict)
                        and row["context"].get("inherits_parent_workspace") is True
                    )
                    else row["id"]
                ),
                "adoptable_by_live_attestation": adoptable,
                "refusal_code": reason,
            }
        )

    print(
        json.dumps(
            {
                "uidless_k8s_job_rows": len(inventory),
                "live_attestation_candidates": len(inventory) - refused,
                "fail_closed_rows": refused,
                "jobs": inventory,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 2 if refused else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
