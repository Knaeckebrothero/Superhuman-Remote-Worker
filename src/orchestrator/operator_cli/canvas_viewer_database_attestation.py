"""Attest the Canvas viewer database identity from a release Job.

The command uses exactly the same dedicated pool construction and privilege
attestation as the running Canvas gateway. Its output is deliberately
coordinate- and credential-free so a failed Helm Job cannot leak a DSN or
password through pod logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Attest the dedicated Canvas viewer PostgreSQL identity."
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _runtime_dependencies() -> tuple[Callable[[], Any], Callable[[Any], Any]]:
    from orchestrator.services.canvas_viewer_database import (
        attest_canvas_viewer_database_privileges,
        create_canvas_viewer_database,
    )

    return create_canvas_viewer_database, attest_canvas_viewer_database_privileges


async def execute(
    *,
    db_factory: Callable[[], Any] | None = None,
    attest: Callable[[Any], Any] | None = None,
) -> int:
    if db_factory is None or attest is None:
        default_factory, default_attest = _runtime_dependencies()
        db_factory = db_factory or default_factory
        attest = attest or default_attest

    database = db_factory()
    await database.connect()
    try:
        async with database.acquire() as connection:
            await attest(connection)
    finally:
        await database.disconnect()

    print(
        json.dumps(
            {
                "event": "canvas_viewer_database_attestation",
                "status": "passed",
                "contains_database_coordinates_or_credentials": False,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parse_args(argv)
    try:
        return asyncio.run(execute())
    except KeyboardInterrupt:
        raise
    except Exception:
        print(
            json.dumps(
                {
                    "event": "canvas_viewer_database_attestation",
                    "status": "failed",
                    "contains_database_coordinates_or_credentials": False,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "execute", "main", "parse_args"]
