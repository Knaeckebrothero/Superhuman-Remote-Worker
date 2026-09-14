"""Execute credential fingerprint retirement against isolated PostgreSQL."""

from pathlib import Path
import re

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="module")
def postgres_url():
    with PostgresContainer("postgres:16-alpine") as container:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )


@pytest.mark.asyncio
async def test_retirement_clears_only_unkeyed_fingerprints(postgres_url):
    migration = (
        Path(__file__).resolve().parents[1]
        / "src/orchestrator/database/migrations/app/0248_retire_unkeyed_credential_provenance.sql"
    ).read_text()
    conn = await asyncpg.connect(postgres_url)
    try:
        for table in ("system_api_keys", "llm_endpoints"):
            await conn.execute(
                f"CREATE TABLE {table} (id text, api_key text, source text, "
                "source_updated_at timestamptz, helm_value_hash text)"
            )
            await conn.executemany(
                f"INSERT INTO {table} VALUES ($1, 'synthetic ciphertext', $2, "
                "'2026-01-01T00:00:00Z', $3)",
                [
                    (source + kind, source, digest)
                    for source in ("helm", "ui", "default")
                    for kind, digest in (
                        ("legacy", "0" * 64),
                        ("keyed", "hmac-sha256:" + "1" * 64),
                        ("empty", None),
                    )
                ],
            )
        before = {
            table: await conn.fetch(f"SELECT * FROM {table} ORDER BY id")
            for table in ("system_api_keys", "llm_endpoints")
        }
        # Repeat to prove retries do not clear new fingerprints or touch ownership.
        await conn.execute(migration)
        await conn.execute(migration)
        for table, rows in before.items():
            expected = [
                dict(row, helm_value_hash=None)
                if row["id"].endswith("legacy")
                else dict(row)
                for row in rows
            ]
            assert [
                dict(row)
                for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")
            ] == expected
    finally:
        await conn.close()
