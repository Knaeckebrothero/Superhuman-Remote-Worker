"""Workspace management for agent working data.

This module provides a workspace abstraction for agents to store and retrieve
working data during task execution. Workspaces support both PostgreSQL and
local file-based storage.
"""

import logging
import json
import os
from typing import Optional, Dict, Any, List
from datetime import datetime
from dataclasses import dataclass, field
import uuid

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceEntry:
    """A single entry in the workspace."""
    workspace_type: str
    data: Dict[str, Any]
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_type": self.workspace_type,
            "data": self.data,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class Workspace:
    """Agent workspace for storing working data.

    Provides persistent storage for:
    - Document chunks during preprocessing
    - Requirement candidates
    - Research notes and findings
    - Todo lists and progress tracking
    - Summaries from context compaction

    Supports both PostgreSQL (production) and file-based (development) storage.

    Example:
        ```python
        workspace = Workspace(
            job_id="job_123",
            agent="creator",
            postgres_conn=conn
        )

        # Save document chunks
        await workspace.save("chunks", chunks_data)

        # Retrieve later
        chunks = await workspace.load("chunks")

        # Save multiple types
        await workspace.save("candidates", candidates_data)
        await workspace.save("todo", todo_list)
        ```
    """

    # Standard workspace types
    TYPE_CHUNKS = "chunks"
    TYPE_CANDIDATES = "candidates"
    TYPE_RESEARCH = "research"
    TYPE_TODO = "todo"
    TYPE_SUMMARIES = "summaries"
    TYPE_NOTES = "notes"

    def __init__(
        self,
        job_id: str,
        agent: str,
        postgres_conn: Optional[Any] = None,
        storage_path: Optional[str] = None
    ):
        """Initialize workspace.

        Args:
            job_id: Job UUID for this workspace
            agent: Agent name ('creator' or 'validator')
            postgres_conn: Optional PostgresConnection for database storage
            storage_path: Optional file path for local storage
        """
        self.job_id = job_id
        self.agent = agent
        self.postgres_conn = postgres_conn
        self.storage_path = storage_path

        if storage_path:
            os.makedirs(storage_path, exist_ok=True)

    async def save(self, workspace_type: str, data: Any) -> None:
        """Save data to workspace.

        Args:
            workspace_type: Type identifier (e.g., 'chunks', 'candidates')
            data: Data to save (will be JSON serialized)
        """
        if self.postgres_conn:
            await self._save_to_postgres(workspace_type, data)
        elif self.storage_path:
            self._save_to_file(workspace_type, data)
        else:
            raise RuntimeError("No storage backend configured")

        logger.debug(f"Saved workspace data: {workspace_type} for job {self.job_id}")

    async def load(self, workspace_type: str) -> Optional[Any]:
        """Load data from workspace.

        Args:
            workspace_type: Type identifier

        Returns:
            Stored data or None if not found
        """
        if self.postgres_conn:
            return await self._load_from_postgres(workspace_type)
        elif self.storage_path:
            return self._load_from_file(workspace_type)

        return None

    async def update(self, workspace_type: str, data: Any) -> None:
        """Update existing workspace data.

        Args:
            workspace_type: Type identifier
            data: New data to store
        """
        if self.postgres_conn:
            await self._update_in_postgres(workspace_type, data)
        elif self.storage_path:
            self._save_to_file(workspace_type, data)  # File storage just overwrites
        else:
            raise RuntimeError("No storage backend configured")

        logger.debug(f"Updated workspace data: {workspace_type} for job {self.job_id}")

    async def delete(self, workspace_type: str) -> None:
        """Delete workspace data.

        Args:
            workspace_type: Type identifier
        """
        if self.postgres_conn:
            await self._delete_from_postgres(workspace_type)
        elif self.storage_path:
            self._delete_from_file(workspace_type)

        logger.debug(f"Deleted workspace data: {workspace_type} for job {self.job_id}")

    async def list_types(self) -> List[str]:
        """List all workspace types with stored data.

        Returns:
            List of workspace type identifiers
        """
        if self.postgres_conn:
            return await self._list_types_postgres()
        elif self.storage_path:
            return self._list_types_file()

        return []

    async def clear(self) -> int:
        """Clear all workspace data for this job/agent.

        Returns:
            Number of entries deleted
        """
        types = await self.list_types()
        for ws_type in types:
            await self.delete(ws_type)

        logger.info(f"Cleared {len(types)} workspace entries for job {self.job_id}")
        return len(types)

    # =========================================================================
    # Convenience Methods for Standard Types
    # =========================================================================

    async def save_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        """Save document chunks."""
        await self.save(self.TYPE_CHUNKS, {"chunks": chunks})

    async def load_chunks(self) -> Optional[List[Dict[str, Any]]]:
        """Load document chunks."""
        data = await self.load(self.TYPE_CHUNKS)
        return data.get("chunks") if data else None

    async def save_candidates(self, candidates: List[Dict[str, Any]]) -> None:
        """Save requirement candidates."""
        await self.save(self.TYPE_CANDIDATES, {"candidates": candidates})

    async def load_candidates(self) -> Optional[List[Dict[str, Any]]]:
        """Load requirement candidates."""
        data = await self.load(self.TYPE_CANDIDATES)
        return data.get("candidates") if data else None

    async def save_todo(self, todo_items: List[Dict[str, Any]]) -> None:
        """Save todo list."""
        await self.save(self.TYPE_TODO, {"items": todo_items})

    async def load_todo(self) -> Optional[List[Dict[str, Any]]]:
        """Load todo list."""
        data = await self.load(self.TYPE_TODO)
        return data.get("items") if data else None

    async def save_summary(self, summary: str) -> None:
        """Append a context summary to the summaries list."""
        existing = await self.load(self.TYPE_SUMMARIES) or {"summaries": []}
        existing["summaries"].append({
            "text": summary,
            "timestamp": datetime.utcnow().isoformat()
        })
        await self.save(self.TYPE_SUMMARIES, existing)

    async def load_summaries(self) -> List[Dict[str, Any]]:
        """Load all context summaries."""
        data = await self.load(self.TYPE_SUMMARIES)
        return data.get("summaries", []) if data else []

    async def append_note(self, note: str, category: str = "general") -> None:
        """Append a note to the notes list."""
        existing = await self.load(self.TYPE_NOTES) or {"notes": []}
        existing["notes"].append({
            "text": note,
            "category": category,
            "timestamp": datetime.utcnow().isoformat()
        })
        await self.save(self.TYPE_NOTES, existing)

    async def load_notes(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load notes, optionally filtered by category."""
        data = await self.load(self.TYPE_NOTES)
        notes = data.get("notes", []) if data else []

        if category:
            notes = [n for n in notes if n.get("category") == category]

        return notes

    # =========================================================================
    # PostgreSQL Storage
    # =========================================================================

    async def _save_to_postgres(self, workspace_type: str, data: Any) -> None:
        """Save to PostgreSQL candidate_workspace table."""
        from src.core.postgres_utils import save_workspace_data

        await save_workspace_data(
            self.postgres_conn,
            job_id=uuid.UUID(self.job_id),
            workspace_type=f"{self.agent}_{workspace_type}",
            data=data if isinstance(data, dict) else {"value": data}
        )

    async def _load_from_postgres(self, workspace_type: str) -> Optional[Any]:
        """Load from PostgreSQL."""
        from src.core.postgres_utils import get_workspace_data

        data = await get_workspace_data(
            self.postgres_conn,
            job_id=uuid.UUID(self.job_id),
            workspace_type=f"{self.agent}_{workspace_type}"
        )

        if data and "value" in data and len(data) == 1:
            return data["value"]
        return data

    async def _update_in_postgres(self, workspace_type: str, data: Any) -> None:
        """Update in PostgreSQL."""
        from src.core.postgres_utils import update_workspace_data

        await update_workspace_data(
            self.postgres_conn,
            job_id=uuid.UUID(self.job_id),
            workspace_type=f"{self.agent}_{workspace_type}",
            data=data if isinstance(data, dict) else {"value": data}
        )

    async def _delete_from_postgres(self, workspace_type: str) -> None:
        """Delete from PostgreSQL."""
        await self.postgres_conn.execute(
            """
            DELETE FROM candidate_workspace
            WHERE job_id = $1 AND workspace_type = $2
            """,
            uuid.UUID(self.job_id), f"{self.agent}_{workspace_type}"
        )

    async def _list_types_postgres(self) -> List[str]:
        """List workspace types from PostgreSQL."""
        prefix = f"{self.agent}_"
        rows = await self.postgres_conn.fetch(
            """
            SELECT DISTINCT workspace_type
            FROM candidate_workspace
            WHERE job_id = $1 AND workspace_type LIKE $2
            """,
            uuid.UUID(self.job_id), f"{prefix}%"
        )
        return [row["workspace_type"].replace(prefix, "") for row in rows]

    # =========================================================================
    # File Storage (Development)
    # =========================================================================

    def _get_filepath(self, workspace_type: str) -> str:
        """Get file path for a workspace type."""
        return os.path.join(
            self.storage_path,
            f"{self.job_id}_{self.agent}_{workspace_type}.json"
        )

    def _save_to_file(self, workspace_type: str, data: Any) -> None:
        """Save to local file."""
        filepath = self._get_filepath(workspace_type)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load_from_file(self, workspace_type: str) -> Optional[Any]:
        """Load from local file."""
        filepath = self._get_filepath(workspace_type)
        if os.path.exists(filepath):
            with open(filepath) as f:
                return json.load(f)
        return None

    def _delete_from_file(self, workspace_type: str) -> None:
        """Delete local file."""
        filepath = self._get_filepath(workspace_type)
        if os.path.exists(filepath):
            os.remove(filepath)

    def _list_types_file(self) -> List[str]:
        """List workspace types from files."""
        import glob

        prefix = f"{self.job_id}_{self.agent}_"
        pattern = os.path.join(self.storage_path, f"{prefix}*.json")
        files = glob.glob(pattern)

        types = []
        for f in files:
            filename = os.path.basename(f)
            ws_type = filename.replace(prefix, "").replace(".json", "")
            types.append(ws_type)

        return types
