"""MongoDB Database Manager with async motor for audit queries.

This module provides the canonical async MongoDB interface using motor with:
- Async connection with lazy initialization
- Paginated audit trail queries
- Chat history retrieval
- Bulk fetch endpoints for client-side caching
- Graph delta tracking

MongoDB is optional - the system gracefully degrades if unavailable.
This is the canonical database layer for the orchestrator.
"""

import logging
import math
import os
from datetime import datetime as dt, timezone
from typing import Optional, List, Dict, Any, Literal


def _to_iso_utc(timestamp: Any) -> str:
    """Convert a datetime to ISO string with UTC timezone indicator.

    MongoDB stores dates in UTC but returns naive datetime objects.
    This ensures the 'Z' suffix is included so JavaScript parses them correctly.
    """
    if hasattr(timestamp, "isoformat"):
        # If naive datetime, assume UTC and add Z suffix
        if hasattr(timestamp, "tzinfo") and timestamp.tzinfo is None:
            return timestamp.isoformat() + "Z"
        # If already has timezone, convert to UTC and use Z suffix
        elif hasattr(timestamp, "astimezone"):
            utc_dt = timestamp.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return timestamp.isoformat()
    return str(timestamp)


try:
    from bson import ObjectId
    from bson.errors import InvalidId
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

    MOTOR_AVAILABLE = True
except ImportError:
    ObjectId = None
    InvalidId = None
    AsyncIOMotorClient = None
    AsyncIOMotorDatabase = None
    MOTOR_AVAILABLE = False

logger = logging.getLogger(__name__)

# Filter category to step_type mapping
FILTER_MAPPINGS: Dict[str, List[str]] = {
    "all": [],  # Empty means no filtering
    "messages": ["llm"],
    "tools": ["tool"],
    "errors": ["error"],
}

FilterCategory = Literal["all", "messages", "tools", "errors"]


# Index declarations consumed by both the runtime `MongoDB.ensure_indexes()`
# helper (idempotent assert on every orchestrator startup) and the one-shot
# `init.py` CLI. Single source of truth: amend here, both paths pick it up.
#
# Why runtime ensure exists: the 2026-05-12 outage was rooted in six of seven
# `agent_audit` indexes never having been created on the live DB because no
# part of the deploy flow actually invoked `init.py`. Per-job COLLSCANs on a
# 117K-row collection then turned a single retrying job into a cluster-wide
# MongoDB CPU pin. See docs/issues/agent_audit_collection_missing_indexes.md.
#
# Each entry is (collection_name, [(keys, index_name), ...]). `keys` follows
# pymongo's create_index signature: a string for single-field, a list of
# (field, direction) tuples for compound. Motor and pymongo both treat a
# create_index call against an existing identical index as a silent no-op;
# only spec drift raises.
MONGODB_INDEX_DECLARATIONS: List[tuple] = [
    (
        "llm_requests",
        [
            ("job_id", "idx_job_id"),
            ("agent_type", "idx_agent_type"),
            ("timestamp", "idx_timestamp"),
            ("model", "idx_model"),
            (
                [("job_id", 1), ("agent_type", 1), ("timestamp", -1)],
                "idx_job_agent_time",
            ),
        ],
    ),
    (
        "agent_audit",
        [
            ("job_id", "idx_audit_job_id"),
            ("step_type", "idx_audit_step_type"),
            ("node_name", "idx_audit_node_name"),
            ("timestamp", "idx_audit_timestamp"),
            ([("job_id", 1), ("step_number", 1)], "idx_audit_job_step"),
            (
                [("job_id", 1), ("iteration", 1), ("step_number", 1)],
                "idx_audit_job_iter_step",
            ),
            (
                [("job_id", 1), ("agent_type", 1), ("step_type", 1)],
                "idx_audit_job_agent_type",
            ),
        ],
    ),
    (
        "chat_history",
        [
            ("job_id", "idx_chat_job_id"),
            ([("job_id", 1), ("timestamp", 1)], "idx_chat_job_timestamp"),
        ],
    ),
]


class MongoDB:
    """Async MongoDB manager for audit queries using motor.

    This database is optional - if not configured or unavailable, operations
    silently return None/empty lists and log warnings.

    Example:
        ```python
        db = MongoDB()
        await db.connect()

        # Get paginated audit trail
        result = await db.get_job_audit("abc-123", page=1, page_size=50)

        # Get chat history
        chat = await db.get_chat_history("abc-123")

        # Bulk fetch for client-side caching
        bulk = await db.get_job_audit_bulk("abc-123", offset=0, limit=5000)

        # Get cache invalidation version
        version = await db.get_job_version("abc-123")

        await db.disconnect()
        ```
    """

    def __init__(self, url: Optional[str] = None):
        """Initialize MongoDB manager.

        Args:
            url: MongoDB connection URL. Falls back to MONGODB_URL env var.
                Format: mongodb://host:port/database
        """
        if not MOTOR_AVAILABLE:
            logger.warning(
                "motor not installed. MongoDB features disabled. "
                "Install with: pip install motor"
            )

        self._url = url or os.getenv("MONGODB_URL")
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None
        self._available: bool = False
        self._db_name = self._parse_db_name(self._url) if self._url else "srw_logs"

        if not self._url:
            logger.info("MongoDB URL not configured. Logging features disabled.")

        logger.info("MongoDB initialized (not connected yet)")

    @staticmethod
    def _parse_db_name(url: str) -> str:
        """Extract database name from MongoDB URL, defaulting to srw_logs."""
        if "/" in url:
            path = url.split("/")[-1]
            if "?" in path:
                path = path.split("?")[0]
            if path:
                return path
        return "srw_logs"

    @property
    def is_available(self) -> bool:
        """Check if MongoDB is connected and available."""
        return self._available

    async def connect(self) -> None:
        """Create MongoDB connection.

        Tests the connection with a ping command. If connection fails,
        MongoDB features are disabled but no exception is raised.
        """
        if self._client is not None:
            return

        if not MOTOR_AVAILABLE:
            self._available = False
            return

        if not self._url:
            self._available = False
            return

        try:
            self._client = AsyncIOMotorClient(self._url, serverSelectionTimeoutMS=5000)
            # Test the connection
            await self._client.admin.command("ping")
            self._db = self._client.get_database(self._db_name)
            self._available = True
            logger.info(f"MongoDB connected: {self._db_name}")
        except Exception as e:
            logger.warning(f"MongoDB connection failed: {e}")
            self._available = False
            self._client = None
            self._db = None

    async def disconnect(self) -> None:
        """Close MongoDB connection."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
            self._available = False
            logger.info("MongoDB connection closed")

    async def ensure_indexes(self) -> int:
        """Idempotently assert every declared index exists on the live DB.

        Called from the orchestrator's FastAPI lifespan after ``connect()``
        so the index set is reasserted on every pod start — closing the gap
        that produced the 2026-05-12 outage, where the only DB-init path
        (the standalone ``init.py`` CLI) wasn't actually invoked by the
        deploy pipeline and six of seven ``agent_audit`` indexes never
        materialised. Motor's ``create_index`` is a silent no-op when the
        same index already exists, so a healthy cluster pays only the
        existence check.

        Failures are logged at ERROR — a missing index that turns a
        per-job aggregation into a 117K-row COLLSCAN is exactly the
        condition that took down the cluster, and silent WARNINGs were
        what kept it invisible. Returns the number of indexes successfully
        asserted (for logging / smoke tests).
        """
        if not self._available or self._db is None:
            return 0
        asserted = 0
        failed: List[str] = []
        for collection_name, indexes in MONGODB_INDEX_DECLARATIONS:
            coll = self._db[collection_name]
            for keys, index_name in indexes:
                try:
                    await coll.create_index(keys, name=index_name)
                    asserted += 1
                except Exception as e:
                    qualified = f"{collection_name}.{index_name}"
                    failed.append(qualified)
                    logger.error(
                        f"MongoDB ensure_indexes: failed to assert "
                        f"{qualified} ({keys!r}): {e}"
                    )
        if failed:
            logger.error(
                f"MongoDB ensure_indexes: {len(failed)} index(es) FAILED "
                f"to assert: {failed}. Per-job queries on these collections "
                f"will COLLSCAN until this is resolved."
            )
        else:
            logger.info(
                f"MongoDB ensure_indexes: asserted {asserted} indexes "
                f"across {len(MONGODB_INDEX_DECLARATIONS)} collections"
            )
        return asserted

    # Alias for compatibility
    async def close(self) -> None:
        """Close MongoDB connection (alias for disconnect())."""
        await self.disconnect()

    # =========================================================================
    # AUDIT TRAIL OPERATIONS
    # =========================================================================

    async def get_job_audit(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        order: Literal["asc", "desc"] = "asc",
    ) -> Dict[str, Any]:
        """Get paginated audit entries for a job.

        Accepts two pagination styles; both map to MongoDB skip/limit under
        the hood. If offset/limit are provided they take precedence over
        page/page_size.

        Args:
            job_id: The job UUID to query
            page: 1-indexed page number; -1 = last page (ignored if offset set)
            page_size: entries per page (ignored if limit set)
            filter_category: Filter by entry type (all, messages, tools, errors)
            offset: Entries to skip, REST-style. Overrides page if set.
            limit: Max entries to return, REST-style. Overrides page_size if set.
            order: asc = oldest-first (default), desc = newest-first

        Returns:
            Dict with entries, total, page, pageSize, offset, limit, hasMore
        """
        effective_size = limit if limit is not None else page_size

        if not self._available or self._db is None:
            return {
                "entries": [],
                "total": 0,
                "page": max(1, page),
                "pageSize": effective_size,
                "offset": offset if offset is not None else 0,
                "limit": effective_size,
                "hasMore": False,
            }

        collection = self._db["agent_audit"]

        # Build query filter
        query: Dict[str, Any] = {"job_id": job_id}
        step_types = FILTER_MAPPINGS.get(filter_category, [])
        if step_types:
            query["step_type"] = {"$in": step_types}

        # Get total count for pagination
        total = await collection.count_documents(query)

        # Resolve effective skip — offset wins over page if both present.
        if offset is not None:
            effective_skip = offset
        else:
            effective_page = page
            # Honor page=-1 (last page) for the legacy path
            if effective_page == -1:
                effective_page = (
                    max(1, math.ceil(total / effective_size)) if effective_size else 1
                )
            effective_skip = (effective_page - 1) * effective_size

        has_more = (effective_skip + effective_size) < total
        direction = 1 if order == "asc" else -1

        # Fetch paginated entries, sorted by step_number
        cursor = (
            collection.find(query)
            .sort("step_number", direction)
            .skip(effective_skip)
            .limit(effective_size)
        )

        entries = []
        async for doc in cursor:
            # Convert ObjectId to string for JSON serialization
            doc["_id"] = str(doc["_id"])
            entries.append(doc)

        # Echo back both param styles so either consumer can find its values
        response_page = (effective_skip // effective_size) + 1 if effective_size else 1

        return {
            "entries": entries,
            "total": total,
            "page": response_page,
            "pageSize": effective_size,
            "offset": effective_skip,
            "limit": effective_size,
            "hasMore": has_more,
        }

    async def get_audit_count(self, job_id: str) -> int:
        """Get total audit entry count for a job.

        Args:
            job_id: The job UUID to query

        Returns:
            Number of audit entries for the job
        """
        if not self._available or self._db is None:
            return 0

        collection = self._db["agent_audit"]
        return await collection.count_documents({"job_id": job_id})

    async def get_job_ids_with_audit(self) -> List[str]:
        """Get list of job IDs that have audit entries.

        Returns:
            List of unique job_id values from the audit collection
        """
        if not self._available or self._db is None:
            return []

        collection = self._db["agent_audit"]
        job_ids = await collection.distinct("job_id")
        return job_ids

    async def get_request(self, doc_id: str) -> Dict[str, Any] | None:
        """Get a single LLM request by document ID.

        Args:
            doc_id: MongoDB ObjectId as string

        Returns:
            Document dict or None if not found/invalid
        """
        if not self._available or self._db is None:
            return None

        try:
            oid = ObjectId(doc_id)
        except InvalidId:
            return None

        collection = self._db["llm_requests"]
        doc = await collection.find_one({"_id": oid})

        if doc is None:
            return None

        # Convert ObjectId to string for JSON serialization
        doc["_id"] = str(doc["_id"])
        return doc

    async def get_audit_time_range(self, job_id: str) -> Dict[str, str] | None:
        """Get first and last timestamps for a job's audit entries.

        Args:
            job_id: The job UUID to query

        Returns:
            Dict with 'start' and 'end' ISO timestamps, or None if no entries
        """
        if not self._available or self._db is None:
            return None

        collection = self._db["agent_audit"]

        # Get first entry (sorted by step_number asc)
        first = await collection.find_one(
            {"job_id": job_id},
            sort=[("step_number", 1)],
            projection={"timestamp": 1},
        )

        # Get last entry (sorted by step_number desc)
        last = await collection.find_one(
            {"job_id": job_id},
            sort=[("step_number", -1)],
            projection={"timestamp": 1},
        )

        if not first or not last:
            return None

        # Convert datetime objects to ISO strings
        start_str = _to_iso_utc(first["timestamp"])
        end_str = _to_iso_utc(last["timestamp"])

        return {"start": start_str, "end": end_str}

    async def get_page_for_timestamp(
        self,
        job_id: str,
        timestamp: str,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
    ) -> Dict[str, Any]:
        """Find which page contains the audit entry closest to a given timestamp.

        Counts entries with timestamp <= target to determine the page number.

        Args:
            job_id: The job UUID to query
            timestamp: ISO timestamp to locate
            page_size: Page size for calculating page number
            filter_category: Active filter category

        Returns:
            Dict with 'page' and 'index' (index within that page)
        """
        if not self._available or self._db is None:
            return {"page": 1, "index": 0}

        collection = self._db["agent_audit"]

        # Build base query with filter
        query: Dict[str, Any] = {"job_id": job_id}
        step_types = FILTER_MAPPINGS.get(filter_category, [])
        if step_types:
            query["step_type"] = {"$in": step_types}

        # Parse the target timestamp
        target_ts = dt.fromisoformat(timestamp)

        # Count entries with timestamp <= target (these come before or at the target)
        before_query = {**query, "timestamp": {"$lte": target_ts}}
        count_before = await collection.count_documents(before_query)

        if count_before == 0:
            return {"page": 1, "index": 0}

        # The entry is at position (count_before - 1) in 0-indexed list
        position = count_before - 1
        page = (position // page_size) + 1
        index = position % page_size

        return {"page": page, "index": index}

    # =========================================================================
    # CHAT HISTORY OPERATIONS
    # =========================================================================

    async def get_chat_history(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Get paginated chat history for a job.

        Returns a clean sequential view of conversation turns (input -> response).

        Args:
            job_id: The job UUID to query
            page: Page number (1-indexed). Use -1 to request the last page.
            page_size: Number of entries per page

        Returns:
            Dict with entries, total count, pagination info
        """
        if not self._available or self._db is None:
            return {
                "entries": [],
                "total": 0,
                "page": max(1, page),
                "pageSize": page_size,
                "hasMore": False,
            }

        collection = self._db["chat_history"]
        query = {"job_id": job_id}

        # Get total count for pagination
        total = await collection.count_documents(query)

        # Handle last page request (page=-1)
        if page == -1:
            page = max(1, math.ceil(total / page_size))

        # Calculate skip and check if there are more pages
        skip = (page - 1) * page_size
        has_more = (skip + page_size) < total

        # Fetch paginated entries, sorted by timestamp
        cursor = collection.find(query).sort("timestamp", 1).skip(skip).limit(page_size)

        entries = []
        async for doc in cursor:
            # Convert ObjectId to string for JSON serialization
            doc["_id"] = str(doc["_id"])
            entries.append(doc)

        return {
            "entries": entries,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "hasMore": has_more,
        }

    async def get_chat_history_count(self, job_id: str) -> int:
        """Get total chat history entries for a job.

        Args:
            job_id: The job UUID to query

        Returns:
            Number of chat history entries for the job
        """
        if not self._available or self._db is None:
            return 0

        return await self._db["chat_history"].count_documents({"job_id": job_id})

    # =========================================================================
    # BULK FETCH ENDPOINTS FOR CLIENT-SIDE CACHING
    # =========================================================================

    async def get_job_audit_bulk(
        self,
        job_id: str,
        offset: int = 0,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Get bulk audit entries for caching in IndexedDB.

        Uses offset/limit instead of page/pageSize for efficient bulk fetching.

        Args:
            job_id: The job UUID to query
            offset: Number of entries to skip
            limit: Maximum entries to return (up to 5000)

        Returns:
            Dict with entries, total count, offset, limit, hasMore
        """
        if not self._available or self._db is None:
            return {
                "entries": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "hasMore": False,
            }

        collection = self._db["agent_audit"]
        query = {"job_id": job_id}

        # Get total count
        total = await collection.count_documents(query)

        # Clamp limit to prevent abuse
        limit = min(limit, 5000)

        # Check if there are more entries
        has_more = (offset + limit) < total

        # Fetch entries sorted by step_number
        cursor = collection.find(query).sort("step_number", 1).skip(offset).limit(limit)

        entries = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            # Convert timestamp to ISO string
            if "timestamp" in doc:
                doc["timestamp"] = _to_iso_utc(doc["timestamp"])
            entries.append(doc)

        return {
            "entries": entries,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
        }

    async def get_chat_history_bulk(
        self,
        job_id: str,
        offset: int = 0,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Get bulk chat history entries for caching in IndexedDB.

        Args:
            job_id: The job UUID to query
            offset: Number of entries to skip
            limit: Maximum entries to return (up to 5000)

        Returns:
            Dict with entries, total count, offset, limit, hasMore
        """
        if not self._available or self._db is None:
            return {
                "entries": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "hasMore": False,
            }

        collection = self._db["chat_history"]
        query = {"job_id": job_id}

        # Get total count
        total = await collection.count_documents(query)

        # Clamp limit
        limit = min(limit, 5000)

        # Check if there are more entries
        has_more = (offset + limit) < total

        # Fetch entries sorted by timestamp
        cursor = collection.find(query).sort("timestamp", 1).skip(offset).limit(limit)

        entries = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            if "timestamp" in doc:
                doc["timestamp"] = _to_iso_utc(doc["timestamp"])
            entries.append(doc)

        return {
            "entries": entries,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
        }

    async def get_graph_deltas_bulk(
        self,
        job_id: str,
        offset: int = 0,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Get bulk graph deltas (execute_cypher_query tool calls) for caching.

        Args:
            job_id: The job UUID to query
            offset: Number of deltas to skip
            limit: Maximum deltas to return (up to 5000)

        Returns:
            Dict with deltas, total count, offset, limit, hasMore
        """
        if not self._available or self._db is None:
            return {
                "deltas": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "hasMore": False,
            }

        collection = self._db["agent_audit"]

        # Query for cypher tool calls (current + legacy name)
        query = {
            "job_id": job_id,
            "step_type": "tool",
            "tool.name": {
                "$in": ["cypher_query", "cypher_execute", "execute_cypher_query"]
            },
        }

        # Get total count
        total = await collection.count_documents(query)

        # Clamp limit
        limit = min(limit, 5000)

        # Check if there are more
        has_more = (offset + limit) < total

        # Fetch sorted by step_number
        cursor = collection.find(query).sort("step_number", 1).skip(offset).limit(limit)

        deltas = []
        index = offset
        async for doc in cursor:
            # Extract relevant data for graph delta
            query_text = doc.get("tool", {}).get("arguments", {}).get("query", "")
            timestamp = doc.get("timestamp")
            timestamp = _to_iso_utc(timestamp) if timestamp else None

            deltas.append(
                {
                    "toolCallIndex": index,
                    "timestamp": timestamp,
                    "cypherQuery": query_text,
                    "toolCallId": str(doc["_id"]),
                    "stepNumber": doc.get("step_number"),
                }
            )
            index += 1

        return {
            "deltas": deltas,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
        }

    async def get_job_version(self, job_id: str) -> Dict[str, Any] | None:
        """Get job data version info for cache invalidation.

        Returns counts and timestamps that can be compared to detect changes.

        Args:
            job_id: The job UUID to query

        Returns:
            Dict with version info, or None if job has no audit data
        """
        if not self._available or self._db is None:
            return None

        audit_collection = self._db["agent_audit"]
        chat_collection = self._db["chat_history"]

        # Get counts
        audit_count = await audit_collection.count_documents({"job_id": job_id})

        if audit_count == 0:
            return None

        chat_count = await chat_collection.count_documents({"job_id": job_id})

        # Count graph deltas (cypher tool calls, current + legacy name)
        graph_count = await audit_collection.count_documents(
            {
                "job_id": job_id,
                "step_type": "tool",
                "tool.name": {
                    "$in": ["cypher_query", "cypher_execute", "execute_cypher_query"]
                },
            }
        )

        # Get last audit entry timestamp
        last_entry = await audit_collection.find_one(
            {"job_id": job_id},
            sort=[("step_number", -1)],
            projection={"timestamp": 1},
        )

        last_update = None
        if last_entry and "timestamp" in last_entry:
            last_update = _to_iso_utc(last_entry["timestamp"])

        # Version is a hash of counts - if any count changes, version changes
        version = hash((audit_count, chat_count, graph_count))

        return {
            "version": version,
            "auditEntryCount": audit_count,
            "chatEntryCount": chat_count,
            "graphDeltaCount": graph_count,
            "lastUpdate": last_update,
        }

    # =========================================================================
    # LLM REQUEST LISTING
    # =========================================================================

    async def list_llm_requests(
        self,
        job_id: str,
        limit: int = 20,
        offset: int = 0,
        call_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List LLM requests for a job with summary fields only.

        Returns lightweight entries suitable for listing (no full message history).

        Args:
            job_id: The job UUID to query
            limit: Maximum entries to return (up to 100)
            offset: Number of entries to skip
            call_type: Optional filter. ``None`` or ``"all"`` returns every
                call type (main + auxiliary: memory_extraction, knowledge_curation,
                memory_assembly, summarization, title_generation, …). Pass an
                exact call_type to narrow.
            status: Optional filter. ``"error"`` returns only failed calls
                (auxiliary failures carry ``status="error"``); success rows omit
                the field, so any value filters them out.

        Returns:
            Dict with entries, total count, offset, limit, hasMore
        """
        if not self._available or self._db is None:
            return {
                "entries": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "hasMore": False,
            }

        collection = self._db["llm_requests"]
        query: Dict[str, Any] = {"job_id": job_id}
        # call_type=None/"all" -> every call type; otherwise narrow to one.
        if call_type and call_type != "all":
            query["call_type"] = call_type
        # Error rows carry status="error"; success rows omit the field.
        if status:
            query["status"] = status

        # Clamp limit
        limit = min(limit, 100)

        # Get total count
        total = await collection.count_documents(query)

        has_more = (offset + limit) < total

        # Project summary fields + response (for tool call names extraction).
        # call_type/status/error let the UI distinguish main vs auxiliary calls
        # and surface auxiliary failures (status="error").
        projection = {
            "_id": 1,
            "job_id": 1,
            "timestamp": 1,
            "model": 1,
            "token_usage": 1,
            "iteration": 1,
            "response": 1,
            "call_type": 1,
            "status": 1,
            "error": 1,
        }

        cursor = (
            collection.find(query, projection)
            .sort("timestamp", 1)
            .skip(offset)
            .limit(limit)
        )

        entries = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            if "timestamp" in doc:
                doc["timestamp"] = _to_iso_utc(doc["timestamp"])
            # Pre-call_type rows (and any miss) default to "main" for the UI.
            doc.setdefault("call_type", "main")

            # Extract just tool call names from response, then drop the full response
            response = doc.pop("response", {})
            if isinstance(response, dict):
                tool_calls_raw = response.get("tool_calls", [])
                doc["tool_calls"] = [
                    {"name": tc.get("name", "?")}
                    if isinstance(tc, dict)
                    else {"name": str(tc)}
                    for tc in (tool_calls_raw or [])
                ]
            else:
                doc["tool_calls"] = []

            entries.append(doc)

        return {
            "entries": entries,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
        }

    # =========================================================================
    # LEGACY COMPATIBILITY (from original MongoDB class)
    # =========================================================================

    async def get_job_audit_trail(
        self, job_id: str, event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get complete audit trail for a job (legacy method).

        Args:
            job_id: Job ID
            event_type: Filter by event type (optional, e.g., "tool_call", "phase_transition")

        Returns:
            List of audit records, or empty list if MongoDB unavailable
        """
        if not self._available or self._db is None:
            return []

        try:
            query: Dict[str, Any] = {"job_id": job_id}
            if event_type:
                query["event_type"] = event_type

            cursor = self._db.agent_audit.find(query).sort("timestamp", 1)
            results = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                results.append(doc)
            return results
        except Exception as e:
            logger.error(f"Failed to get audit trail: {e}")
            return []

    async def get_llm_conversation(
        self, job_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get LLM conversation history for a job (legacy method).

        Args:
            job_id: Job ID
            limit: Maximum number of records to return

        Returns:
            List of LLM request/response records, or empty list if MongoDB unavailable
        """
        if not self._available or self._db is None:
            return []

        try:
            cursor = (
                self._db.llm_requests.find({"job_id": job_id})
                .sort("timestamp", 1)
                .limit(limit)
            )
            results = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                results.append(doc)
            return results
        except Exception as e:
            logger.error(f"Failed to get LLM conversation: {e}")
            return []

    async def get_statistics(self) -> Dict[str, Any]:
        """Get MongoDB collection statistics (legacy method).

        Returns:
            Dictionary with collection counts, or empty dict if MongoDB unavailable
        """
        if not self._available or self._db is None:
            return {}

        try:
            stats = {
                "llm_requests_count": await self._db.llm_requests.count_documents({}),
                "agent_audit_count": await self._db.agent_audit.count_documents({}),
                "connected": True,
            }
            return stats
        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            return {"connected": False}

    @property
    def db(self):
        """Get database instance (for backward compatibility).

        Returns:
            Database instance or None if unavailable
        """
        return self._db


__all__ = ["MongoDB", "FILTER_MAPPINGS", "FilterCategory"]
