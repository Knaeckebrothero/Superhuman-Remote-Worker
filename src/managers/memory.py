"""Memory manager for nested loop graph architecture.

This module provides the MemoryManager for reading and writing the
workspace memory file (workspace.md). This file serves as the agent's
long-term memory, similar to CLAUDE.md - it's always available in
the system prompt.

The MemoryManager is a service (stateless) - it reads/writes directly
to the workspace filesystem without holding state in memory.
"""

import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class MemoryManager:
    """Service for workspace.md (long-term memory) operations.

    The MemoryManager provides access to the agent's persistent memory file.
    workspace.md is always injected into the system prompt (like CLAUDE.md)
    and survives context compaction.

    Design principles:
    - Stateless service: No in-memory state
    - Workspace-backed: All operations go to filesystem
    - Flexible format: Markdown sections, no strict structure

    The memory file typically contains:
    - Current state/phase tracking
    - Key accomplishments
    - Important decisions and rationale
    - Working notes that need to persist

    Example:
        ```python
        mem_mgr = MemoryManager(workspace)

        # Initialize with template
        if not mem_mgr.exists():
            mem_mgr.write("# Workspace Memory\\n\\n## Current State\\n...")

        # Read full memory
        memory = mem_mgr.read()

        # Get specific section
        state = mem_mgr.get_section("Current State")
        ```
    """

    MEMORY_FILE = "workspace.md"

    def __init__(self, workspace: "WorkspaceManager"):
        """Initialize memory manager.

        Args:
            workspace: WorkspaceManager for file operations
        """
        self._workspace = workspace

    def exists(self) -> bool:
        """Check if the memory file exists.

        Returns:
            True if workspace.md exists
        """
        return self._workspace.exists(self.MEMORY_FILE)

    def read(self) -> str:
        """Read the memory file content.

        Returns:
            Memory content as string, or empty string if not found
        """
        if not self.exists():
            logger.debug(f"Memory file not found: {self.MEMORY_FILE}")
            return ""

        content = self._workspace.read_file(self.MEMORY_FILE)
        logger.debug(f"Read memory: {len(content)} characters")
        return content

    def write(self, content: str) -> None:
        """Write content to the memory file.

        Args:
            content: Memory content to write
        """
        self._workspace.write_file(self.MEMORY_FILE, content)
        logger.info(f"Wrote memory: {len(content)} characters")

    def get_section(self, section_name: str) -> Optional[str]:
        """Extract a specific section from the memory file.

        Sections are identified by markdown headers (## or ###).
        Returns content from the header until the next header of
        same or higher level.

        Args:
            section_name: Name of the section (without # prefix)

        Returns:
            Section content or None if not found

        Example:
            ```python
            # Given workspace.md:
            # ## Current State
            # Phase: 2
            # Status: In progress
            #
            # ## Accomplishments
            # - Completed extraction

            state = mem_mgr.get_section("Current State")
            # Returns: "Phase: 2\\nStatus: In progress"
            ```
        """
        content = self.read()
        if not content:
            return None

        # Escape special regex characters in section name
        escaped_name = re.escape(section_name)

        # Pattern to match section header (## or ###)
        # Captures everything until next header of same/higher level
        pattern = rf"^(##?#?)\s*{escaped_name}\s*\n(.*?)(?=^##?\s|\Z)"

        match = re.search(pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
        if match:
            section_content = match.group(2).strip()
            return section_content

        return None

    def update_section(self, section_name: str, new_content: str) -> bool:
        """Update a specific section in the memory file.

        If the section exists, replaces its content.
        If the section doesn't exist, appends it.

        Args:
            section_name: Name of the section (without # prefix)
            new_content: New content for the section

        Returns:
            True if updated successfully
        """
        content = self.read()

        if not content:
            # Create new file with just this section
            self.write(f"# Workspace Memory\n\n## {section_name}\n\n{new_content}\n")
            return True

        # Try to find and replace existing section
        escaped_name = re.escape(section_name)
        pattern = rf"(^##?\s*{escaped_name}\s*\n)(.*?)(?=^##\s|\Z)"

        def replace_section(match):
            header = match.group(1)
            return f"{header}{new_content}\n\n"

        new_memory, count = re.subn(
            pattern, replace_section, content, count=1, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE
        )

        if count > 0:
            self.write(new_memory)
            logger.info(f"Updated section: {section_name}")
            return True

        # Section not found - append it
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n## {section_name}\n\n{new_content}\n"
        self.write(content)
        logger.info(f"Added new section: {section_name}")
        return True

    def append_to_section(self, section_name: str, item: str) -> bool:
        """Append an item to a section.

        Useful for adding to lists like "Accomplishments" or "Notes".

        Args:
            section_name: Name of the section
            item: Item to append (will be prefixed with "- ")

        Returns:
            True if appended successfully
        """
        current = self.get_section(section_name)

        if current is None:
            # Create new section with the item
            return self.update_section(section_name, f"- {item}")

        # Append to existing section
        new_content = f"{current}\n- {item}"
        return self.update_section(section_name, new_content)

    def get_state(self) -> dict:
        """Parse the Current State section into a dictionary.

        Expects format like:
        ```
        ## Current State
        Phase: 2
        Status: In progress
        ```

        Returns:
            Dictionary of state values
        """
        section = self.get_section("Current State")
        if not section:
            return {}

        state = {}
        for line in section.split("\n"):
            line = line.strip()
            if ":" in line and not line.startswith("-"):
                key, value = line.split(":", 1)
                state[key.strip().lower()] = value.strip()

        return state

    def set_state(self, key: str, value: str) -> bool:
        """Update a key in the Current State section.

        Args:
            key: State key (e.g., "Phase", "Status")
            value: New value

        Returns:
            True if updated successfully
        """
        section = self.get_section("Current State")

        if section is None:
            # Create new Current State section
            return self.update_section("Current State", f"{key}: {value}")

        # Try to update existing key
        lines = section.split("\n")
        key_lower = key.lower()
        updated = False

        for i, line in enumerate(lines):
            if ":" in line:
                existing_key = line.split(":")[0].strip().lower()
                if existing_key == key_lower:
                    lines[i] = f"{key}: {value}"
                    updated = True
                    break

        if not updated:
            # Key not found, add it
            lines.append(f"{key}: {value}")

        return self.update_section("Current State", "\n".join(lines))
