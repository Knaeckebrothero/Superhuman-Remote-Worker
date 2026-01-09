"""PostgreSQL connection utilities for the Graph-RAG Autonomous Agent System.

This module provides async PostgreSQL connection management for shared state
between Creator and Validator agents, job tracking, and requirement storage.

Note: LLM logging is handled by MongoDB (llm_archiver.py).
Note: Agent checkpointing is handled by LangGraph's AsyncPostgresSaver.
Note: Agent workspace is handled by filesystem (workspace_manager.py).
"""

import os
import logging
from typing import Optional, Any, List, Dict
from contextlib import asynccontextmanager
from datetime import datetime
import json
import uuid

try:
    import asyncpg
except ImportError:
    asyncpg = None  # Handle optional dependency gracefully

logger = logging.getLogger(__name__)


class PostgresConnection:
    """PostgreSQL connection manager for shared state.

    Manages a connection pool to PostgreSQL for efficient async database operations.
    Used by both Creator and Validator agents for job tracking and requirement storage.

    Example:
        ```python
        conn = PostgresConnection()
        await conn.connect()

        # Execute queries
        result = await conn.fetch("SELECT * FROM jobs WHERE status = $1", "pending")

        # Clean up
        await conn.disconnect()
        ```
    """

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize PostgreSQL connection manager.

        Args:
            connection_string: PostgreSQL connection string. If not provided,
                               reads from DATABASE_URL environment variable.
        """
        if asyncpg is None:
            raise ImportError(
                "asyncpg is required for PostgreSQL support. "
                "Install it with: pip install asyncpg"
            )

        self.connection_string = connection_string or os.getenv("DATABASE_URL")
        if not self.connection_string:
            raise ValueError(
                "Database connection string required. "
                "Set DATABASE_URL environment variable or pass connection_string."
            )

        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Establish connection pool.

        Creates a connection pool with min 2 and max 10 connections.
        Logs successful connection.
        """
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.connection_string,
                min_size=2,
                max_size=10,
                command_timeout=60.0
            )
            logger.info("PostgreSQL connection pool established")

    async def disconnect(self) -> None:
        """Close connection pool.

        Gracefully closes all connections in the pool.
        """
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgreSQL connection pool closed")

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool.

        Context manager for getting a connection from the pool.
        Connection is automatically returned when context exits.

        Yields:
            asyncpg.Connection: Database connection

        Raises:
            RuntimeError: If not connected to database
        """
        if self._pool is None:
            raise RuntimeError("Not connected to database. Call connect() first.")
        async with self._pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a query.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Command status string
        """
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> List[asyncpg.Record]:
        """Fetch multiple rows.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            List of Record objects
        """
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch a single row.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single Record or None if no results
        """
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch a single value.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single value from first column of first row
        """
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    @property
    def is_connected(self) -> bool:
        """Check if connected to database."""
        return self._pool is not None


# =============================================================================
# Job Management Functions
# =============================================================================


async def create_job(
    conn: PostgresConnection,
    prompt: str,
    document_path: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None
) -> uuid.UUID:
    """Create a new job in the database.

    Args:
        conn: PostgreSQL connection
        prompt: User prompt for the job
        document_path: Path to document file (optional)
        context: Additional context dictionary (optional)

    Returns:
        UUID of the created job
    """
    job_id = await conn.fetchval(
        """
        INSERT INTO jobs (prompt, document_path, context)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        prompt,
        document_path,
        json.dumps(context or {})
    )
    logger.info(f"Created job {job_id}")
    return job_id


async def get_job(conn: PostgresConnection, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Get job details by ID.

    Args:
        conn: PostgreSQL connection
        job_id: Job UUID

    Returns:
        Job details as dictionary or None if not found
    """
    row = await conn.fetchrow(
        "SELECT * FROM jobs WHERE id = $1",
        job_id
    )
    if row:
        return dict(row)
    return None


async def update_job_status(
    conn: PostgresConnection,
    job_id: uuid.UUID,
    status: Optional[str] = None,
    creator_status: Optional[str] = None,
    validator_status: Optional[str] = None,
    error_message: Optional[str] = None
) -> None:
    """Update job status fields.

    Args:
        conn: PostgreSQL connection
        job_id: Job UUID
        status: Main job status (optional)
        creator_status: Creator agent status (optional)
        validator_status: Validator agent status (optional)
        error_message: Error message if failed (optional)
    """
    updates = []
    values = []
    idx = 1

    if status is not None:
        updates.append(f"status = ${idx}")
        values.append(status)
        idx += 1

    if creator_status is not None:
        updates.append(f"creator_status = ${idx}")
        values.append(creator_status)
        idx += 1

    if validator_status is not None:
        updates.append(f"validator_status = ${idx}")
        values.append(validator_status)
        idx += 1

    if error_message is not None:
        updates.append(f"error_message = ${idx}")
        values.append(error_message)
        idx += 1

    if status == 'completed':
        updates.append(f"completed_at = ${idx}")
        values.append(datetime.utcnow())
        idx += 1

    if updates:
        values.append(job_id)
        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ${idx}"
        await conn.execute(query, *values)
        logger.info(f"Updated job {job_id} status")


# =============================================================================
# Requirement Functions
# =============================================================================


async def create_requirement(
    conn: PostgresConnection,
    job_id: uuid.UUID,
    text: str,
    name: Optional[str] = None,
    req_type: Optional[str] = None,
    priority: Optional[str] = None,
    source_document: Optional[str] = None,
    source_location: Optional[Dict] = None,
    gobd_relevant: bool = False,
    gdpr_relevant: bool = False,
    citations: Optional[List[str]] = None,
    mentioned_objects: Optional[List[str]] = None,
    mentioned_messages: Optional[List[str]] = None,
    reasoning: Optional[str] = None,
    research_notes: Optional[str] = None,
    confidence: float = 0.0,
    tags: Optional[List[str]] = None
) -> uuid.UUID:
    """Create a new requirement in the database.

    Args:
        conn: PostgreSQL connection
        job_id: Parent job UUID
        text: Requirement text
        name: Short name for requirement
        req_type: Type (functional, compliance, constraint, etc.)
        priority: Priority level (high, medium, low)
        source_document: Source document path
        source_location: Location in document (article, paragraph, etc.)
        gobd_relevant: GoBD relevance flag
        gdpr_relevant: GDPR relevance flag
        citations: List of citation IDs
        mentioned_objects: List of BusinessObject references
        mentioned_messages: List of Message references
        reasoning: Agent reasoning text
        research_notes: Research notes
        confidence: Confidence score (0.0-1.0)
        tags: List of tags for traceability

    Returns:
        UUID of created requirement
    """
    req_id = await conn.fetchval(
        """
        INSERT INTO requirements (
            job_id, text, name, type, priority, source_document, source_location,
            gobd_relevant, gdpr_relevant, citations, mentioned_objects,
            mentioned_messages, reasoning, research_notes, confidence, tags
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
        RETURNING id
        """,
        job_id, text, name, req_type, priority, source_document,
        json.dumps(source_location or {}),
        gobd_relevant, gdpr_relevant,
        json.dumps(citations or []),
        json.dumps(mentioned_objects or []),
        json.dumps(mentioned_messages or []),
        reasoning, research_notes, confidence,
        json.dumps(tags or [])
    )
    logger.info(f"Created requirement {req_id} for job {job_id}")
    return req_id


async def get_pending_requirement(
    conn: PostgresConnection,
    job_id: Optional[uuid.UUID] = None
) -> Optional[Dict[str, Any]]:
    """Get and lock the next pending requirement for validation.

    Uses SKIP LOCKED to prevent multiple validators grabbing the same requirement.

    Args:
        conn: PostgreSQL connection
        job_id: Optionally filter by job ID

    Returns:
        Requirement details as dictionary or None if none available
    """
    if job_id:
        query = """
            SELECT * FROM requirements
            WHERE status = 'pending' AND job_id = $1
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """
        row = await conn.fetchrow(query, job_id)
    else:
        query = """
            SELECT * FROM requirements
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """
        row = await conn.fetchrow(query)

    if row:
        # Update status to validating
        await conn.execute(
            "UPDATE requirements SET status = 'validating' WHERE id = $1",
            row['id']
        )
        return dict(row)
    return None


async def update_requirement_status(
    conn: PostgresConnection,
    requirement_id: uuid.UUID,
    status: str,
    validation_result: Optional[Dict] = None,
    neo4j_id: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    error: Optional[str] = None
) -> None:
    """Update requirement validation status.

    Args:
        conn: PostgreSQL connection
        requirement_id: Requirement UUID
        status: New status (integrated, rejected, failed)
        validation_result: Validation result details
        neo4j_id: Created Neo4j node rid
        rejection_reason: Reason for rejection
        error: Error message if failed
    """
    updates = ["status = $1"]
    values = [status]
    idx = 2

    if validation_result is not None:
        updates.append(f"validation_result = ${idx}")
        values.append(json.dumps(validation_result))
        idx += 1

    if neo4j_id is not None:
        updates.append(f"neo4j_id = ${idx}")
        values.append(neo4j_id)
        idx += 1

    if rejection_reason is not None:
        updates.append(f"rejection_reason = ${idx}")
        values.append(rejection_reason)
        idx += 1

    if error is not None:
        updates.append(f"last_error = ${idx}")
        values.append(error)
        idx += 1
        updates.append("retry_count = retry_count + 1")

    if status in ('integrated', 'rejected'):
        updates.append(f"validated_at = ${idx}")
        values.append(datetime.utcnow())
        idx += 1

    values.append(requirement_id)
    query = f"UPDATE requirements SET {', '.join(updates)} WHERE id = ${idx}"
    await conn.execute(query, *values)
    logger.info(f"Updated requirement {requirement_id} status to {status}")


async def count_requirements_by_status(
    conn: PostgresConnection,
    job_id: uuid.UUID
) -> Dict[str, int]:
    """Count requirements by status for a job.

    Args:
        conn: PostgreSQL connection
        job_id: Job UUID

    Returns:
        Dictionary mapping status to count
    """
    rows = await conn.fetch(
        """
        SELECT status, COUNT(*) as count
        FROM requirements
        WHERE job_id = $1
        GROUP BY status
        """,
        job_id
    )
    return {row['status']: row['count'] for row in rows}


# =============================================================================
# Factory Function
# =============================================================================


def create_postgres_connection(connection_string: Optional[str] = None) -> PostgresConnection:
    """Create a PostgreSQL connection instance.

    Args:
        connection_string: PostgreSQL connection string. If not provided,
                           reads from DATABASE_URL environment variable.

    Returns:
        PostgresConnection instance

    Example:
        ```python
        conn = create_postgres_connection()
        await conn.connect()
        # Use connection...
        await conn.disconnect()
        ```
    """
    return PostgresConnection(connection_string)
