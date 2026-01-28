"""MongoDB service for the cockpit API."""

import math
import os
from typing import Any, Literal

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Filter category to step_type mapping
FILTER_MAPPINGS: dict[str, list[str]] = {
    "all": [],  # Empty means no filtering
    "messages": ["llm"],
    "tools": ["tool"],
    "errors": ["error"],
}

FilterCategory = Literal["all", "messages", "tools", "errors"]


class MongoDBService:
    """Async MongoDB service for querying agent audit logs."""

    def __init__(self) -> None:
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None
        self._available: bool = False

    @property
    def is_available(self) -> bool:
        """Check if MongoDB is connected and available."""
        return self._available

    async def connect(self) -> None:
        """Create MongoDB connection."""
        if self._client is not None:
            return

        mongodb_url = os.getenv("MONGODB_URL")
        if not mongodb_url:
            # MongoDB is optional - graceful degradation
            self._available = False
            return

        try:
            self._client = AsyncIOMotorClient(mongodb_url, serverSelectionTimeoutMS=5000)
            # Test the connection
            await self._client.admin.command("ping")
            self._db = self._client.get_database("graphrag_logs")
            self._available = True
        except Exception:
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

    async def get_job_audit(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
    ) -> dict[str, Any]:
        """Get paginated audit entries for a job.

        Args:
            job_id: The job UUID to query
            page: Page number (1-indexed). Use -1 to request the last page.
            page_size: Number of entries per page
            filter_category: Filter by entry type (all, messages, tools, errors)

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

        collection = self._db["agent_audit"]

        # Build query filter
        query: dict[str, Any] = {"job_id": job_id}
        step_types = FILTER_MAPPINGS.get(filter_category, [])
        if step_types:
            query["step_type"] = {"$in": step_types}

        # Get total count for pagination
        total = await collection.count_documents(query)

        # Handle last page request (page=-1)
        if page == -1:
            page = max(1, math.ceil(total / page_size))

        # Calculate skip and check if there are more pages
        skip = (page - 1) * page_size
        has_more = (skip + page_size) < total

        # Fetch paginated entries, sorted by step_number
        cursor = collection.find(query).sort("step_number", 1).skip(skip).limit(page_size)

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

    async def get_job_ids_with_audit(self) -> list[str]:
        """Get list of job IDs that have audit entries.

        Returns:
            List of unique job_id values from the audit collection
        """
        if not self._available or self._db is None:
            return []

        collection = self._db["agent_audit"]
        job_ids = await collection.distinct("job_id")
        return job_ids

    async def get_request(self, doc_id: str) -> dict[str, Any] | None:
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

    async def get_audit_time_range(self, job_id: str) -> dict[str, str] | None:
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
        start_ts = first["timestamp"]
        end_ts = last["timestamp"]
        start_str = start_ts.isoformat() if hasattr(start_ts, 'isoformat') else str(start_ts)
        end_str = end_ts.isoformat() if hasattr(end_ts, 'isoformat') else str(end_ts)

        return {"start": start_str, "end": end_str}

    async def get_page_for_timestamp(
        self,
        job_id: str,
        timestamp: str,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
    ) -> dict[str, Any]:
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

        from datetime import datetime as dt

        collection = self._db["agent_audit"]

        # Build base query with filter
        query: dict[str, Any] = {"job_id": job_id}
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

    async def get_chat_history(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
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

        # Fetch paginated entries, sorted by sequence_number
        cursor = collection.find(query).sort("sequence_number", 1).skip(skip).limit(page_size)

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
    # Bulk Fetch Endpoints for Client-Side Caching
    # =========================================================================

    async def get_job_audit_bulk(
        self,
        job_id: str,
        offset: int = 0,
        limit: int = 5000,
    ) -> dict[str, Any]:
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
            if "timestamp" in doc and hasattr(doc["timestamp"], "isoformat"):
                doc["timestamp"] = doc["timestamp"].isoformat()
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
    ) -> dict[str, Any]:
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

        # Fetch entries sorted by sequence_number
        cursor = collection.find(query).sort("sequence_number", 1).skip(offset).limit(limit)

        entries = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            if "timestamp" in doc and hasattr(doc["timestamp"], "isoformat"):
                doc["timestamp"] = doc["timestamp"].isoformat()
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
    ) -> dict[str, Any]:
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

        # Query for execute_cypher_query tool calls
        query = {
            "job_id": job_id,
            "step_type": "tool",
            "tool.name": "execute_cypher_query",
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
            if hasattr(timestamp, "isoformat"):
                timestamp = timestamp.isoformat()

            deltas.append({
                "toolCallIndex": index,
                "timestamp": timestamp,
                "cypherQuery": query_text,
                "toolCallId": str(doc["_id"]),
                "stepNumber": doc.get("step_number"),
            })
            index += 1

        return {
            "deltas": deltas,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
        }

    async def get_job_version(self, job_id: str) -> dict[str, Any] | None:
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

        # Count graph deltas (execute_cypher_query tool calls)
        graph_count = await audit_collection.count_documents({
            "job_id": job_id,
            "step_type": "tool",
            "tool.name": "execute_cypher_query",
        })

        # Get last audit entry timestamp
        last_entry = await audit_collection.find_one(
            {"job_id": job_id},
            sort=[("step_number", -1)],
            projection={"timestamp": 1},
        )

        last_update = None
        if last_entry and "timestamp" in last_entry:
            ts = last_entry["timestamp"]
            last_update = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

        # Version is a hash of counts - if any count changes, version changes
        version = hash((audit_count, chat_count, graph_count))

        return {
            "version": version,
            "auditEntryCount": audit_count,
            "chatEntryCount": chat_count,
            "graphDeltaCount": graph_count,
            "lastUpdate": last_update,
        }


# Singleton instance
mongodb_service = MongoDBService()
