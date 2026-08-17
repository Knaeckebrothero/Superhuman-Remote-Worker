"""Gitea-backed parsing for job todo files.

Workers push their todo state to the job's Gitea repo at every phase
boundary: ``todos.yaml`` (current-phase list; the staged-todos flow may
never write one) and ``archive/todos_phase_{N}_{type}_{ts}.md`` phase
archives written by ``TodoManager.archive()`` (src/managers/todo.py).

These helpers parse that pushed content into the response shapes the
``/api/jobs/{id}/todos*`` routes have always served — the shapes are frozen
by the cockpit todo view (cockpit/src/app/core/models/todo.model.ts); only
the data source moved from the orchestrator-local ``WorkspaceService`` disk
relic to Gitea. Staleness contract: committed state as of the worker's last
phase-boundary push. See
knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md §4 P0-B.
"""

import re
from typing import Any

import yaml


def parse_current_todos(content: str) -> dict[str, Any] | None:
    """Parse todos.yaml content into the current-todos response shape.

    Args:
        content: Raw todos.yaml text (as pushed to the job repo)

    Returns:
        Dict with todos list and metadata, or None if unparseable
    """
    try:
        data = yaml.safe_load(content)

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


def build_archive_listing(
    entries: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build the archive-listing response from a Gitea ``archive/`` directory.

    Args:
        entries: Gitea contents entries (name, path, type, size) for the
            repo's ``archive/`` directory, or None when the directory is
            missing / Gitea is unavailable

    Returns:
        List of archive metadata (filename, phase_name, timestamp, path),
        newest first
    """
    archives: list[dict[str, Any]] = []

    for entry in entries or []:
        filename = entry.get("name", "")
        if entry.get("type") != "file":
            continue
        if not (filename.startswith("todos_") and filename.endswith(".md")):
            continue

        # Extract phase name and timestamp from filename
        # Format: todos_{phase_name}_{YYYYMMDD_HHMMSS}.md
        name = filename[: -len(".md")]  # todos_phase_name_20260124_183618
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

        archives.append(
            {
                "filename": filename,
                "phase_name": phase_name or name.replace("todos_", ""),
                "timestamp": timestamp,
                "path": entry.get("path") or f"archive/{filename}",
            }
        )

    # Sort by timestamp (newest first)
    archives.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return archives


def parse_archived_todos(content: str, filename: str) -> dict[str, Any]:
    """Parse archived todo markdown into structured data.

    Args:
        content: Markdown content (as written by ``TodoManager.archive()``)
        filename: Source filename

    Returns:
        Dict with todos, summary, and metadata
    """
    result: dict[str, Any] = {
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
            result["phase_name"] = line_stripped.replace(
                "# Archived Todos:", ""
            ).strip()
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
        elif current_section in (
            "completed",
            "not_completed",
        ) and line_stripped.startswith("- ["):
            # Parse todo line: - [x] Content or - [ ] Content or - [~] Content
            match = re.match(r"- \[([x ~])\] (.+)", line_stripped)
            if match:
                status_char = match.group(1)
                todo_content = match.group(2)

                status = (
                    "completed"
                    if status_char == "x"
                    else ("in_progress" if status_char == "~" else "pending")
                )

                current_todo = {
                    "content": todo_content,
                    "status": status,
                    "notes": [],
                }
                result["todos"].append(current_todo)

        # Parse todo notes (indented under todo)
        elif (
            current_todo
            and line.startswith("  - ")
            and current_section in ("completed", "not_completed")
        ):
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
        elif (
            current_section == "failure_note"
            and line_stripped
            and not line_stripped.startswith("#")
        ):
            if result["failure_note"]:
                result["failure_note"] += "\n" + line_stripped
            else:
                result["failure_note"] = line_stripped

    return result
