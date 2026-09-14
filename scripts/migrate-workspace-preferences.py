#!/usr/bin/env python3
"""Preview or apply workspace preference migration in the configured application DB.

Only safe identity/revision metadata is printed. Execution history and active
Project generations are preserved. Managed Experts need a Project update.
"""

import argparse
import asyncio
import json

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.manifest_workspace_migration import (
    migrate_workspace_preferences,
)


async def run(args):
    db = PostgresDB()
    await db.connect()
    try:
        return await migrate_workspace_preferences(
            db, apply=args.apply, plan_revision=args.plan_revision
        )
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--plan-revision", help="The exact revision returned by preview"
    )
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))
