#!/usr/bin/env python3
"""Read-only checkpoint type inventory; emits counts/types, never blob contents.

Run inside an existing application environment, using credentials already in
that environment. No application modules are imported and no constructors from
the stored data are invoked. Both channel blobs and pending writes are scanned.

  python checkpoint_type_inventory.py --db-prefix POSTGRES
  python checkpoint_type_inventory.py --url-env CHECKPOINT_DB_URL

POSTGRES is the authoritative database used by stateless fenced workers. Inspect
a separately configured CHECKPOINT store as well if pinned workers use one.
Msgpack and legacy JSON references are inspected; byte/null values cannot embed
serializer constructors. Pickle and unknown encodings fail the gate, as do
missing tables and decode failures.
"""

from __future__ import annotations

import argparse
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
import json
import os
import re
from typing import Any

import ormsgpack


FIRST_PARTY = frozenset(
    {"src", "agent", "orchestrator", "shared", "mcp_server", "vm_controller"}
)
_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


class Inventory:
    def __init__(self) -> None:
        self.references: Counter[tuple[str, str]] = Counter()
        self.extensions: Counter[int] = Counter()
        self.encodings: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()

    def _extension(self, code: int, raw: bytes) -> None:
        self.extensions[code] += 1
        if code not in range(8):
            self.errors["unknown_extension"] += 1
            return None
        decoded = ormsgpack.unpackb(
            raw, ext_hook=self._extension, option=ormsgpack.OPT_NON_STR_KEYS
        )
        if code in range(6):
            if (
                not isinstance(decoded, (list, tuple))
                or len(decoded) < 3
                or not all(isinstance(value, str) for value in decoded[:2])
            ):
                self.errors["malformed_type_reference"] += 1
            elif not _MODULE.fullmatch(decoded[0]) or not decoded[1].isidentifier():
                self.errors["invalid_type_reference"] += 1
            else:
                self.references[(decoded[0], decoded[1])] += 1
        # None is hashable if an encoded object was a map key. We only need its
        # type reference; no executable reconstruction or payload output occurs.
        return None

    def add(self, encoding: str, blob: bytes | memoryview | None) -> None:
        self.encodings[
            encoding
            if encoding
            in {"msgpack", "json", "null", "bytes", "bytearray", "empty", "pickle"}
            else "unknown"
        ] += 1
        if encoding in {"empty", "null", "bytes", "bytearray"}:
            return
        if encoding not in {"msgpack", "json"}:
            self.errors["unsupported_encoding"] += 1
            return
        if blob is None:
            self.errors["missing_blob"] += 1
            return
        try:
            if encoding == "msgpack":
                ormsgpack.unpackb(
                    bytes(blob),
                    ext_hook=self._extension,
                    option=ormsgpack.OPT_NON_STR_KEYS,
                )
            else:
                self._json_references(json.loads(bytes(blob)))
        except Exception as exc:
            # Decode diagnostics can include payload fragments; never stringify.
            self.errors[type(exc).__name__] += 1

    def _json_references(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self._json_references(item)
        elif isinstance(value, dict):
            if value.get("lc") in (1, 2) and value.get("type") == "constructor":
                identifier = value.get("id")
                if (
                    isinstance(identifier, list)
                    and len(identifier) >= 2
                    and all(
                        isinstance(part, str) and part.isidentifier()
                        for part in identifier
                    )
                ):
                    self.references[(".".join(identifier[:-1]), identifier[-1])] += 1
                else:
                    self.errors["invalid_json_type_reference"] += 1
            for item in value.values():
                self._json_references(item)

    def summary(self) -> dict[str, Any]:
        refs = [
            {"module": module, "class": name, "count": count}
            for (module, name), count in sorted(self.references.items())
        ]
        return {
            "rows": sum(self.encodings.values()),
            "encodings": dict(sorted(self.encodings.items())),
            "extensions": {
                str(code): count for code, count in sorted(self.extensions.items())
            },
            "type_references": refs,
            "first_party_references": [
                ref for ref in refs if ref["module"].split(".")[0] in FIRST_PARTY
            ],
            "errors": dict(sorted(self.errors.items())),
        }


def versions() -> dict[str, str]:
    result = {}
    for package in (
        "langgraph-checkpoint",
        "langgraph-checkpoint-postgres",
        "langchain-core",
        "ormsgpack",
        "pydantic",
    ):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "not-installed"
    return result


def inspect_database(
    *, prefix: str = "POSTGRES", url_env: str | None = None
) -> dict[str, Any]:
    import psycopg

    kwargs = {
        "autocommit": False,
        "connect_timeout": 10,
        "options": "-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=3000",
    }
    if url_env:
        dsn = os.environ.get(url_env)
        if not dsn:
            raise ValueError("requested connection environment variable is absent")
        kwargs["conninfo"] = dsn
    else:
        for part, keyword in (
            ("HOST", "host"),
            ("PORT", "port"),
            ("DB", "dbname"),
            ("USER", "user"),
            ("PASSWORD", "password"),
            ("SSLMODE", "sslmode"),
            ("SSLROOTCERT", "sslrootcert"),
        ):
            if value := os.environ.get(f"{prefix}_{part}"):
                kwargs[keyword] = value
        if not all(key in kwargs for key in ("host", "dbname", "user", "password")):
            raise ValueError(
                "required discrete database environment variables are absent"
            )
    output: dict[str, Any] = {"versions": versions(), "tables": {}, "read_only": False}
    with psycopg.connect(**kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SHOW transaction_read_only")
            output["read_only"] = cursor.fetchone()[0] == "on"
            if not output["read_only"]:
                raise RuntimeError("read-only transaction was not established")
            for table in ("checkpoint_blobs", "checkpoint_writes"):
                cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cursor.fetchone()[0] is None:
                    output["tables"][table] = {"error": "missing_table"}
                    continue
                inventory = Inventory()
                with connection.cursor(name=f"inventory_{table}") as stream:
                    # table comes from the fixed tuple above, never user input.
                    stream.execute(f"SELECT type, blob FROM public.{table}")
                    for encoding, blob in stream:
                        inventory.add(encoding, blob)
                output["tables"][table] = inventory.summary()
        connection.rollback()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--db-prefix", choices=("POSTGRES", "CHECKPOINT"), default="POSTGRES"
    )
    source.add_argument(
        "--url-env",
        help="Name of an existing environment variable; never put a DSN on the command line",
    )
    args = parser.parse_args()
    try:
        result = inspect_database(prefix=args.db_prefix, url_env=args.url_env)
    except Exception as exc:
        print(
            json.dumps(
                {"error_type": type(exc).__name__, "status": "inventory_incomplete"}
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(
        any(
            table.get("error")
            or table.get("errors")
            or table.get("first_party_references")
            for table in result["tables"].values()
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
