#!/usr/bin/env python3
"""Preview or migrate stored Experts into canonical resources.

Uses the configured application database. Pending schema migrations must already
have run. No rows or credential values are printed. --apply is explicit so an
operator can first validate the conversion in their target installation.
"""

import argparse
import asyncio
import json
from pathlib import Path

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.manifest_experts import (
    expert_manifest,
    installed_srw_image,
    migrate_stored_experts,
    seed_bundled_expert_manifests,
)
from shared.manifests import preview_documents


async def run(args) -> dict:
    db = PostgresDB()
    await db.connect()
    try:
        if not args.apply:
            rows = await db.fetch(
                "SELECT * FROM experts WHERE manifest_resource_id IS NULL ORDER BY id"
            )
            for row in rows:
                preview_documents([expert_manifest(dict(row), image=args.image)])
            return {"operation": "preview", "pendingExperts": len(rows), "effects": []}
        stored = await migrate_stored_experts(db, image=args.image)
        bundled = await seed_bundled_expert_manifests(
            db, args.config_dir, image=args.image
        )
        return {"operation": "migrate", "stored": stored, "bundled": bundled}
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--image", default=installed_srw_image())
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config",
    )
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
