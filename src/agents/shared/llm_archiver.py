"""LLM Request Archiver - stores LLM requests/responses in MongoDB.

This module provides functionality to archive all LLM interactions for:
- Debugging and troubleshooting
- Conversation replay and analysis
- Cost tracking and optimization
- Audit trails

Usage:
    archiver = LLMArchiver.from_env()

    # Archive a request/response
    archiver.archive(
        job_id="job-123",
        agent_type="creator",
        messages=[...],  # LangChain messages
        response=response,  # AIMessage
        model="gpt-4",
        latency_ms=1234,
        iteration=5,
    )

    # Query conversation history
    history = archiver.get_conversation(job_id="job-123")
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

logger = logging.getLogger(__name__)


def _message_to_dict(msg: BaseMessage) -> Dict[str, Any]:
    """Convert a LangChain message to a serializable dict."""
    result = {
        "type": msg.__class__.__name__,
        "content": msg.content if isinstance(msg.content, str) else str(msg.content),
    }

    # Add role for clarity
    if isinstance(msg, SystemMessage):
        result["role"] = "system"
    elif isinstance(msg, HumanMessage):
        result["role"] = "human"
    elif isinstance(msg, AIMessage):
        result["role"] = "assistant"
        # Include tool calls if present
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                }
                for tc in msg.tool_calls
            ]
    elif isinstance(msg, ToolMessage):
        result["role"] = "tool"
        result["tool_call_id"] = getattr(msg, "tool_call_id", "")
        result["name"] = getattr(msg, "name", "")

    # Include additional_kwargs if present and non-empty
    if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
        result["additional_kwargs"] = msg.additional_kwargs

    return result


class LLMArchiver:
    """Archives LLM requests and responses to MongoDB.

    Provides:
    - Request/response storage with full message history
    - Query by job_id, agent_type, time range
    - Conversation reconstruction for debugging
    """

    def __init__(
        self,
        mongodb_url: str,
        database_name: str = "graphrag_logs",
        collection_name: str = "llm_requests",
    ):
        """Initialize the archiver.

        Args:
            mongodb_url: MongoDB connection string
            database_name: Database name
            collection_name: Collection for LLM requests
        """
        self._mongodb_url = mongodb_url
        self._database_name = database_name
        self._collection_name = collection_name
        self._client = None
        self._db = None
        self._collection = None
        self._connected = False
        self._connection_attempted = False

    @classmethod
    def from_env(cls) -> Optional["LLMArchiver"]:
        """Create archiver from environment variables.

        Returns:
            LLMArchiver instance if MONGODB_URL is set, None otherwise.
        """
        mongodb_url = os.getenv("MONGODB_URL")
        if not mongodb_url:
            logger.debug("MONGODB_URL not set, LLM archiving disabled")
            return None

        # Extract database name from URL if present
        # Format: mongodb://host:port/database_name
        db_name = "graphrag_logs"
        if "/" in mongodb_url:
            url_path = mongodb_url.split("/")[-1]
            if url_path and "?" not in url_path:
                db_name = url_path
            elif "?" in url_path:
                db_name = url_path.split("?")[0] or "graphrag_logs"

        return cls(mongodb_url=mongodb_url, database_name=db_name)

    def _ensure_connected(self) -> bool:
        """Ensure MongoDB connection is established.

        Returns:
            True if connected, False otherwise.
        """
        if self._connected:
            return True

        if self._connection_attempted:
            return False

        self._connection_attempted = True

        try:
            from pymongo import MongoClient
            from pymongo.errors import ConnectionFailure

            self._client = MongoClient(
                self._mongodb_url,
                serverSelectionTimeoutMS=5000,
            )
            # Test connection
            self._client.admin.command("ping")

            self._db = self._client[self._database_name]
            self._collection = self._db[self._collection_name]
            self._connected = True

            logger.info(f"LLM Archiver connected to MongoDB: {self._database_name}")
            return True

        except ImportError:
            logger.warning("pymongo not installed, LLM archiving disabled")
            return False
        except Exception as e:
            logger.warning(f"Failed to connect to MongoDB: {e}")
            return False

    def archive(
        self,
        job_id: str,
        agent_type: str,
        messages: Sequence[BaseMessage],
        response: AIMessage,
        model: str,
        latency_ms: Optional[int] = None,
        iteration: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Archive an LLM request/response.

        Args:
            job_id: Job identifier
            agent_type: Agent type (e.g., "creator", "validator", "universal")
            messages: Input messages sent to LLM
            response: LLM response (AIMessage)
            model: Model name used
            latency_ms: Request latency in milliseconds
            iteration: Current iteration number
            metadata: Additional metadata to store

        Returns:
            Inserted document ID, or None if archiving failed.
        """
        if not self._ensure_connected():
            return None

        try:
            # Build document
            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "timestamp": datetime.now(timezone.utc),
                "model": model,
                "request": {
                    "messages": [_message_to_dict(m) for m in messages],
                    "message_count": len(messages),
                },
                "response": _message_to_dict(response),
            }

            # Add optional fields
            if latency_ms is not None:
                doc["latency_ms"] = latency_ms

            if iteration is not None:
                doc["iteration"] = iteration

            if metadata:
                doc["metadata"] = metadata

            # Count tokens approximately
            total_input_chars = sum(
                len(m.content) if isinstance(m.content, str) else 0
                for m in messages
            )
            response_chars = len(response.content) if isinstance(response.content, str) else 0

            doc["metrics"] = {
                "input_chars": total_input_chars,
                "output_chars": response_chars,
                "tool_calls": len(response.tool_calls) if hasattr(response, "tool_calls") and response.tool_calls else 0,
            }

            # Insert
            result = self._collection.insert_one(doc)
            doc_id = str(result.inserted_id)

            # Log a concise summary - use INFO so it's visible but not overwhelming
            tool_count = doc["metrics"]["tool_calls"]
            iter_str = f"iter={iteration}" if iteration else ""
            latency_str = f"{latency_ms}ms" if latency_ms else "?"
            tool_str = f"{tool_count} tools" if tool_count > 0 else "no tools"

            logger.info(
                f"[LLM] {doc_id[-8:]} | job={job_id[:8]}... | {iter_str} | "
                f"{latency_str} | {tool_str}"
            )
            return doc_id

        except Exception as e:
            logger.warning(f"Failed to archive LLM request: {e}")
            return None

    def get_conversation(
        self,
        job_id: str,
        agent_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get conversation history for a job.

        Args:
            job_id: Job identifier
            agent_type: Optional filter by agent type
            limit: Maximum number of records to return

        Returns:
            List of archived requests, sorted by timestamp ascending.
        """
        if not self._ensure_connected():
            return []

        try:
            query = {"job_id": job_id}
            if agent_type:
                query["agent_type"] = agent_type

            cursor = self._collection.find(query).sort("timestamp", 1).limit(limit)
            return list(cursor)

        except Exception as e:
            logger.warning(f"Failed to query conversation: {e}")
            return []

    def get_job_stats(self, job_id: str) -> Dict[str, Any]:
        """Get statistics for a job's LLM usage.

        Args:
            job_id: Job identifier

        Returns:
            Dict with usage statistics.
        """
        if not self._ensure_connected():
            return {}

        try:
            pipeline = [
                {"$match": {"job_id": job_id}},
                {
                    "$group": {
                        "_id": "$job_id",
                        "total_requests": {"$sum": 1},
                        "total_input_chars": {"$sum": "$metrics.input_chars"},
                        "total_output_chars": {"$sum": "$metrics.output_chars"},
                        "total_tool_calls": {"$sum": "$metrics.tool_calls"},
                        "avg_latency_ms": {"$avg": "$latency_ms"},
                        "first_request": {"$min": "$timestamp"},
                        "last_request": {"$max": "$timestamp"},
                        "models_used": {"$addToSet": "$model"},
                    }
                },
            ]

            results = list(self._collection.aggregate(pipeline))
            if results:
                stats = results[0]
                stats.pop("_id", None)
                return stats
            return {}

        except Exception as e:
            logger.warning(f"Failed to get job stats: {e}")
            return {}

    def get_recent_requests(
        self,
        limit: int = 50,
        agent_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get most recent LLM requests across all jobs.

        Args:
            limit: Maximum number of records
            agent_type: Optional filter by agent type

        Returns:
            List of recent requests, newest first.
        """
        if not self._ensure_connected():
            return []

        try:
            query = {}
            if agent_type:
                query["agent_type"] = agent_type

            cursor = self._collection.find(query).sort("timestamp", -1).limit(limit)
            return list(cursor)

        except Exception as e:
            logger.warning(f"Failed to get recent requests: {e}")
            return []

    def close(self):
        """Close MongoDB connection."""
        if self._client:
            self._client.close()
            self._connected = False
            logger.debug("LLM Archiver connection closed")


# Singleton instance for convenience
_default_archiver: Optional[LLMArchiver] = None


def get_archiver() -> Optional[LLMArchiver]:
    """Get or create the default LLM archiver instance.

    Returns:
        LLMArchiver instance if MongoDB is configured, None otherwise.
    """
    global _default_archiver
    if _default_archiver is None:
        _default_archiver = LLMArchiver.from_env()
    return _default_archiver


def archive_llm_request(
    job_id: str,
    agent_type: str,
    messages: Sequence[BaseMessage],
    response: AIMessage,
    model: str,
    latency_ms: Optional[int] = None,
    iteration: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Convenience function to archive an LLM request using default archiver.

    See LLMArchiver.archive() for parameter details.
    """
    archiver = get_archiver()
    if archiver:
        return archiver.archive(
            job_id=job_id,
            agent_type=agent_type,
            messages=messages,
            response=response,
            model=model,
            latency_ms=latency_ms,
            iteration=iteration,
            metadata=metadata,
        )
    return None
