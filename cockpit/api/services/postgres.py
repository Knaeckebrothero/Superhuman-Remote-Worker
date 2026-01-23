"""PostgreSQL service for the cockpit API."""

import os
from typing import Any

import asyncpg

# Tables exposed to the cockpit
ALLOWED_TABLES = frozenset({"jobs", "requirements", "sources", "citations"})

# Column type mapping from PostgreSQL types to frontend-friendly types
PG_TYPE_MAP = {
    "uuid": "string",
    "text": "string",
    "varchar": "string",
    "character varying": "string",
    "integer": "number",
    "bigint": "number",
    "smallint": "number",
    "real": "number",
    "double precision": "number",
    "numeric": "number",
    "serial": "number",
    "boolean": "boolean",
    "timestamp with time zone": "date",
    "timestamp without time zone": "date",
    "timestamp": "date",
    "date": "date",
    "jsonb": "json",
    "json": "json",
    "bytea": "binary",
}


class PostgresService:
    """Async PostgreSQL service for querying tables."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Create connection pool."""
        if self._pool is not None:
            return

        database_url = os.getenv(
            "DATABASE_URL",
            "postgresql://graphrag:graphrag_password@localhost:5432/graphrag",
        )
        self._pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def get_tables(self) -> list[dict[str, Any]]:
        """Get list of allowed tables with row counts."""
        if not self._pool:
            raise RuntimeError("Database not connected")

        tables = []
        async with self._pool.acquire() as conn:
            for table in sorted(ALLOWED_TABLES):
                row = await conn.fetchrow(
                    f"SELECT COUNT(*) as count FROM {table}"  # noqa: S608
                )
                tables.append({"name": table, "rowCount": row["count"] if row else 0})
        return tables

    async def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Get column definitions for a table."""
        if table_name not in ALLOWED_TABLES:
            raise ValueError(f"Table '{table_name}' not allowed")

        if not self._pool:
            raise RuntimeError("Database not connected")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    column_name,
                    data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_name = $1 AND table_schema = 'public'
                ORDER BY ordinal_position
                """,
                table_name,
            )

        return [
            {
                "name": row["column_name"],
                "type": PG_TYPE_MAP.get(row["data_type"], "string"),
                "nullable": row["is_nullable"] == "YES",
            }
            for row in rows
        ]

    async def get_table_data(
        self,
        table_name: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Get paginated data from a table."""
        if table_name not in ALLOWED_TABLES:
            raise ValueError(f"Table '{table_name}' not allowed")

        if not self._pool:
            raise RuntimeError("Database not connected")

        offset = (page - 1) * page_size

        async with self._pool.acquire() as conn:
            # Get total count
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM {table_name}"  # noqa: S608
            )
            total = count_row["total"] if count_row else 0

            # Get schema for type info
            columns = await self.get_table_schema(table_name)

            # Get data with ordering by created_at if available, else by id
            order_col = "created_at" if table_name in ("jobs", "requirements", "citations") else "id"
            rows = await conn.fetch(
                f"SELECT * FROM {table_name} ORDER BY {order_col} DESC LIMIT $1 OFFSET $2",  # noqa: S608
                page_size,
                offset,
            )

            # Convert records to dicts, handling special types
            data = []
            for row in rows:
                row_dict = {}
                for key, value in dict(row).items():
                    if isinstance(value, bytes):
                        row_dict[key] = f"<binary: {len(value)} bytes>"
                    else:
                        row_dict[key] = value
                data.append(row_dict)

        return {
            "columns": columns,
            "rows": data,
            "total": total,
            "page": page,
            "pageSize": page_size,
        }


# Singleton instance
postgres_service = PostgresService()
