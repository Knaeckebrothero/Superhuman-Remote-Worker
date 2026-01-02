"""Requirement Cache Reader for Validator Agent.

Handles reading requirements from the PostgreSQL cache with SKIP LOCKED
pattern for concurrent processing, and status update operations.
"""

import logging
import uuid
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class RequirementCacheReader:
    """Reads and manages requirements from the PostgreSQL cache.

    Implements:
    - SKIP LOCKED polling for concurrent-safe requirement fetching
    - Status update methods (pending → validating → integrated/rejected/failed)
    - Retry count tracking for failed validations
    - Batch operations for efficiency

    Example:
        ```python
        from src.core.postgres_utils import create_postgres_connection

        pg_conn = create_postgres_connection()
        await pg_conn.connect()

        reader = RequirementCacheReader(pg_conn)
        requirement = await reader.get_next_pending()

        if requirement:
            # Process requirement...
            await reader.mark_integrated(requirement["id"], graph_node_id="R-0042")
        ```
    """

    def __init__(
        self,
        postgres_connection,
        max_retries: int = 5,
    ):
        """Initialize the cache reader.

        Args:
            postgres_connection: Active PostgresConnection instance
            max_retries: Maximum retry count before marking as failed
        """
        self.conn = postgres_connection
        self.max_retries = max_retries

    async def get_next_pending(
        self,
        job_id: Optional[uuid.UUID] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get and lock the next pending requirement.

        Uses FOR UPDATE SKIP LOCKED to prevent race conditions.
        Automatically updates status to 'validating'.

        Args:
            job_id: Optional filter by job ID

        Returns:
            Requirement data or None if no pending requirements
        """
        if job_id:
            query = """
                SELECT * FROM requirement_cache
                WHERE status = 'pending' AND job_id = $1 AND retry_count < $2
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """
            row = await self.conn.fetchrow(query, job_id, self.max_retries)
        else:
            query = """
                SELECT * FROM requirement_cache
                WHERE status = 'pending' AND retry_count < $1
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """
            row = await self.conn.fetchrow(query, self.max_retries)

        if row:
            # Update status to validating
            await self.conn.execute(
                """
                UPDATE requirement_cache
                SET status = 'validating', updated_at = $2
                WHERE id = $1
                """,
                row['id'],
                datetime.utcnow()
            )

            # Convert to dict and parse JSON fields
            result = dict(row)
            for json_field in ['source_location', 'citations', 'mentioned_objects',
                              'mentioned_messages', 'tags', 'validation_result']:
                if result.get(json_field):
                    try:
                        if isinstance(result[json_field], str):
                            result[json_field] = json.loads(result[json_field])
                    except (json.JSONDecodeError, TypeError):
                        pass

            logger.info(f"Acquired requirement {result['id']} for validation")
            return result

        return None

    async def get_pending_count(
        self,
        job_id: Optional[uuid.UUID] = None,
    ) -> int:
        """Count pending requirements.

        Args:
            job_id: Optional filter by job ID

        Returns:
            Number of pending requirements
        """
        if job_id:
            query = """
                SELECT COUNT(*) FROM requirement_cache
                WHERE status = 'pending' AND job_id = $1 AND retry_count < $2
            """
            return await self.conn.fetchval(query, job_id, self.max_retries)
        else:
            query = """
                SELECT COUNT(*) FROM requirement_cache
                WHERE status = 'pending' AND retry_count < $1
            """
            return await self.conn.fetchval(query, self.max_retries)

    async def get_validating_count(
        self,
        job_id: Optional[uuid.UUID] = None,
    ) -> int:
        """Count requirements currently being validated.

        Args:
            job_id: Optional filter by job ID

        Returns:
            Number of validating requirements
        """
        if job_id:
            query = """
                SELECT COUNT(*) FROM requirement_cache
                WHERE status = 'validating' AND job_id = $1
            """
            return await self.conn.fetchval(query, job_id)
        else:
            query = """
                SELECT COUNT(*) FROM requirement_cache
                WHERE status = 'validating'
            """
            return await self.conn.fetchval(query)

    async def mark_integrated(
        self,
        requirement_id: uuid.UUID,
        graph_node_id: str,
        validation_result: Optional[Dict] = None,
    ) -> None:
        """Mark a requirement as successfully integrated.

        Args:
            requirement_id: Requirement UUID
            graph_node_id: Created Neo4j node RID
            validation_result: Optional validation details
        """
        await self.conn.execute(
            """
            UPDATE requirement_cache
            SET status = 'integrated',
                graph_node_id = $2,
                validation_result = $3,
                validated_at = $4,
                updated_at = $4
            WHERE id = $1
            """,
            requirement_id,
            graph_node_id,
            json.dumps(validation_result) if validation_result else None,
            datetime.utcnow()
        )
        logger.info(f"Marked requirement {requirement_id} as integrated ({graph_node_id})")

    async def mark_rejected(
        self,
        requirement_id: uuid.UUID,
        rejection_reason: str,
        validation_result: Optional[Dict] = None,
    ) -> None:
        """Mark a requirement as rejected.

        Args:
            requirement_id: Requirement UUID
            rejection_reason: Reason for rejection (e.g., "duplicate", "not_relevant")
            validation_result: Optional validation details
        """
        await self.conn.execute(
            """
            UPDATE requirement_cache
            SET status = 'rejected',
                rejection_reason = $2,
                validation_result = $3,
                validated_at = $4,
                updated_at = $4
            WHERE id = $1
            """,
            requirement_id,
            rejection_reason,
            json.dumps(validation_result) if validation_result else None,
            datetime.utcnow()
        )
        logger.info(f"Marked requirement {requirement_id} as rejected: {rejection_reason}")

    async def mark_failed(
        self,
        requirement_id: uuid.UUID,
        error_message: str,
    ) -> None:
        """Mark a requirement as failed with error.

        Increments retry count. If max retries exceeded, keeps status as 'failed'.

        Args:
            requirement_id: Requirement UUID
            error_message: Error message
        """
        # Get current retry count
        row = await self.conn.fetchrow(
            "SELECT retry_count FROM requirement_cache WHERE id = $1",
            requirement_id
        )

        if not row:
            logger.warning(f"Requirement {requirement_id} not found")
            return

        new_retry_count = (row['retry_count'] or 0) + 1

        if new_retry_count >= self.max_retries:
            # Max retries exceeded - mark as permanently failed
            status = 'failed'
            logger.warning(f"Requirement {requirement_id} exceeded max retries ({self.max_retries})")
        else:
            # Return to pending for retry
            status = 'pending'
            logger.info(f"Requirement {requirement_id} will retry ({new_retry_count}/{self.max_retries})")

        await self.conn.execute(
            """
            UPDATE requirement_cache
            SET status = $2,
                retry_count = $3,
                last_error = $4,
                updated_at = $5
            WHERE id = $1
            """,
            requirement_id,
            status,
            new_retry_count,
            error_message,
            datetime.utcnow()
        )

    async def release_stale_validating(
        self,
        timeout_minutes: int = 30,
    ) -> int:
        """Release requirements stuck in 'validating' status.

        Returns requirements that have been validating too long back to 'pending'.
        This handles agent crashes during validation.

        Args:
            timeout_minutes: Minutes after which to consider validating as stale

        Returns:
            Number of released requirements
        """
        result = await self.conn.execute(
            """
            UPDATE requirement_cache
            SET status = 'pending',
                updated_at = $2
            WHERE status = 'validating'
            AND updated_at < NOW() - INTERVAL '$1 minutes'
            """,
            timeout_minutes,
            datetime.utcnow()
        )

        # Parse affected row count from result
        try:
            count = int(result.split()[-1])
        except (ValueError, IndexError):
            count = 0

        if count > 0:
            logger.info(f"Released {count} stale validating requirements")

        return count

    async def get_requirement_by_id(
        self,
        requirement_id: uuid.UUID,
    ) -> Optional[Dict[str, Any]]:
        """Get a specific requirement by ID.

        Args:
            requirement_id: Requirement UUID

        Returns:
            Requirement data or None
        """
        row = await self.conn.fetchrow(
            "SELECT * FROM requirement_cache WHERE id = $1",
            requirement_id
        )

        if row:
            result = dict(row)
            # Parse JSON fields
            for json_field in ['source_location', 'citations', 'mentioned_objects',
                              'mentioned_messages', 'tags', 'validation_result']:
                if result.get(json_field):
                    try:
                        if isinstance(result[json_field], str):
                            result[json_field] = json.loads(result[json_field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            return result

        return None

    async def get_requirements_by_job(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get requirements for a specific job.

        Args:
            job_id: Job UUID
            status: Optional status filter
            limit: Maximum results

        Returns:
            List of requirement data
        """
        if status:
            query = """
                SELECT * FROM requirement_cache
                WHERE job_id = $1 AND status = $2
                ORDER BY created_at ASC
                LIMIT $3
            """
            rows = await self.conn.fetch(query, job_id, status, limit)
        else:
            query = """
                SELECT * FROM requirement_cache
                WHERE job_id = $1
                ORDER BY created_at ASC
                LIMIT $2
            """
            rows = await self.conn.fetch(query, job_id, limit)

        results = []
        for row in rows:
            result = dict(row)
            for json_field in ['source_location', 'citations', 'mentioned_objects',
                              'mentioned_messages', 'tags', 'validation_result']:
                if result.get(json_field):
                    try:
                        if isinstance(result[json_field], str):
                            result[json_field] = json.loads(result[json_field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(result)

        return results

    async def get_status_counts(
        self,
        job_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, int]:
        """Get counts of requirements by status.

        Args:
            job_id: Optional filter by job ID

        Returns:
            Dictionary mapping status to count
        """
        if job_id:
            query = """
                SELECT status, COUNT(*) as count
                FROM requirement_cache
                WHERE job_id = $1
                GROUP BY status
            """
            rows = await self.conn.fetch(query, job_id)
        else:
            query = """
                SELECT status, COUNT(*) as count
                FROM requirement_cache
                GROUP BY status
            """
            rows = await self.conn.fetch(query)

        return {row['status']: row['count'] for row in rows}


def create_cache_reader(
    postgres_connection,
    max_retries: int = 5,
) -> RequirementCacheReader:
    """Create a RequirementCacheReader instance.

    Args:
        postgres_connection: Active PostgresConnection
        max_retries: Maximum retry count

    Returns:
        Configured RequirementCacheReader
    """
    return RequirementCacheReader(
        postgres_connection=postgres_connection,
        max_retries=max_retries,
    )
