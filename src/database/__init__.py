"""Database module for the agent system (PostgreSQL, Neo4j).

This module provides database access through PostgresDB and Neo4jDB classes.

- PostgresDB: Async orchestrator database (jobs, agents, citations)
- Neo4jDB: Generic Neo4j client for graph operations (via datasource connector)

Example:
    ```python
    from src.database import PostgresDB, Neo4jDB

    # PostgreSQL (async)
    postgres_db = PostgresDB()
    await postgres_db.connect()
    job = await postgres_db.jobs.get(job_id)  # creation is orchestrator-side

    # Neo4j (sync) - connection details from datasource connector
    neo4j_db = Neo4jDB(uri="bolt://...", username="neo4j", password="...")
    neo4j_db.connect()
    results = neo4j_db.execute_query("MATCH (n) RETURN n LIMIT 10")
    ```
"""

from pathlib import Path

# Schema files
SCHEMA_DIR = Path(__file__).parent
SCHEMA_FILE = SCHEMA_DIR / "queries" / "postgres" / "schema.sql"
SCHEMA_VECTOR_FILE = (
    SCHEMA_DIR / "schema_vector.sql"
)  # TODO: Move to queries/ if exists

# Database classes
from .postgres_db import PostgresDB  # noqa: E402
from .neo4j_db import Neo4jDB  # noqa: E402

__all__ = [
    # Database classes
    "PostgresDB",
    "Neo4jDB",
    # Schema paths
    "SCHEMA_DIR",
    "SCHEMA_FILE",
    "SCHEMA_VECTOR_FILE",
]
