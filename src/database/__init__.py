"""Database module for Graph-RAG system (PostgreSQL, Neo4j, MongoDB).

This module provides database access through PostgresDB, Neo4jDB, and MongoDB classes
with namespace pattern for organized operations.

Example:
    ```python
    from src.database import PostgresDB, Neo4jDB, MongoDB

    # PostgreSQL (async) - create instance with dependency injection
    postgres_db = PostgresDB()
    await postgres_db.connect()
    job_id = await postgres_db.jobs.create(description="Extract requirements")

    # Neo4j (sync)
    neo4j_db = Neo4jDB()
    neo4j_db.connect()
    node_id = neo4j_db.requirements.create(rid="R001", text="...")

    # MongoDB (optional, lazy)
    mongo_db = MongoDB()
    mongo_db.archive_llm_request(job_id="abc", agent_type="creator", ...)
    ```
"""

from pathlib import Path

# Schema files
SCHEMA_DIR = Path(__file__).parent
SCHEMA_FILE = SCHEMA_DIR / "queries" / "postgres" / "schema.sql"
SCHEMA_VECTOR_FILE = SCHEMA_DIR / "schema_vector.sql"  # TODO: Move to queries/ if exists

# Database classes
from .postgres_db import PostgresDB  # noqa: E402
from .neo4j_db import Neo4jDB  # noqa: E402
from .mongo_db import MongoDB  # noqa: E402

__all__ = [
    # Database classes
    'PostgresDB',
    'Neo4jDB',
    'MongoDB',
    # Schema paths
    'SCHEMA_DIR',
    'SCHEMA_FILE',
    'SCHEMA_VECTOR_FILE',
]
