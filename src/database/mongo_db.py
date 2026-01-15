"""MongoDB Database Manager with lazy connection for optional logging.

This module provides optional MongoDB support for:
- LLM request/response archiving
- Agent audit trail logging
- Tool invocation tracking

MongoDB is optional - the system gracefully degrades if unavailable.

Part of Phase 1 database refactoring - see docs/db_refactor.md
"""

import os
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
except ImportError:
    MongoClient = None

logger = logging.getLogger(__name__)


class MongoDB:
    """MongoDB manager with lazy connection for optional logging.

    This database is optional - if not configured or unavailable, operations
    silently return None/empty lists and log warnings.

    Example:
        ```python
        db = MongoDB()

        # Archive LLM request (no error if MongoDB unavailable)
        request_id = db.archive_llm_request(
            job_id="abc-123",
            agent_type="creator",
            messages=[...],
            response={...},
            model="gpt-4"
        )

        # Get audit trail
        trail = db.get_job_audit_trail("abc-123")
        ```
    """

    def __init__(self, url: Optional[str] = None):
        """Initialize MongoDB manager with lazy connection.

        Args:
            url: MongoDB connection URL. Falls back to MONGODB_URL env var.
                Format: mongodb://host:port/database
        """
        if MongoClient is None:
            logger.warning(
                "pymongo not installed. MongoDB features disabled. "
                "Install with: pip install pymongo"
            )

        self._url = url or os.getenv('MONGODB_URL')
        self._client: Optional[MongoClient] = None
        self._db = None
        self._connected = False

        if not self._url:
            logger.info("MongoDB URL not configured. Logging features disabled.")

        logger.info("MongoDB initialized (lazy connection)")

    def _connect(self) -> bool:
        """Lazy connect to MongoDB.

        Returns:
            True if connected, False if unavailable or not configured
        """
        if self._connected:
            return True

        if MongoClient is None:
            return False

        if not self._url:
            return False

        try:
            self._client = MongoClient(
                self._url,
                serverSelectionTimeoutMS=5000  # 5 second timeout
            )
            # Test connection with ping
            self._client.admin.command('ping')

            # Extract database name from URL
            db_name = self._url.split('/')[-1].split('?')[0] or 'graphrag_logs'
            self._db = self._client[db_name]

            self._connected = True
            logger.info(f"MongoDB connected: {db_name}")
            return True

        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.warning(f"MongoDB connection failed: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.warning(f"MongoDB connection error: {e}")
            self._connected = False
            return False

    def close(self) -> None:
        """Close MongoDB connection.

        This method is idempotent - safe to call multiple times.
        """
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
            self._connected = False
            logger.info("MongoDB connection closed")

    def archive_llm_request(
        self,
        job_id: str,
        agent_type: str,
        messages: List[Dict],
        response: Dict,
        model: str,
        **metadata
    ) -> Optional[str]:
        """Archive LLM request/response for debugging and compliance.

        Args:
            job_id: Associated job ID
            agent_type: Agent type (e.g., "creator", "validator")
            messages: List of message dictionaries sent to LLM
            response: LLM response dictionary
            model: Model identifier (e.g., "gpt-4")
            **metadata: Additional metadata (tokens, duration, etc.)

        Returns:
            Document ID as string, or None if MongoDB unavailable
        """
        if not self._connect():
            return None

        try:
            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "messages": messages,
                "response": response,
                "model": model,
                "timestamp": datetime.utcnow(),
                **metadata
            }
            result = self._db.llm_requests.insert_one(doc)
            logger.debug(f"Archived LLM request: {result.inserted_id}")
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"Failed to archive LLM request: {e}")
            return None

    def audit_tool_call(
        self,
        job_id: str,
        agent_type: str,
        tool_name: str,
        inputs: Dict,
        output: Optional[Any] = None,
        error: Optional[str] = None,
        **metadata
    ) -> Optional[str]:
        """Audit a tool invocation.

        Args:
            job_id: Associated job ID
            agent_type: Agent type (e.g., "creator", "validator")
            tool_name: Name of invoked tool
            inputs: Tool input parameters
            output: Tool output (optional)
            error: Error message if tool failed (optional)
            **metadata: Additional metadata (duration, etc.)

        Returns:
            Document ID as string, or None if MongoDB unavailable
        """
        if not self._connect():
            return None

        try:
            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "event_type": "tool_call",
                "tool_name": tool_name,
                "inputs": inputs,
                "output": output,
                "error": error,
                "timestamp": datetime.utcnow(),
                **metadata
            }
            result = self._db.agent_audit.insert_one(doc)
            logger.debug(f"Audited tool call: {tool_name}")
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"Failed to audit tool call: {e}")
            return None

    def audit_phase_transition(
        self,
        job_id: str,
        agent_type: str,
        from_phase: str,
        to_phase: str,
        **metadata
    ) -> Optional[str]:
        """Audit a phase transition in nested loop architecture.

        Args:
            job_id: Associated job ID
            agent_type: Agent type
            from_phase: Previous phase (e.g., "INITIALIZATION", "OUTER_LOOP")
            to_phase: Next phase
            **metadata: Additional metadata

        Returns:
            Document ID as string, or None if MongoDB unavailable
        """
        if not self._connect():
            return None

        try:
            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "event_type": "phase_transition",
                "from_phase": from_phase,
                "to_phase": to_phase,
                "timestamp": datetime.utcnow(),
                **metadata
            }
            result = self._db.agent_audit.insert_one(doc)
            logger.debug(f"Audited phase transition: {from_phase} -> {to_phase}")
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"Failed to audit phase transition: {e}")
            return None

    def get_job_audit_trail(
        self,
        job_id: str,
        event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get complete audit trail for a job.

        Args:
            job_id: Job ID
            event_type: Filter by event type (optional, e.g., "tool_call", "phase_transition")

        Returns:
            List of audit records, or empty list if MongoDB unavailable
        """
        if not self._connect():
            return []

        try:
            query = {"job_id": job_id}
            if event_type:
                query["event_type"] = event_type

            cursor = self._db.agent_audit.find(query).sort("timestamp", 1)
            return list(cursor)
        except Exception as e:
            logger.error(f"Failed to get audit trail: {e}")
            return []

    def get_llm_conversation(
        self,
        job_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get LLM conversation history for a job.

        Args:
            job_id: Job ID
            limit: Maximum number of records to return

        Returns:
            List of LLM request/response records, or empty list if MongoDB unavailable
        """
        if not self._connect():
            return []

        try:
            cursor = self._db.llm_requests.find(
                {"job_id": job_id}
            ).sort("timestamp", 1).limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f"Failed to get LLM conversation: {e}")
            return []

    def get_statistics(self) -> Dict[str, Any]:
        """Get MongoDB collection statistics.

        Returns:
            Dictionary with collection counts, or empty dict if MongoDB unavailable
        """
        if not self._connect():
            return {}

        try:
            stats = {
                'llm_requests_count': self._db.llm_requests.count_documents({}),
                'agent_audit_count': self._db.agent_audit.count_documents({}),
                'connected': True
            }
            return stats
        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            return {'connected': False}

    @property
    def db(self):
        """Get database instance, connecting if needed.

        Returns:
            Database instance or None if unavailable
        """
        if not self._connect():
            return None
        return self._db

    @property
    def is_connected(self) -> bool:
        """Check if connected to MongoDB."""
        return self._connected


__all__ = ['MongoDB']
