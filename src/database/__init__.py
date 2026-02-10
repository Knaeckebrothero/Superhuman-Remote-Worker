"""Database module for the agent system (PostgreSQL, Neo4j, MongoDB).

This module provides database access through PostgresDB, Neo4jDB, and MongoDB classes.

- PostgresDB: Async orchestrator database (jobs, agents, citations)
- Neo4jDB: Generic Neo4j client for graph operations (via datasource connector)
- MongoDB: Optional LLM request archiving

Example:
    ```python
    from src.database import PostgresDB, Neo4jDB, MongoDB

    # PostgreSQL (async)
    postgres_db = PostgresDB()
    await postgres_db.connect()
    job_id = await postgres_db.jobs.create(description="Task description")

    # Neo4j (sync) - connection details from datasource connector
    neo4j_db = Neo4jDB(uri="bolt://...", username="neo4j", password="...")
    neo4j_db.connect()
    results = neo4j_db.execute_query("MATCH (n) RETURN n LIMIT 10")

    # MongoDB (optional, lazy)
    mongo_db = MongoDB()
    mongo_db.archive_llm_request(job_id="abc", agent_type="worker", ...)
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
