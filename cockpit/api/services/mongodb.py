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


# Singleton instance
mongodb_service = MongoDBService()
