"""Plan manager for nested loop graph architecture.

This module provides the PlanManager for reading and writing the
main plan file (main_plan.md). The plan contains the strategic
direction for the job and is read at phase transitions.

The PlanManager is a service (stateless) - it reads/writes directly
to the workspace filesystem without holding state in memory.
"""

import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class PlanManager:
    """Service for main_plan.md operations.

    The PlanManager provides access to the strategic plan file.
    The plan is created during initialization and read at each
    phase transition to determine next steps.

    Design principles:
    - Stateless service: No in-memory state
    - Workspace-backed: All operations go to filesystem
    - Flexible format: No strict structure enforcement

    The plan file typically contains:
    - Overall goal description
    - Phases with numbered steps
    - Status markers for completed phases

    Example:
        ```python
        plan_mgr = PlanManager(workspace)

        # Check if plan exists
        if not plan_mgr.exists():
            plan_mgr.write("# Plan\\n\\n## Phase 1: Setup\\n...")

        # Read plan content
        plan = plan_mgr.read()

        # Check if all phases are done
        if plan_mgr.is_complete():
            print("Job complete!")
        ```
    """

    PLAN_FILE = "main_plan.md"

    def __init__(self, workspace: "WorkspaceManager"):
        """Initialize plan manager.

        Args:
            workspace: WorkspaceManager for file operations
        """
        self._workspace = workspace

    def exists(self) -> bool:
        """Check if the plan file exists.

        Returns:
            True if main_plan.md exists in workspace
        """
        return self._workspace.exists(self.PLAN_FILE)

    def read(self) -> str:
        """Read the plan file content.

        Returns:
            Plan content as string, or empty string if not found
        """
        if not self.exists():
            logger.warning(f"Plan file not found: {self.PLAN_FILE}")
            return ""

        content = self._workspace.read_file(self.PLAN_FILE)
        logger.debug(f"Read plan: {len(content)} characters")
        return content

    def write(self, content: str) -> None:
        """Write content to the plan file.

        Args:
            content: Plan content to write
        """
        self._workspace.write_file(self.PLAN_FILE, content)
        logger.info(f"Wrote plan: {len(content)} characters")

    def is_complete(self, content: Optional[str] = None) -> bool:
        """Check if all phases in the plan are marked complete.

        Uses heuristics to detect completion:
        1. Looks for explicit "COMPLETE" or "DONE" markers
        2. Checks if all phases have completion checkmarks
        3. Looks for "Goal achieved" or similar phrases

        This is intentionally flexible - no strict format enforcement.

        Args:
            content: Optional plan content (reads from file if not provided)

        Returns:
            True if plan appears to be complete
        """
        if content is None:
            content = self.read()

        if not content:
            return False

        content_lower = content.lower()

        # Check for explicit completion markers at document level
        completion_markers = [
            "# complete",
            "## complete",
            "status: complete",
            "status: done",
            "goal achieved",
            "all phases complete",
            "job complete",
        ]
        for marker in completion_markers:
            if marker in content_lower:
                return True

        # Check for phase completion pattern
        # Look for phases like "## Phase 1" or "### Step 1"
        phase_pattern = r"(?:##?\s*(?:phase|step)\s*\d+)"
        phases = re.findall(phase_pattern, content_lower)

        if not phases:
            # No phases found - can't determine completion
            return False

        # Look for uncompleted phase markers
        # Phases are incomplete if they have:
        # - [ ] unchecked todos
        # - "in progress", "pending", "todo" status
        incomplete_markers = [
            "- [ ]",  # Unchecked checkbox
            "status: pending",
            "status: in progress",
            "status: todo",
            "(pending)",
            "(in progress)",
        ]

        for marker in incomplete_markers:
            if marker in content_lower:
                return False

        # If we found phases and no incomplete markers, consider complete
        # Also check for at least one completed marker
        completed_markers = [
            "- [x]",  # Checked checkbox
            "status: complete",
            "status: done",
            "(complete)",
            "(done)",
        ]

        has_completed = any(m in content_lower for m in completed_markers)
        return has_completed

    def get_current_phase(self) -> Optional[str]:
        """Extract the current phase from the plan.

        Looks for the first phase that is not marked complete.

        Returns:
            Phase name/description or None if no phases found
        """
        content = self.read()
        if not content:
            return None

        lines = content.split("\n")

        # Look for phase headers
        phase_header_pattern = re.compile(r"^##?\s*(phase\s*\d+[^#]*)", re.IGNORECASE)
        current_phase = None

        for i, line in enumerate(lines):
            match = phase_header_pattern.match(line.strip())
            if match:
                phase_name = match.group(1).strip()

                # Check if this phase is marked complete
                # Look at the header line and a few lines after
                section_text = "\n".join(lines[i : i + 5]).lower()

                if "(complete)" in section_text or "(done)" in section_text:
                    continue  # This phase is done, keep looking

                if "status: complete" in section_text or "status: done" in section_text:
                    continue  # This phase is done, keep looking

                # Found an incomplete phase
                current_phase = phase_name
                break

        return current_phase

    def mark_phase_complete(self, phase_identifier: str) -> bool:
        """Mark a phase as complete in the plan.

        Adds a (COMPLETE) marker to the phase header.

        Args:
            phase_identifier: Phase name or number (e.g., "Phase 1" or "1")

        Returns:
            True if phase was found and marked, False otherwise
        """
        content = self.read()
        if not content:
            return False

        # Build pattern to find the phase header
        if phase_identifier.isdigit():
            pattern = rf"(^##?\s*phase\s*{phase_identifier}[^#\n]*)"
        else:
            escaped = re.escape(phase_identifier)
            pattern = rf"(^##?\s*{escaped}[^#\n]*)"

        # Find and update the phase header
        def add_complete_marker(match):
            header = match.group(1)
            if "(complete)" in header.lower() or "(done)" in header.lower():
                return header  # Already marked
            return f"{header.rstrip()} (COMPLETE)"

        new_content, count = re.subn(
            pattern, add_complete_marker, content, count=1, flags=re.IGNORECASE | re.MULTILINE
        )

        if count > 0:
            self.write(new_content)
            logger.info(f"Marked phase complete: {phase_identifier}")
            return True

        logger.warning(f"Phase not found: {phase_identifier}")
        return False
