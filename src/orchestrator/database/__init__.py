"""Database layer for orchestrator.

Provides core database classes for the orchestrator:
- PostgresDB: Async PostgreSQL with connection pooling
- AuditStore: Async Postgres reader for the audit trail

This is the canonical database layer. All database operations should go
through these classes rather than creating separate connection pools.

Example:
    ```python
    from orchestrator.database import PostgresDB, ALLOWED_TABLES, SCHEMA_FILE

    # PostgreSQL (async)
    db = PostgresDB()
    await db.connect()
    rows = await db.fetch("SELECT * FROM jobs WHERE status = $1", "pending")

    # Schema management
    await db.apply_migrations()  # Apply pending migrations (idempotent)
    tables = await db.verify_schema()  # Check all tables exist

    # Job and agent operations
    job = await db.create_job(description="Extract requirements")
    await db.register_agent(config_name="creator", pod_ip="10.0.0.1")

    # Audit reads (async)
    audit = AuditStore(audit_db_url)
    await audit.connect()
    trail = await audit.get_job_audit("abc-123", page=1, page_size=50)
    ```

The public exports are loaded lazily.  In particular, invoking
``python -m database.migrate`` must not import the application database layer
or its web/cloud dependencies before the standalone migration runner starts.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.database.audit_store import (
        AuditStore,
        FILTER_MAPPINGS,
        FilterCategory,
    )
    from orchestrator.database.postgres import (
        ALLOWED_TABLES,
        MIGRATIONS_APP_DIR,
        MIGRATIONS_AUDIT_DIR,
        MIGRATIONS_VECTOR_DIR,
        PG_TYPE_MAP,
        REQUIRED_TABLES,
        SCHEMA_FILE,
        PostgresDB,
    )

__all__ = [
    # PostgreSQL
    "PostgresDB",
    "ALLOWED_TABLES",
    "PG_TYPE_MAP",
    "SCHEMA_FILE",
    "MIGRATIONS_APP_DIR",
    "MIGRATIONS_VECTOR_DIR",
    "MIGRATIONS_AUDIT_DIR",
    "REQUIRED_TABLES",
    # Postgres audit store (reader + filter helpers)
    "AuditStore",
    "FILTER_MAPPINGS",
    "FilterCategory",
]

_EXPORT_MODULES = {
    "PostgresDB": "postgres",
    "ALLOWED_TABLES": "postgres",
    "PG_TYPE_MAP": "postgres",
    "SCHEMA_FILE": "postgres",
    "MIGRATIONS_APP_DIR": "postgres",
    "MIGRATIONS_VECTOR_DIR": "postgres",
    "MIGRATIONS_AUDIT_DIR": "postgres",
    "REQUIRED_TABLES": "postgres",
    "AuditStore": "audit_store",
    "FILTER_MAPPINGS": "audit_store",
    "FilterCategory": "audit_store",
}


def __getattr__(name: str) -> Any:
    """Load a public database export on first access."""

    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
