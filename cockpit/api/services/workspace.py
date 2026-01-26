"""Workspace service for accessing job workspace files.

Provides access to:
- Current todos (todos.yaml)
- Archived todos (archive/todos_*.md)
- Workspace metadata
"""

import re
from pathlib import Path
from typing import Any

import yaml


class WorkspaceService:
    """Service for reading job workspace files."""

    def __init__(self, workspace_base: str | None = None):
        """Initialize workspace service.

        Args:
            workspace_base: Base path for workspaces. If None, uses default location.
        """
        if workspace_base:
            self._base = Path(workspace_base)
        else:
            # Default: relative to project root
            self._base = Path(__file__).parent.parent.parent.parent / "workspace"

    def _get_job_path(self, job_id: str) -> Path | None:
        """Get the workspace path for a job.

        Args:
            job_id: Job UUID

        Returns:
            Path to job workspace or None if not found
        """
        job_path = self._base / f"job_{job_id}"
        if job_path.exists() and job_path.is_dir():
            return job_path
        return None

    def get_current_todos(self, job_id: str) -> dict[str, Any] | None:
        """Get current todos from todos.yaml.

        Args:
            job_id: Job UUID

        Returns:
            Dict with todos list and metadata, or None if not found
        """
        job_path = self._get_job_path(job_id)
        if not job_path:
            return None

        todos_file = job_path / "todos.yaml"
        if not todos_file.exists():
            return None

        try:
            with open(todos_file) as f:
                data = yaml.safe_load(f)

            if not data:
                return {"todos": [], "source": "todos.yaml"}

            # Handle both list and dict formats
            if isinstance(data, list):
                todos = data
            elif isinstance(data, dict):
                todos = data.get("todos", [])
            else:
                todos = []

            return {
                "todos": todos,
                "source": "todos.yaml",
                "is_current": True,
            }
        except Exception:
            return None

    def list_archived_todos(self, job_id: str) -> list[dict[str, Any]]:
        """List all archived todo files for a job.

        Args:
            job_id: Job UUID

        Returns:
            List of archive metadata (name, path, timestamp)
        """
        job_path = self._get_job_path(job_id)
        if not job_path:
            return []

        archive_dir = job_path / "archive"
        if not archive_dir.exists():
            return []

        archives = []
        for f in archive_dir.glob("todos_*.md"):
            # Extract phase name and timestamp from filename
            # Format: todos_{phase_name}_{YYYYMMDD_HHMMSS}.md
            name = f.stem  # todos_phase_name_20260124_183618
            parts = name.split("_")

            # Try to extract timestamp (last 2 parts should be date and time)
            timestamp = None
            phase_name = None
            if len(parts) >= 3:
                try:
                    date_part = parts[-2]  # YYYYMMDD
                    time_part = parts[-1]  # HHMMSS
                    if len(date_part) == 8 and len(time_part) == 6:
                        timestamp = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
                        # Phase name is everything between "todos_" and the timestamp
                        phase_name = "_".join(parts[1:-2]) if len(parts) > 3 else None
                except (ValueError, IndexError):
                    pass

            archives.append({
                "filename": f.name,
                "phase_name": phase_name or name.replace("todos_", ""),
                "timestamp": timestamp,
                "path": str(f.relative_to(job_path)),
            })

        # Sort by timestamp (newest first)
        archives.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
        return archives

    def get_archived_todos(self, job_id: str, filename: str) -> dict[str, Any] | None:
        """Get parsed content of an archived todo file.

        Args:
            job_id: Job UUID
            filename: Archive filename (e.g., "todos_phase1_20260124_183618.md")

        Returns:
            Dict with parsed todos and metadata, or None if not found
        """
        job_path = self._get_job_path(job_id)
        if not job_path:
            return None

        # Security: ensure filename is safe
        if ".." in filename or "/" in filename or "\\" in filename:
            return None

        archive_file = job_path / "archive" / filename
        if not archive_file.exists():
            return None

        try:
            content = archive_file.read_text()
            return self._parse_archived_todos(content, filename)
        except Exception:
            return None

    def _parse_archived_todos(self, content: str, filename: str) -> dict[str, Any]:
        """Parse archived todo markdown into structured data.

        Args:
            content: Markdown content
            filename: Source filename

        Returns:
            Dict with todos, summary, and metadata
        """
        result = {
            "source": filename,
            "is_current": False,
            "todos": [],
            "summary": {},
            "phase_name": None,
            "archived_at": None,
            "failure_note": None,
        }

        lines = content.split("\n")
        current_section = None
        current_todo = None

        for line in lines:
            line_stripped = line.strip()

            # Parse header for phase name
            if line_stripped.startswith("# Archived Todos:"):
                result["phase_name"] = line_stripped.replace("# Archived Todos:", "").strip()
            elif line_stripped.startswith("Archived:"):
                result["archived_at"] = line_stripped.replace("Archived:", "").strip()

            # Section headers
            elif line_stripped.startswith("## Completed"):
                current_section = "completed"
                # Extract count from "## Completed (N)"
                match = re.search(r"\((\d+)\)", line_stripped)
                if match:
                    result["summary"]["completed"] = int(match.group(1))
            elif line_stripped.startswith("## Not Completed"):
                current_section = "not_completed"
                match = re.search(r"\((\d+)\)", line_stripped)
                if match:
                    result["summary"]["not_completed"] = int(match.group(1))
            elif line_stripped.startswith("## Summary"):
                current_section = "summary"
            elif line_stripped.startswith("## Failure Note"):
                current_section = "failure_note"

            # Parse todos
            elif current_section in ("completed", "not_completed") and line_stripped.startswith("- ["):
                # Parse todo line: - [x] Content or - [ ] Content or - [~] Content
                match = re.match(r"- \[([x ~])\] (.+)", line_stripped)
                if match:
                    status_char = match.group(1)
                    content = match.group(2)

                    status = "completed" if status_char == "x" else ("in_progress" if status_char == "~" else "pending")

                    current_todo = {
                        "content": content,
                        "status": status,
                        "notes": [],
                    }
                    result["todos"].append(current_todo)

            # Parse todo notes (indented under todo)
            elif current_todo and line.startswith("  - ") and current_section in ("completed", "not_completed"):
                note = line.strip()[2:]  # Remove "- " prefix
                current_todo["notes"].append(note)

            # Parse summary lines
            elif current_section == "summary" and line_stripped.startswith("- "):
                match = re.match(r"- (\w+): (\d+)", line_stripped)
                if match:
                    key = match.group(1).lower()
                    value = int(match.group(2))
                    result["summary"][key] = value

            # Parse failure note
            elif current_section == "failure_note" and line_stripped and not line_stripped.startswith("#"):
                if result["failure_note"]:
                    result["failure_note"] += "\n" + line_stripped
                else:
                    result["failure_note"] = line_stripped

        return result

    def get_all_todos(self, job_id: str) -> dict[str, Any]:
        """Get all todos for a job (current + archives).

        Args:
            job_id: Job UUID

        Returns:
            Dict with current todos and list of archives
        """
        current = self.get_current_todos(job_id)
        archives = self.list_archived_todos(job_id)

        return {
            "job_id": job_id,
            "current": current,
            "archives": archives,
            "has_workspace": self._get_job_path(job_id) is not None,
        }


# Global service instance
workspace_service = WorkspaceService()
