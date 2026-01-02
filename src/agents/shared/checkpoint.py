"""Checkpoint management for agent state persistence and recovery.

This module provides utilities for saving and restoring agent state,
enabling recovery from failures and resumption of long-running tasks.
"""

import logging
import json
import uuid
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CheckpointData:
    """Container for agent checkpoint data."""
    job_id: str
    agent: str  # 'creator' or 'validator'
    thread_id: str
    checkpoint_id: str
    state: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_checkpoint_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize checkpoint to dictionary."""
        return {
            "job_id": self.job_id,
            "agent": self.agent,
            "thread_id": self.thread_id,
            "checkpoint_id": self.checkpoint_id,
            "state": self.state,
            "metadata": self.metadata,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckpointData":
        """Deserialize checkpoint from dictionary."""
        return cls(
            job_id=data["job_id"],
            agent=data["agent"],
            thread_id=data["thread_id"],
            checkpoint_id=data["checkpoint_id"],
            state=data["state"],
            metadata=data.get("metadata", {}),
            parent_checkpoint_id=data.get("parent_checkpoint_id"),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else datetime.utcnow(),
        )


class CheckpointManager:
    """Manages agent checkpoints for state persistence and recovery.

    Provides methods to save and restore agent state, enabling:
    - Recovery from crashes or failures
    - Resumption of long-running tasks
    - State inspection for debugging

    The manager supports both PostgreSQL (via postgres_utils) and
    local file-based storage for development.

    Example:
        ```python
        # With PostgreSQL
        checkpoint_mgr = CheckpointManager(
            postgres_conn=conn,
            job_id="job_123",
            agent="creator"
        )

        # Save checkpoint
        await checkpoint_mgr.save(state_dict)

        # Restore latest checkpoint
        state = await checkpoint_mgr.restore_latest()
        ```
    """

    def __init__(
        self,
        job_id: str,
        agent: str,
        postgres_conn: Optional[Any] = None,
        storage_path: Optional[str] = None,
        thread_id: Optional[str] = None
    ):
        """Initialize checkpoint manager.

        Args:
            job_id: Job UUID for this checkpoint context
            agent: Agent name ('creator' or 'validator')
            postgres_conn: Optional PostgresConnection for database storage
            storage_path: Optional file path for local storage (dev mode)
            thread_id: Optional thread ID (auto-generated if not provided)
        """
        self.job_id = job_id
        self.agent = agent
        self.postgres_conn = postgres_conn
        self.storage_path = storage_path
        self.thread_id = thread_id or str(uuid.uuid4())
        self._checkpoint_count = 0
        self._last_checkpoint_id: Optional[str] = None

    async def save(
        self,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Save a checkpoint of the current agent state.

        Args:
            state: Agent state dictionary to persist
            metadata: Optional metadata about this checkpoint

        Returns:
            Checkpoint ID

        Raises:
            RuntimeError: If no storage backend is configured
        """
        checkpoint_id = f"cp_{self._checkpoint_count}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        checkpoint = CheckpointData(
            job_id=self.job_id,
            agent=self.agent,
            thread_id=self.thread_id,
            checkpoint_id=checkpoint_id,
            state=self._serialize_state(state),
            metadata=metadata or {},
            parent_checkpoint_id=self._last_checkpoint_id,
        )

        if self.postgres_conn:
            await self._save_to_postgres(checkpoint)
        elif self.storage_path:
            self._save_to_file(checkpoint)
        else:
            raise RuntimeError("No storage backend configured for checkpoints")

        self._checkpoint_count += 1
        self._last_checkpoint_id = checkpoint_id

        logger.info(f"Saved checkpoint {checkpoint_id} for {self.agent}")
        return checkpoint_id

    async def restore_latest(self) -> Optional[Dict[str, Any]]:
        """Restore the most recent checkpoint.

        Returns:
            State dictionary from latest checkpoint, or None if no checkpoints exist
        """
        checkpoint = await self._load_latest_checkpoint()

        if checkpoint:
            logger.info(f"Restored checkpoint {checkpoint.checkpoint_id} for {self.agent}")
            self._last_checkpoint_id = checkpoint.checkpoint_id
            return self._deserialize_state(checkpoint.state)

        logger.info(f"No checkpoints found for {self.agent} in job {self.job_id}")
        return None

    async def restore_by_id(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        """Restore a specific checkpoint by ID.

        Args:
            checkpoint_id: The checkpoint ID to restore

        Returns:
            State dictionary from the checkpoint, or None if not found
        """
        checkpoint = await self._load_checkpoint_by_id(checkpoint_id)

        if checkpoint:
            logger.info(f"Restored checkpoint {checkpoint_id}")
            return self._deserialize_state(checkpoint.state)

        logger.warning(f"Checkpoint {checkpoint_id} not found")
        return None

    async def list_checkpoints(self) -> list[Dict[str, Any]]:
        """List all checkpoints for this job/agent.

        Returns:
            List of checkpoint metadata dictionaries
        """
        if self.postgres_conn:
            return await self._list_checkpoints_postgres()
        elif self.storage_path:
            return self._list_checkpoints_file()
        return []

    async def delete_old_checkpoints(self, keep_count: int = 5) -> int:
        """Delete old checkpoints, keeping the most recent ones.

        Args:
            keep_count: Number of recent checkpoints to keep

        Returns:
            Number of checkpoints deleted
        """
        checkpoints = await self.list_checkpoints()

        if len(checkpoints) <= keep_count:
            return 0

        to_delete = checkpoints[keep_count:]
        deleted = 0

        for cp in to_delete:
            try:
                await self._delete_checkpoint(cp["checkpoint_id"])
                deleted += 1
            except Exception as e:
                logger.warning(f"Failed to delete checkpoint {cp['checkpoint_id']}: {e}")

        logger.info(f"Deleted {deleted} old checkpoints for {self.agent}")
        return deleted

    # =========================================================================
    # PostgreSQL Storage
    # =========================================================================

    async def _save_to_postgres(self, checkpoint: CheckpointData) -> None:
        """Save checkpoint to PostgreSQL."""
        from ..core.postgres_utils import save_checkpoint

        await save_checkpoint(
            self.postgres_conn,
            job_id=uuid.UUID(self.job_id),
            agent=self.agent,
            thread_id=self.thread_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_data=checkpoint.state,
            parent_checkpoint_id=checkpoint.parent_checkpoint_id,
            metadata=checkpoint.metadata,
        )

    async def _load_latest_checkpoint(self) -> Optional[CheckpointData]:
        """Load the latest checkpoint from storage."""
        if self.postgres_conn:
            from ..core.postgres_utils import get_latest_checkpoint

            row = await get_latest_checkpoint(
                self.postgres_conn,
                job_id=uuid.UUID(self.job_id),
                agent=self.agent
            )
            if row:
                return CheckpointData(
                    job_id=str(row["job_id"]),
                    agent=row["agent"],
                    thread_id=row["thread_id"],
                    checkpoint_id=row["checkpoint_id"],
                    state=row["checkpoint_data"],
                    metadata=row.get("metadata", {}),
                    parent_checkpoint_id=row.get("parent_checkpoint_id"),
                    created_at=row["created_at"],
                )
        elif self.storage_path:
            return self._load_latest_checkpoint_file()

        return None

    async def _load_checkpoint_by_id(self, checkpoint_id: str) -> Optional[CheckpointData]:
        """Load a specific checkpoint by ID."""
        if self.postgres_conn:
            row = await self.postgres_conn.fetchrow(
                """
                SELECT * FROM agent_checkpoints
                WHERE job_id = $1 AND agent = $2 AND checkpoint_id = $3
                """,
                uuid.UUID(self.job_id), self.agent, checkpoint_id
            )
            if row:
                return CheckpointData(
                    job_id=str(row["job_id"]),
                    agent=row["agent"],
                    thread_id=row["thread_id"],
                    checkpoint_id=row["checkpoint_id"],
                    state=row["checkpoint_data"],
                    metadata=row.get("metadata", {}),
                    parent_checkpoint_id=row.get("parent_checkpoint_id"),
                    created_at=row["created_at"],
                )
        elif self.storage_path:
            return self._load_checkpoint_by_id_file(checkpoint_id)

        return None

    async def _list_checkpoints_postgres(self) -> list[Dict[str, Any]]:
        """List checkpoints from PostgreSQL."""
        rows = await self.postgres_conn.fetch(
            """
            SELECT checkpoint_id, created_at, metadata
            FROM agent_checkpoints
            WHERE job_id = $1 AND agent = $2
            ORDER BY created_at DESC
            """,
            uuid.UUID(self.job_id), self.agent
        )
        return [
            {
                "checkpoint_id": row["checkpoint_id"],
                "created_at": row["created_at"].isoformat(),
                "metadata": row.get("metadata", {}),
            }
            for row in rows
        ]

    async def _delete_checkpoint(self, checkpoint_id: str) -> None:
        """Delete a checkpoint."""
        if self.postgres_conn:
            await self.postgres_conn.execute(
                """
                DELETE FROM agent_checkpoints
                WHERE job_id = $1 AND agent = $2 AND checkpoint_id = $3
                """,
                uuid.UUID(self.job_id), self.agent, checkpoint_id
            )
        elif self.storage_path:
            self._delete_checkpoint_file(checkpoint_id)

    # =========================================================================
    # File Storage (Development)
    # =========================================================================

    def _save_to_file(self, checkpoint: CheckpointData) -> None:
        """Save checkpoint to local file."""
        import os

        os.makedirs(self.storage_path, exist_ok=True)
        filepath = os.path.join(
            self.storage_path,
            f"{self.job_id}_{self.agent}_{checkpoint.checkpoint_id}.json"
        )

        with open(filepath, "w") as f:
            json.dump(checkpoint.to_dict(), f, indent=2, default=str)

    def _load_latest_checkpoint_file(self) -> Optional[CheckpointData]:
        """Load latest checkpoint from files."""
        import os
        import glob

        pattern = os.path.join(self.storage_path, f"{self.job_id}_{self.agent}_*.json")
        files = sorted(glob.glob(pattern), reverse=True)

        if files:
            with open(files[0]) as f:
                data = json.load(f)
                return CheckpointData.from_dict(data)

        return None

    def _load_checkpoint_by_id_file(self, checkpoint_id: str) -> Optional[CheckpointData]:
        """Load specific checkpoint from file."""
        import os

        filepath = os.path.join(
            self.storage_path,
            f"{self.job_id}_{self.agent}_{checkpoint_id}.json"
        )

        if os.path.exists(filepath):
            with open(filepath) as f:
                data = json.load(f)
                return CheckpointData.from_dict(data)

        return None

    def _list_checkpoints_file(self) -> list[Dict[str, Any]]:
        """List checkpoints from files."""
        import os
        import glob

        pattern = os.path.join(self.storage_path, f"{self.job_id}_{self.agent}_*.json")
        files = sorted(glob.glob(pattern), reverse=True)

        result = []
        for filepath in files:
            with open(filepath) as f:
                data = json.load(f)
                result.append({
                    "checkpoint_id": data["checkpoint_id"],
                    "created_at": data["created_at"],
                    "metadata": data.get("metadata", {}),
                })

        return result

    def _delete_checkpoint_file(self, checkpoint_id: str) -> None:
        """Delete checkpoint file."""
        import os

        filepath = os.path.join(
            self.storage_path,
            f"{self.job_id}_{self.agent}_{checkpoint_id}.json"
        )

        if os.path.exists(filepath):
            os.remove(filepath)

    # =========================================================================
    # Serialization Helpers
    # =========================================================================

    def _serialize_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Serialize agent state for storage.

        Handles special types like LangChain messages.
        """
        serialized = {}

        for key, value in state.items():
            if key == "messages":
                # Serialize LangChain messages
                serialized[key] = [
                    {
                        "type": type(m).__name__,
                        "content": m.content,
                        "additional_kwargs": getattr(m, "additional_kwargs", {}),
                    }
                    for m in value
                ] if value else []
            elif hasattr(value, "to_dict"):
                serialized[key] = value.to_dict()
            elif isinstance(value, (str, int, float, bool, type(None))):
                serialized[key] = value
            elif isinstance(value, (list, dict)):
                serialized[key] = json.loads(json.dumps(value, default=str))
            else:
                serialized[key] = str(value)

        return serialized

    def _deserialize_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Deserialize agent state from storage."""
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

        deserialized = {}

        for key, value in state.items():
            if key == "messages" and isinstance(value, list):
                # Reconstruct LangChain messages
                messages = []
                for m in value:
                    msg_type = m.get("type", "HumanMessage")
                    content = m.get("content", "")

                    if msg_type == "HumanMessage":
                        messages.append(HumanMessage(content=content))
                    elif msg_type == "AIMessage":
                        messages.append(AIMessage(content=content))
                    elif msg_type == "SystemMessage":
                        messages.append(SystemMessage(content=content))
                    elif msg_type == "ToolMessage":
                        messages.append(ToolMessage(
                            content=content,
                            tool_call_id=m.get("additional_kwargs", {}).get("tool_call_id", "")
                        ))
                    else:
                        messages.append(HumanMessage(content=content))

                deserialized[key] = messages
            else:
                deserialized[key] = value

        return deserialized

    @property
    def checkpoint_count(self) -> int:
        """Number of checkpoints saved in this session."""
        return self._checkpoint_count

    @property
    def last_checkpoint_id(self) -> Optional[str]:
        """ID of the most recently saved checkpoint."""
        return self._last_checkpoint_id
