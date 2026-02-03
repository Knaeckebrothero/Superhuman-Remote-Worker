"""Database layer for orchestrator.

Provides core database classes for the orchestrator:
- PostgresDB: Async PostgreSQL with connection pooling
- MongoDB: Async MongoDB for audit queries (optional)

This is the canonical database layer. All database operations should go
through these classes rather than creating separate connection pools.

Example:
    ```python
    from orchestrator.database import PostgresDB, MongoDB, ALLOWED_TABLES, SCHEMA_FILE

    # PostgreSQL (async)
    db = PostgresDB()
    await db.connect()
    rows = await db.fetch("SELECT * FROM jobs WHERE status = $1", "pending")

    # Schema management
    await db.ensure_schema()  # Apply schema.sql (idempotent)
    tables = await db.verify_schema()  # Check all tables exist

    # Job and agent operations
    job = await db.create_job(description="Extract requirements")
    await db.register_agent(config_name="creator", pod_ip="10.0.0.1")

    # MongoDB (optional, async)
    mongo = MongoDB()
    await mongo.connect()
    audit = await mongo.get_job_audit("abc-123", page=1, page_size=50)
    ```
"""

from .postgres import PostgresDB, ALLOWED_TABLES, PG_TYPE_MAP, SCHEMA_FILE, REQUIRED_TABLES
from .mongodb import MongoDB, FILTER_MAPPINGS, FilterCategory

__all__ = [
    # PostgreSQL
    'PostgresDB',
    'ALLOWED_TABLES',
    'PG_TYPE_MAP',
    'SCHEMA_FILE',
    'REQUIRED_TABLES',
    # MongoDB
    'MongoDB',
    'FILTER_MAPPINGS',
    'FilterCategory',
]
