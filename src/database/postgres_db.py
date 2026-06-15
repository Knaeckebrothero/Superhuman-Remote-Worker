"""PostgreSQL Database Manager with async connection pooling.

This module provides a modern async PostgreSQL interface using asyncpg with:
- Async connection pooling
- Namespace-based operations (jobs, requirements, citations)
- Named query loading from SQL files
- CRUD operations with proper async patterns

Part of Phase 1 database refactoring - see docs/db_refactor.md
"""

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Any, List, Dict

try:
    import asyncpg
except ImportError:
    asyncpg = None

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).parent / "queries" / "postgres"

# Fixed namespace for deriving a UUID primary key from a non-UUID message id.
# In-memory message ids are provider-issued (``chatcmpl-…``, ``resp_…``) or
# locally minted (``msg_…``) — none are valid UUIDs, but ``thread_messages.id``
# is a UUID column. uuid5 maps an id deterministically, so re-saving the same
# message lands on the same row (``ON CONFLICT (id)`` dedup).
_THREAD_MSG_ID_NS = uuid.UUID("4b9d8f7e-2c3a-5d6b-8e1f-0a1b2c3d4e5f")


def _coerce_row_id(raw_id: Optional[str]) -> str:
    """Map a caller-supplied message id to a valid UUID for ``thread_messages.id``.

    Already-valid UUIDs (restored rows, the user-message fallback) pass through;
    provider/minted ids are derived deterministically via uuid5 so the upsert is
    idempotent across a message's incremental write and its turn-complete
    reconciliation; ``None`` mints a fresh UUID for single-shot rows (user
    message, summary). The DB row id has always been independent of the in-memory
    message id (restore assigns a fresh uuid4 either way), so deriving it changes
    no correlation — it only makes the key stable.
    """
    if not raw_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(raw_id)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_THREAD_MSG_ID_NS, str(raw_id)))


class PostgresDB:
    """PostgreSQL database manager with async connection pooling.

    Provides namespace-based operations for:
    - jobs: Job tracking and management
    - requirements: Requirement storage and queries
    - citations: Citation management

    Example:
        ```python
        db = PostgresDB()
        await db.connect()

        # Create a job
        job_id = await db.jobs.create(
            description="Extract requirements",
            document_path="doc.pdf"
        )

        # Get pending jobs
        jobs = await db.jobs.get_pending()

        await db.close()
        ```
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        min_connections: int = None,
        max_connections: int = None,
        command_timeout: float = None,
    ):
        """Initialize PostgreSQL database manager.

        Args:
            connection_string: PostgreSQL connection URL. Falls back to DATABASE_URL env var.
            min_connections: Minimum pool size (default: 2)
            max_connections: Maximum pool size (default: 10)
            command_timeout: Query timeout in seconds (default: 60.0)

        Raises:
            ImportError: If asyncpg is not installed
            ValueError: If no connection string provided
        """
        if asyncpg is None:
            raise ImportError(
                "asyncpg is required for PostgreSQL support. "
                "Install it with: pip install asyncpg"
            )

        from src.utils.db_url import build_postgres_url

        self._connection_string = connection_string or build_postgres_url(
            "POSTGRES",
            fallback_env="DATABASE_URL",
        )
        if not self._connection_string:
            raise ValueError(
                "Database connection string required. Set "
                "POSTGRES_USER + POSTGRES_PASSWORD (with POSTGRES_HOST/PORT/DB "
                "from ConfigMap) or DATABASE_URL, or pass connection_string."
            )

        self._min_connections = min_connections or int(
            os.getenv("POSTGRES_MIN_CONNECTIONS", "2")
        )
        self._max_connections = max_connections or int(
            os.getenv("POSTGRES_MAX_CONNECTIONS", "10")
        )
        self._command_timeout = command_timeout or 60.0

        self._pool: Optional[asyncpg.Pool] = None
        self._queries: Dict[str, str] = {}  # Cache for loaded queries

        # Initialize namespaces
        self.jobs = JobsNamespace(self)
        self.citations = CitationsNamespace(self)
        self.config_overrides = ConfigOverridesNamespace(self)
        self.experts = ExpertsNamespace(self)

        logger.info("PostgresDB initialized (not connected yet)")

    async def connect(self) -> None:
        """Establish async connection pool.

        Creates an asyncpg connection pool with configured size and timeout.
        Registers pgvector type codec on each connection if available.
        This method is idempotent - safe to call multiple times.
        """
        if self._pool is None:

            async def _init_connection(conn):
                """Register pgvector type codec on new connections."""
                try:
                    from pgvector.asyncpg import register_vector

                    await register_vector(conn)
                except (ImportError, ValueError):
                    pass  # pgvector not installed or extension not on this DB

            self._pool = await asyncpg.create_pool(
                self._connection_string,
                min_size=self._min_connections,
                max_size=self._max_connections,
                command_timeout=self._command_timeout,
                init=_init_connection,
            )
            logger.info(
                f"PostgreSQL connection pool established "
                f"(min={self._min_connections}, max={self._max_connections})"
            )

    async def close(self) -> None:
        """Close connection pool.

        Gracefully closes all connections in the pool.
        This method is idempotent - safe to call multiple times.
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
        """Execute a query without returning results.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Command status string (e.g., "UPDATE 1")
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

    def _load_query(self, filename: str, query_name: str) -> str:
        """Load a named query from a .sql file.

        Queries are cached after first load. Query files use the format:

        ```sql
        -- name: query_name
        SELECT ...;

        -- name: another_query
        SELECT ...;
        ```

        Args:
            filename: SQL file name (e.g., "complex.sql")
            query_name: Name of the query to load

        Returns:
            SQL query string

        Raises:
            ValueError: If query not found in file
        """
        cache_key = f"{filename}:{query_name}"
        if cache_key in self._queries:
            return self._queries[cache_key]

        file_path = QUERIES_DIR / filename
        if not file_path.exists():
            raise ValueError(f"Query file not found: {file_path}")

        content = file_path.read_text()

        # Parse named queries: -- name: query_name
        pattern = r"--\s*name:\s*(\w+)\s*\n(.*?)(?=--\s*name:|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)

        for name, sql in matches:
            self._queries[f"{filename}:{name}"] = sql.strip()

        if cache_key not in self._queries:
            raise ValueError(f"Query '{query_name}' not found in {filename}")

        return self._queries[cache_key]

    @staticmethod
    def _row_to_dict(row: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
        """Convert asyncpg Record to dictionary.

        Args:
            row: asyncpg Record or None

        Returns:
            Dictionary with column names as keys, or None if row is None
        """
        if row is None:
            return None
        return dict(row)

    @property
    def is_connected(self) -> bool:
        """Check if connected to database."""
        return self._pool is not None

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get thread by ID."""
        row = await self.fetchrow(
            "SELECT * FROM threads WHERE id = $1",
            thread_id,
        )
        return self._row_to_dict(row)

    async def end_thread(self, thread_id: str) -> None:
        """End a persistent thread."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET status   = 'ended',
                    ended_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                thread_id,
            )

    async def update_thread_status(self, thread_id: str, status: str) -> None:
        """Update thread status."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET status        = $2,
                    last_activity = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                thread_id,
                status,
            )

    async def get_thread_messages_history(
        self,
        thread_id: str,
        limit: Optional[int] = 200,
        offset: int = 0,
        since_turn: Optional[int] = None,
        seq_gt: Optional[int] = None,
        newest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """Load thread message history. Ordered by turn_number, created_at ASC.

        Pass ``limit=None`` to load the entire conversation. Session resume
        uses this: the LLM working context is bounded afterwards by
        ContextManager.ensure_within_limits (token-driven compaction), not by
        truncating stored history. A fixed message cap can slice a parallel
        tool-call batch and orphan a function call, which the Responses API
        rejects with a 400.

        Pass ``since_turn=N`` to load only rows with ``turn_number > N``. The
        resume-from-checkpoint path uses this to skip the pre-checkpoint
        history that the summary row already covers (see
        ``get_latest_compaction_checkpoint``).

        Pass ``seq_gt=S`` to load only rows with ``seq > S``, ordered by ``seq``
        (exact insertion order). This is the message-granular resume cursor: a
        compaction records ``boundary_seq`` = the seq of the last message its
        summary covers, and resume loads ``summary + (seq > boundary_seq)`` — the
        agent's real live tail — instead of whole post-boundary turns. Preferred
        over ``since_turn`` when the checkpoint carries a ``boundary_seq``.

        Pass ``newest_first=True`` (with a ``limit``) for the resume backstop:
        the rows are selected ``seq DESC LIMIT N`` (the **newest** N, not the
        oldest) and returned reversed to chronological order. This bounds the
        restore so one pathological tail (thousands of messages) can't OOM even
        when there's no usable summary/boundary; it is a floor, not the
        mechanism. The caller logs when ``len(result) == limit`` (trimmed).
        """
        query = """
            SELECT id, role, content, tool_calls, turn_number, metrics,
                   tool_call_id, thinking, reasoning, tool_results,
                   provider, provider_raw, additional_kwargs, response_metadata,
                   created_at
            FROM thread_messages
            WHERE thread_id = $1
              AND role NOT IN ('summary', 'error')
        """
        params: List[Any] = [thread_id]
        if seq_gt is not None:
            params.append(seq_gt)
            query += f"\n              AND seq > ${len(params)}"
        if since_turn is not None:
            params.append(since_turn)
            query += f"\n              AND turn_number > ${len(params)}"
        # newest_first: take the NEWEST `limit` rows (seq DESC), reversed to
        # chronological below — the resume floor. Else order by seq (the seq
        # cursor) or turn/created_at (legacy since_turn / full-load callers).
        if newest_first:
            query += "\n            ORDER BY seq DESC"
        elif seq_gt is not None:
            query += "\n            ORDER BY seq ASC"
        else:
            query += "\n            ORDER BY turn_number ASC, created_at ASC"
        if limit is not None:
            params.append(limit)
            query += f"\n            LIMIT ${len(params)}"
        params.append(offset)
        query += f"\n            OFFSET ${len(params)}"

        rows = await self.fetch(query, *params)

        def _j(v):
            return json.loads(v) if isinstance(v, (str, bytes)) else v

        result = []
        for row in rows:
            result.append(
                {
                    "id": str(row["id"]),
                    "role": row["role"],
                    "content": row["content"],
                    "tool_calls": _j(row["tool_calls"]) if row["tool_calls"] else None,
                    "turn_number": row["turn_number"],
                    "metrics": _j(row["metrics"]) if row["metrics"] else None,
                    "tool_call_id": row["tool_call_id"],
                    "thinking": row["thinking"],
                    "reasoning": _j(row["reasoning"]) if row["reasoning"] else None,
                    "tool_results": _j(row["tool_results"])
                    if row["tool_results"]
                    else None,
                    "provider": row["provider"],
                    "provider_raw": _j(row["provider_raw"])
                    if row["provider_raw"]
                    else None,
                    "additional_kwargs": _j(row["additional_kwargs"])
                    if row["additional_kwargs"]
                    else None,
                    "response_metadata": _j(row["response_metadata"])
                    if row["response_metadata"]
                    else None,
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                }
            )
        if newest_first:
            # Selected newest-first to apply the LIMIT to the tail; hand back
            # chronological so callers (resume) get oldest→newest as usual.
            result.reverse()
        return result

    async def get_latest_compaction_checkpoint(
        self, thread_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the latest compaction checkpoint for this thread, if any.

        Reads the newest ``role='summary'`` row (which the main history query
        excludes). Used by the resume path: if a checkpoint exists, restore
        ``[summary] + history(since_turn=boundary_turn)`` instead of the full
        log — avoids re-loading hundreds of messages and re-summarizing from
        scratch.

        Rolling compactions merge prior summaries into a single new row, so the
        newest row always carries the cumulative summary. Returns ``None`` when
        no compaction has been persisted yet (back-compat: fall back to full
        load). ``boundary_turn`` may be ``None`` on rows written before the
        checkpoint feature shipped — caller must also fall back in that case.
        """
        query = """
            SELECT content, metrics, turn_number
            FROM thread_messages
            WHERE thread_id = $1
              AND role = 'summary'
            ORDER BY turn_number DESC NULLS LAST, created_at DESC
            LIMIT 1
        """
        row = await self.fetchrow(query, thread_id)
        if row is None:
            return None

        metrics_raw = row["metrics"]
        metrics = (
            json.loads(metrics_raw)
            if isinstance(metrics_raw, (str, bytes))
            else metrics_raw
        ) or {}
        return {
            "summary": row["content"] or "",
            "boundary_turn": metrics.get("boundary_turn"),
            "boundary_seq": metrics.get("boundary_seq"),
            "turn_number": row["turn_number"],
        }

    async def get_seq_for_message_id(
        self, thread_id: str, msg_id: str
    ) -> Optional[int]:
        """Return the persisted ``seq`` of a message by its in-memory id.

        Resolves the summarized/kept boundary message
        (``ContextManager._last_compaction_boundary_id``) into a ``boundary_seq``
        for the summary row. The in-memory id is coerced the same way it was on
        write (``_coerce_row_id``), so a provider/minted id resolves to its row.
        Returns ``None`` when the message isn't persisted (a transient injection,
        or a resume-time fresh-id message) — the caller then leaves
        ``boundary_seq`` unset and falls back to ``boundary_turn``.
        """
        row = await self.fetchrow(
            "SELECT seq FROM thread_messages WHERE thread_id = $1 AND id = $2",
            thread_id,
            _coerce_row_id(msg_id),
        )
        return row["seq"] if row else None

    async def save_thread_message(
        self,
        thread_id: str,
        role: str,
        content: Optional[str] = None,
        tool_calls: Optional[Any] = None,
        turn_number: Optional[int] = None,
        metrics: Optional[dict] = None,
        tool_call_id: Optional[str] = None,
        thinking: Optional[str] = None,
        reasoning: Optional[Any] = None,
        tool_results: Optional[Any] = None,
        provider: Optional[str] = None,
        provider_raw: Optional[Any] = None,
        additional_kwargs: Optional[dict] = None,
        response_metadata: Optional[dict] = None,
        id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a ``thread_messages`` row directly (no orchestrator hop).

        The persistent agent already holds this asyncpg pool for resume reads and
        config writes, so message writes go straight here instead of through
        ``POST /api/agents/threads/{id}/messages`` (a pure pass-through). Two
        properties this direct path adds, both load-bearing for the resume fix in
        docs/issues/persistent_session_midturn_message_loss.md:

        - **Caller-supplied ``id`` + idempotent upsert.** The agent mints a stable
          id per message (``persistent_graph._ensure_msg_id`` /
          ``_sanitize_ai_response``) and passes it here; ``_coerce_row_id`` maps
          it to a valid UUID for the PK column (provider/minted ids aren't UUIDs).
          ``ON CONFLICT (id) DO UPDATE`` means a message written incrementally and
          then re-saved by the turn-complete reconciliation pass collapses onto
          one row — no duplicates. ``id=None`` mints a fresh UUID (single-shot
          rows like the user message and the summary, which are never re-saved).
        - **``seq`` returned in the same round-trip.** ``RETURNING id, seq`` hands
          back the row's monotonic insertion order, so a compaction can record
          ``boundary_seq`` without a follow-up read.

        Mirrors the orchestrator's column set and the ``threads`` activity bump.
        ``tool_call_id`` is set only on role='tool' rows; ``thinking`` only on
        role='ai' rows with reasoning. The component columns (reasoning,
        tool_results, provider, provider_raw, additional_kwargs,
        response_metadata) are nullable (migration 0019). The ``seq`` column and
        its ``ON CONFLICT`` target arrived in migration 0023.
        """
        msg_id = _coerce_row_id(id)
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO thread_messages
                    (id, thread_id, role, content, tool_calls, turn_number,
                     metrics, tool_call_id, thinking,
                     reasoning, tool_results, provider, provider_raw,
                     additional_kwargs, response_metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14, $15)
                ON CONFLICT (id) DO UPDATE SET
                    content           = EXCLUDED.content,
                    tool_calls        = EXCLUDED.tool_calls,
                    turn_number       = EXCLUDED.turn_number,
                    metrics           = EXCLUDED.metrics,
                    tool_call_id      = EXCLUDED.tool_call_id,
                    thinking          = EXCLUDED.thinking,
                    reasoning         = EXCLUDED.reasoning,
                    tool_results      = EXCLUDED.tool_results,
                    provider          = EXCLUDED.provider,
                    provider_raw      = EXCLUDED.provider_raw,
                    additional_kwargs = EXCLUDED.additional_kwargs,
                    response_metadata = EXCLUDED.response_metadata
                RETURNING id, seq
                """,
                msg_id,
                thread_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls is not None else None,
                turn_number,
                json.dumps(metrics) if metrics is not None else None,
                tool_call_id,
                thinking,
                json.dumps(reasoning) if reasoning is not None else None,
                json.dumps(tool_results) if tool_results is not None else None,
                provider,
                json.dumps(provider_raw) if provider_raw is not None else None,
                json.dumps(additional_kwargs)
                if additional_kwargs is not None
                else None,
                json.dumps(response_metadata)
                if response_metadata is not None
                else None,
            )
            # Bump thread activity + turn count (mirrors the orchestrator path so
            # going direct doesn't regress last_activity / total_turns tracking).
            await conn.execute(
                """
                UPDATE threads
                SET last_activity = CURRENT_TIMESTAMP,
                    total_turns   = GREATEST(total_turns, COALESCE($2, 0))
                WHERE id = $1
                """,
                thread_id,
                turn_number,
            )
        return {"id": str(row["id"]), "seq": row["seq"]}

    # =========================================================================
    # SYNC WRAPPERS (for scripts and other sync contexts)
    # =========================================================================

    # Class-level event loop for sync wrappers (shared across all instances)
    _sync_loop = None

    @classmethod
    def _get_sync_loop(cls):
        """Get or create a persistent event loop for sync operations.

        Returns the same event loop across all calls, allowing asyncpg
        connection pools to persist between sync wrapper calls.
        """
        import asyncio

        if cls._sync_loop is None or cls._sync_loop.is_closed():
            cls._sync_loop = asyncio.new_event_loop()
        return cls._sync_loop

    @classmethod
    def _run_async(cls, coro):
        """Helper to run async coroutines in sync context.

        Uses a persistent event loop to execute coroutines, allowing
        asyncpg connection pools to persist between calls.

        Args:
            coro: Async coroutine to execute

        Returns:
            Result of the coroutine
        """
        import asyncio

        try:
            # Check if we're already in an async context
            loop = asyncio.get_running_loop()
            if loop.is_running():
                raise RuntimeError(
                    "Cannot use sync wrapper inside async context. "
                    "Use async methods directly instead."
                )
        except RuntimeError:
            pass  # No running loop, that's fine

        loop = cls._get_sync_loop()
        return loop.run_until_complete(coro)

    def connect_sync(self) -> None:
        """Synchronous wrapper for connect().

        Establishes async connection pool in sync context.
        """
        self._run_async(self.connect())

    def close_sync(self) -> None:
        """Synchronous wrapper for close().

        Closes connection pool in sync context.
        """
        self._run_async(self.close())

    def execute_sync(self, query: str, *args) -> str:
        """Synchronous wrapper for execute().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Command status string
        """
        return self._run_async(self.execute(query, *args))

    def fetch_sync(self, query: str, *args) -> List[Dict[str, Any]]:
        """Synchronous wrapper for fetch().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            List of row dictionaries
        """
        rows = self._run_async(self.fetch(query, *args))
        return [self._row_to_dict(row) for row in rows]

    def fetchrow_sync(self, query: str, *args) -> Optional[Dict[str, Any]]:
        """Synchronous wrapper for fetchrow().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single row dictionary or None
        """
        row = self._run_async(self.fetchrow(query, *args))
        return self._row_to_dict(row)

    def fetchval_sync(self, query: str, *args) -> Any:
        """Synchronous wrapper for fetchval().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single value from first column of first row
        """
        return self._run_async(self.fetchval(query, *args))


class JobsNamespace:
    """Namespace for job-related operations.

    Provides CRUD operations for the jobs table.
    """

    def __init__(self, db: PostgresDB):
        self.db = db

    async def create(
        self,
        description: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> uuid.UUID:
        """Create a new job.

        Args:
            description: Job description - what the agent should accomplish
            document_path: Path to document file (optional)
            context: Additional context dictionary (optional)

        Returns:
            UUID of the created job
        """
        job_id = await self.db.fetchval(
            """
            INSERT INTO jobs (description, document_path, context)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            description,
            document_path,
            json.dumps(context or {}),
        )
        logger.info(f"Created job {job_id}")
        return job_id

    async def get(self, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Get job by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job details as dictionary or None if not found
        """
        row = await self.db.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        return self.db._row_to_dict(row)

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update job status fields.

        Args:
            job_id: Job UUID
            status: Main job status (optional)
            error_message: Error message if failed (optional)
        """
        updates = []
        values = []
        idx = 1

        if status is not None:
            updates.append(f"status = ${idx}")
            values.append(status)
            idx += 1

        if error_message is not None:
            updates.append(f"error_message = ${idx}")
            values.append(error_message)
            idx += 1

        if not updates:
            return

        updates.append("updated_at = NOW()")
        values.append(job_id)

        query = f"""
            UPDATE jobs
            SET {", ".join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Updated job {job_id} status")

    async def get_pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get pending jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of job dictionaries
        """
        rows = await self.db.fetch(
            """
            SELECT * FROM jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [self.db._row_to_dict(row) for row in rows]

    async def list(
        self, status: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List jobs with optional filtering.

        Args:
            status: Filter by status (optional)
            limit: Maximum number of jobs to return
            offset: Number of jobs to skip

        Returns:
            List of job dictionaries
        """
        if status:
            rows = await self.db.fetch(
                """
                SELECT * FROM jobs
                WHERE status = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                status,
                limit,
                offset,
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT * FROM jobs
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        return [self.db._row_to_dict(row) for row in rows]

    async def store_resolved_config(
        self,
        job_id: uuid.UUID,
        resolved_config: Dict[str, Any],
    ) -> bool:
        """Store the fully resolved config snapshot for a job.

        Only writes if resolved_config is currently NULL (first run only).

        Args:
            job_id: Job UUID
            resolved_config: Fully resolved config dict (agent config + prompts + instructions)

        Returns:
            True if the config was stored, False if already set
        """
        result = await self.db.execute(
            "UPDATE jobs SET resolved_config = $1::jsonb WHERE id = $2 AND resolved_config IS NULL",
            json.dumps(resolved_config),
            job_id,
        )
        stored = result == "UPDATE 1"
        if stored:
            logger.debug(f"Stored resolved config for job {job_id}")
        return stored

    async def get_resolved_config(
        self,
        job_id: uuid.UUID,
    ) -> Optional[Dict[str, Any]]:
        """Get the resolved config snapshot for a job.

        Args:
            job_id: Job UUID

        Returns:
            Resolved config dict or None if not set
        """
        row = await self.db.fetchrow(
            "SELECT resolved_config FROM jobs WHERE id = $1",
            job_id,
        )
        if row and row["resolved_config"]:
            rc = row["resolved_config"]
            return rc if isinstance(rc, dict) else json.loads(rc)
        return None

    # Sync wrappers for scripts
    def create_sync(
        self,
        description: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> uuid.UUID:
        """Synchronous wrapper for create()."""
        return PostgresDB._run_async(self.create(description, document_path, context))

    def get_sync(self, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Synchronous wrapper for get()."""
        return PostgresDB._run_async(self.get(job_id))

    def update_status_sync(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Synchronous wrapper for update_status()."""
        return PostgresDB._run_async(self.update_status(job_id, status, error_message))

    def get_pending_sync(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Synchronous wrapper for get_pending()."""
        return PostgresDB._run_async(self.get_pending(limit))

    def list_sync(
        self, status: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for list()."""
        return PostgresDB._run_async(self.list(status, limit, offset))


class ConfigOverridesNamespace:
    """Config-override reads for the agent's resolution path."""

    def __init__(self, db: PostgresDB):
        self.db = db

    async def list_overrides_for_family(self, family: str) -> List[Dict[str, Any]]:
        """Return override rows for <family> plus global (NULL-family) rows.

        Read once per job at first run; the result is loaded into the loader's
        process map and frozen into resolved_config.
        """
        rows = await self.db.fetch(
            """
            SELECT family, kind, name, content, content_format, value_json
            FROM config_overrides
            WHERE family = $1 OR family IS NULL
            """,
            family,
        )
        return [self.db._row_to_dict(row) for row in rows]


class ExpertsNamespace:
    """Expert reads for the agent's resolution path (decision 6)."""

    def __init__(self, db: PostgresDB):
        self.db = db

    async def get_by_id(self, expert_id: str) -> Optional[Dict[str, Any]]:
        """Return one expert row by UUID, or None (the agent fails loud on None).
        config/prompts come back as JSONB (str without a codec) — the consumer
        json.loads them via build_expert_config."""
        row = await self.db.fetchrow(
            "SELECT id, name, expert_type, config, prompts "
            "FROM experts WHERE id = $1::uuid",
            expert_id,
        )
        return self.db._row_to_dict(row)


class CitationsNamespace:
    """Namespace for citation-related operations.

    Provides CRUD operations for citations (if schema includes citations table).
    """

    def __init__(self, db: PostgresDB):
        self.db = db

    async def edit(
        self,
        citation_id: int,
        claim: Optional[str] = None,
        verbatim_quote: Optional[str] = None,
        quote_context: Optional[str] = None,
        relevance_reasoning: Optional[str] = None,
        confidence: Optional[str] = None,
        extraction_method: Optional[str] = None,
        locator: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Edit fields of a citation.

        When content fields (claim, verbatim_quote, quote_context) change,
        verification_status is reset to 'pending' and verification_notes,
        similarity_score, matched_location are cleared.

        Args:
            citation_id: Citation integer ID
            claim: The assertion being supported
            verbatim_quote: Exact quote from source
            quote_context: Context around the quote
            relevance_reasoning: Why this citation is relevant
            confidence: Confidence level (high, medium, low)
            extraction_method: How the citation was extracted
            locator: Location reference (JSON)

        Raises:
            ValueError: If citation not found or no fields provided
        """
        # Guard: check citation exists
        row = await self.db.fetchrow(
            "SELECT id FROM citations WHERE id = $1", citation_id
        )
        if not row:
            raise ValueError(f"Citation {citation_id} not found")

        # Determine if content fields are changing
        content_fields_changed = any(
            v is not None for v in [claim, verbatim_quote, quote_context]
        )

        # Build dynamic UPDATE clause
        updates = []
        values = []
        idx = 1

        field_map = [
            ("claim", claim, lambda v: v),
            ("verbatim_quote", verbatim_quote, lambda v: v),
            ("quote_context", quote_context, lambda v: v),
            ("relevance_reasoning", relevance_reasoning, lambda v: v),
            ("confidence", confidence, lambda v: v),
            ("extraction_method", extraction_method, lambda v: v),
            ("locator", locator, lambda v: json.dumps(v)),
        ]

        for col, val, transform in field_map:
            if val is not None:
                updates.append(f"{col} = ${idx}")
                values.append(transform(val))
                idx += 1

        if not updates:
            raise ValueError("No fields provided to edit")

        # Reset verification fields when content changes
        if content_fields_changed:
            updates.append("verification_status = 'pending'")
            updates.append("verification_notes = NULL")
            updates.append("similarity_score = NULL")
            updates.append("matched_location = NULL")

        values.append(citation_id)

        query = f"""
            UPDATE citations
            SET {", ".join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Edited citation {citation_id}")

    def edit_sync(self, citation_id: int, **kwargs) -> None:
        """Synchronous wrapper for edit()."""
        return PostgresDB._run_async(self.edit(citation_id, **kwargs))


__all__ = ["PostgresDB"]
