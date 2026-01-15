"""LLM Request Archiver & Agent Auditor - stores LLM requests and agent steps in MongoDB.

This module provides functionality to archive all LLM interactions and agent steps for:
- Debugging and troubleshooting
- Conversation replay and analysis
- Cost tracking and optimization
- Complete audit trails of agent execution

Usage:
    archiver = LLMArchiver.from_env()

    # Archive a request/response (existing functionality)
    archiver.archive(
        job_id="job-123",
        agent_type="creator",
        messages=[...],  # LangChain messages
        response=response,  # AIMessage
        model="gpt-4",
        latency_ms=1234,
        iteration=5,
    )

    # Audit any agent step (new functionality)
    archiver.audit_step(
        job_id="job-123",
        agent_type="creator",
        step_type="tool_call",
        node_name="tools",
        iteration=5,
        data={"tool": {"name": "read_file", "arguments": {"path": "file.txt"}}},
    )

    # Query conversation history
    history = archiver.get_conversation(job_id="job-123")

    # Query complete audit trail
    audit_trail = archiver.get_job_audit_trail(job_id="job-123")
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
        audit_collection_name: str = "agent_audit",
    ):
        """Initialize the archiver.

        Args:
            mongodb_url: MongoDB connection string
            database_name: Database name
            collection_name: Collection for LLM requests
            audit_collection_name: Collection for agent audit trail
        """
        # Import here to avoid circular imports
        from src.database.mongo_db import MongoDB

        self._mongodb_url = mongodb_url
        self._database_name = database_name
        self._collection_name = collection_name
        self._audit_collection_name = audit_collection_name
        self._mongo_db = MongoDB(url=mongodb_url) if mongodb_url else None
        self._collection = None
        self._audit_collection = None
        self._connected = False
        self._connection_attempted = False
        self._step_counters: Dict[str, int] = {}  # Per-job step counters

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

        if not self._mongo_db:
            return False

        self._connection_attempted = True

        try:
            # MongoDB class has lazy connection - access db property to trigger connection
            if self._mongo_db.db is None:
                logger.warning("MongoDB connection not available")
                return False

            # Get collections from the underlying database
            self._collection = self._mongo_db.db[self._collection_name]
            self._audit_collection = self._mongo_db.db[self._audit_collection_name]
            self._connected = True

            logger.info(f"LLM Archiver connected to MongoDB: {self._database_name}")
            return True

        except Exception as e:
            logger.warning(f"Failed to connect to MongoDB: {e}")
            return False

    def _get_next_step_number(self, job_id: str) -> int:
        """Get the next step number for a job.

        Args:
            job_id: Job identifier

        Returns:
            Next sequential step number for this job.
        """
        if job_id not in self._step_counters:
            self._step_counters[job_id] = 0
        self._step_counters[job_id] += 1
        return self._step_counters[job_id]

    def _truncate_string(self, s: str, max_length: int = 500) -> str:
        """Truncate a string to max_length with ellipsis indicator."""
        if not s or len(s) <= max_length:
            return s
        return s[:max_length] + "... [truncated]"

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
                # Token usage from response metadata (includes reasoning_tokens for supported models)
                "token_usage": getattr(response, "response_metadata", {}).get("token_usage", {}),
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

    # =========================================================================
    # Agent Audit Methods - Complete execution history tracking
    # =========================================================================

    def audit_step(
        self,
        job_id: str,
        agent_type: str,
        step_type: str,
        node_name: str,
        iteration: int,
        data: Optional[Dict[str, Any]] = None,
        latency_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Audit any step in the agent workflow.

        Args:
            job_id: Job identifier
            agent_type: Agent type (e.g., "creator", "validator")
            step_type: Type of step ("initialize", "llm_call", "llm_response",
                       "tool_call", "tool_result", "check", "routing", "error")
            node_name: Graph node name ("initialize", "process", "tools", "check")
            iteration: Current iteration number
            data: Step-specific data (llm, tool, routing, state, error info)
            latency_ms: Operation latency in milliseconds
            metadata: Additional metadata

        Returns:
            Inserted document ID, or None if audit failed.
        """
        if not self._ensure_connected():
            return None

        try:
            step_number = self._get_next_step_number(job_id)

            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "iteration": iteration,
                "step_number": step_number,
                "step_type": step_type,
                "node_name": node_name,
                "timestamp": datetime.now(timezone.utc),
            }

            if latency_ms is not None:
                doc["latency_ms"] = latency_ms

            if metadata:
                doc["metadata"] = metadata

            # Merge step-specific data
            if data:
                doc.update(data)

            result = self._audit_collection.insert_one(doc)
            doc_id = str(result.inserted_id)

            logger.debug(
                f"[AUDIT] {doc_id[-8:]} | job={job_id[:8]}... | "
                f"iter={iteration} | step={step_number} | {step_type}"
            )
            return doc_id

        except Exception as e:
            logger.warning(f"Failed to audit step: {e}")
            return None

    def audit_tool_call(
        self,
        job_id: str,
        agent_type: str,
        iteration: int,
        tool_name: str,
        call_id: str,
        arguments: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Audit a tool call before execution.

        Args:
            job_id: Job identifier
            agent_type: Agent type
            iteration: Current iteration number
            tool_name: Name of the tool being called
            call_id: Tool call ID from LLM
            arguments: Tool arguments
            metadata: Additional metadata

        Returns:
            Inserted document ID, or None if audit failed.
        """
        # Truncate large arguments for storage
        args_preview = {}
        for key, value in arguments.items():
            if isinstance(value, str):
                args_preview[key] = self._truncate_string(value, 200)
            elif isinstance(value, (dict, list)):
                args_str = str(value)
                args_preview[key] = self._truncate_string(args_str, 200)
            else:
                args_preview[key] = value

        return self.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type="tool_call",
            node_name="tools",
            iteration=iteration,
            data={
                "tool": {
                    "name": tool_name,
                    "call_id": call_id,
                    "arguments": args_preview,
                }
            },
            metadata=metadata,
        )

    def audit_tool_result(
        self,
        job_id: str,
        agent_type: str,
        iteration: int,
        tool_name: str,
        call_id: str,
        result: str,
        success: bool,
        latency_ms: int,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Audit a tool result after execution.

        Args:
            job_id: Job identifier
            agent_type: Agent type
            iteration: Current iteration number
            tool_name: Name of the tool that executed
            call_id: Tool call ID
            result: Tool result content
            success: Whether tool succeeded
            latency_ms: Tool execution time
            error: Error message if failed
            metadata: Additional metadata

        Returns:
            Inserted document ID, or None if audit failed.
        """
        tool_data = {
            "name": tool_name,
            "call_id": call_id,
            "result_preview": self._truncate_string(result, 500),
            "result_size_bytes": len(result) if result else 0,
            "success": success,
        }
        if error:
            tool_data["error"] = self._truncate_string(error, 500)

        return self.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type="tool_result",
            node_name="tools",
            iteration=iteration,
            data={"tool": tool_data},
            latency_ms=latency_ms,
            metadata=metadata,
        )

    def get_job_audit_trail(
        self,
        job_id: str,
        step_type: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Get complete audit trail for a job.

        Args:
            job_id: Job identifier
            step_type: Optional filter by step type
            limit: Maximum number of records

        Returns:
            List of audit records, sorted by step_number ascending.
        """
        if not self._ensure_connected():
            return []

        try:
            query = {"job_id": job_id}
            if step_type:
                query["step_type"] = step_type

            cursor = (
                self._audit_collection.find(query)
                .sort("step_number", 1)
                .limit(limit)
            )
            return list(cursor)

        except Exception as e:
            logger.warning(f"Failed to get audit trail: {e}")
            return []

    def get_audit_stats(self, job_id: str) -> Dict[str, Any]:
        """Get audit statistics for a job.

        Args:
            job_id: Job identifier

        Returns:
            Dict with audit statistics by step type.
        """
        if not self._ensure_connected():
            return {}

        try:
            pipeline = [
                {"$match": {"job_id": job_id}},
                {
                    "$group": {
                        "_id": "$step_type",
                        "count": {"$sum": 1},
                        "avg_latency_ms": {"$avg": "$latency_ms"},
                        "total_latency_ms": {"$sum": "$latency_ms"},
                    }
                },
            ]

            results = list(self._audit_collection.aggregate(pipeline))

            stats = {
                "total_steps": 0,
                "by_step_type": {},
            }
            for r in results:
                step_type = r["_id"]
                stats["by_step_type"][step_type] = {
                    "count": r["count"],
                    "avg_latency_ms": r.get("avg_latency_ms"),
                    "total_latency_ms": r.get("total_latency_ms"),
                }
                stats["total_steps"] += r["count"]

            # Also get first/last timestamps
            time_pipeline = [
                {"$match": {"job_id": job_id}},
                {
                    "$group": {
                        "_id": None,
                        "first_step": {"$min": "$timestamp"},
                        "last_step": {"$max": "$timestamp"},
                        "max_iteration": {"$max": "$iteration"},
                    }
                },
            ]
            time_results = list(self._audit_collection.aggregate(time_pipeline))
            if time_results:
                stats["first_step"] = time_results[0].get("first_step")
                stats["last_step"] = time_results[0].get("last_step")
                stats["max_iteration"] = time_results[0].get("max_iteration")

            return stats

        except Exception as e:
            logger.warning(f"Failed to get audit stats: {e}")
            return {}

    def close(self):
        """Close MongoDB connection."""
        if self._mongo_db:
            self._mongo_db.close()
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
