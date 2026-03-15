"""MemoryObserver — Memory Light Phase 3: LLM-based memory extraction.

Async background process that reads recent conversation turns and extracts
"noteworthy" memories using a dedicated LLM call. Fills gaps that free sources
(todo notes, compaction summaries, phase archives, tool errors) miss.

Trigger points:
1. Every N turns (configurable via observer_interval, default: 5)
2. On phase boundaries (when archive_phase fires)

The observer deduplicates against existing memories using RecallStore.find_similar()
before storing, so repeated observations boost access_count rather than creating
duplicate entries.

See docs/features/memory_light.md Phase 3 for full design.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)


# ===========================================================================
# Extraction prompt
# ===========================================================================

EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a segment of agent conversation, \
extract noteworthy information that would be useful in future turns.

For each memory, output:
- content: The insight (1-3 sentences, self-contained, no references to "the conversation")
- summary: One-line summary (under 100 chars)
- keywords: Relevant terms for search (3-8 keywords)
- importance: How useful is this for future work? (0.0-1.0)
- type: One of: factual, procedural, error_solution, vocabulary, relational

Type definitions:
- factual: A discovered fact about the codebase, data, or domain
- procedural: A process, pattern, or sequence that works (or doesn't)
- error_solution: A problem encountered and how it was resolved
- vocabulary: Domain-specific terminology or naming conventions
- relational: How things connect (A depends on B, X requires Y)

Focus on:
- Decisions made and why
- Facts discovered about the codebase/data/domain
- Mistakes made and corrections applied
- Patterns that worked or failed
- Constraints and requirements discovered

Do NOT extract:
- Routine tool calls with no insight
- Repetitive information already captured
- Raw data or file contents (summarize instead)

Output as a JSON array. If nothing noteworthy, output an empty array [].
Example:
[
  {
    "content": "The users table uses soft deletes via a deleted_at timestamp column rather than hard deletes.",
    "summary": "Users table has soft deletes via deleted_at",
    "keywords": ["users", "soft_delete", "deleted_at", "database"],
    "importance": 0.8,
    "type": "factual"
  }
]
"""

# Valid memory types for validation
_VALID_TYPES = frozenset({
    "factual", "procedural", "error_solution", "vocabulary", "relational",
})

# Max messages to include in a single observation window
_MAX_OBSERVATION_WINDOW = 40


class MemoryObserver:
    """Async background memory extractor.

    Reads recent conversation messages and uses an LLM to extract
    noteworthy memories that free sources miss.

    Args:
        recall_store: RecallStore instance for storage and dedup
        llm: LLM instance for extraction (can be a separate model)
        observer_interval: Run observer every N turns (default: 5)
    """

    def __init__(
        self,
        recall_store,
        llm,
        observer_interval: int = 5,
        timeout: float = 120.0,
    ):
        self.recall_store = recall_store
        self.llm = llm
        self.observer_interval = observer_interval
        self._timeout = timeout
        self._last_observed_turn: int = 0

    def should_observe(self, current_turn: int) -> bool:
        """Check if the observer should run on this turn.

        Returns True every observer_interval turns, avoiding re-observation
        of the same turn range.

        Args:
            current_turn: Current turn count from state

        Returns:
            True if observer should run
        """
        if current_turn <= 0:
            return False
        if current_turn <= self._last_observed_turn:
            return False
        return current_turn % self.observer_interval == 0

    async def observe(
        self,
        messages: List[BaseMessage],
        current_turn: int,
        phase: int = 0,
    ) -> List[Dict[str, Any]]:
        """Extract and store memories from recent conversation.

        Reads recent messages, sends them to the extraction LLM,
        validates and stores the results via RecallStore (which handles
        dedup internally).

        Args:
            messages: Full conversation message list
            current_turn: Current turn count
            phase: Current phase number

        Returns:
            List of extracted memory dicts (for testing/logging)
        """
        try:
            # Determine observation window
            window_start = max(0, self._last_observed_turn)
            self._last_observed_turn = current_turn

            # Get recent messages (within window)
            segment = self._get_message_segment(messages, window_start, current_turn)
            if not segment:
                logger.debug(f"Observer: no messages in window [{window_start}:{current_turn}]")
                return []

            # Extract memories via LLM
            extracted = await self.extract_memories(segment)
            if not extracted:
                logger.debug("Observer: no memories extracted")
                return []

            # Store each extracted memory
            stored_count = 0
            for mem in extracted:
                try:
                    mem_id = await self.recall_store.store(
                        content=mem["content"],
                        summary=mem.get("summary"),
                        keywords=mem.get("keywords", []),
                        importance=mem.get("importance", 0.5),
                        memory_type=mem.get("type", "factual"),
                        source="observer",
                        source_turn_start=window_start,
                        source_turn_end=current_turn,
                        source_phase=phase,
                    )
                    if mem_id:
                        stored_count += 1
                except Exception as e:
                    logger.warning(f"Observer: failed to store memory: {e}")

            logger.info(
                f"Observer: extracted {len(extracted)} memories, "
                f"stored {stored_count} (turns {window_start}-{current_turn}, phase {phase})"
            )
            return extracted

        except Exception as e:
            logger.warning(f"Observer: observation failed (non-fatal): {e}")
            return []

    async def observe_phase_boundary(
        self,
        messages: List[BaseMessage],
        phase: int = 0,
    ) -> List[Dict[str, Any]]:
        """Trigger observer at a phase boundary.

        Unlike regular observation (which uses turn intervals), this
        processes the full recent conversation as a rich extraction target.

        Args:
            messages: Full conversation message list
            phase: Current phase number

        Returns:
            List of extracted memory dicts
        """
        try:
            # Use the last N messages as the segment
            segment = messages[-_MAX_OBSERVATION_WINDOW:] if len(messages) > _MAX_OBSERVATION_WINDOW else messages
            if not segment:
                return []

            extracted = await self.extract_memories(segment)
            if not extracted:
                return []

            stored_count = 0
            for mem in extracted:
                try:
                    mem_id = await self.recall_store.store(
                        content=mem["content"],
                        summary=mem.get("summary"),
                        keywords=mem.get("keywords", []),
                        importance=mem.get("importance", 0.5),
                        memory_type=mem.get("type", "factual"),
                        source="observer",
                        source_phase=phase,
                    )
                    if mem_id:
                        stored_count += 1
                except Exception as e:
                    logger.warning(f"Observer (phase boundary): failed to store memory: {e}")

            logger.info(
                f"Observer (phase boundary): extracted {len(extracted)}, "
                f"stored {stored_count} (phase {phase})"
            )
            return extracted

        except Exception as e:
            logger.warning(f"Observer (phase boundary): failed (non-fatal): {e}")
            return []

    async def extract_memories(
        self,
        messages: List[BaseMessage],
    ) -> List[Dict[str, Any]]:
        """Send message segment to extraction LLM and parse results.

        Args:
            messages: Conversation segment to analyze

        Returns:
            List of validated memory dicts with keys:
            content, summary, keywords, importance, type
        """
        # Format messages into a readable conversation text
        conversation_text = self._format_messages_for_extraction(messages)
        if not conversation_text.strip():
            return []

        # Call LLM with extraction prompt
        try:
            llm_messages = [
                HumanMessage(content=f"{EXTRACTION_PROMPT}\n\n--- Conversation Segment ---\n{conversation_text}\n--- End Segment ---")
            ]
            response = await asyncio.wait_for(
                self.llm.ainvoke(llm_messages),
                timeout=self._timeout,
            )
            raw_content = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            logger.warning(f"Observer: LLM extraction call failed: {e}")
            return []

        # Parse JSON response
        return self._parse_extraction_response(raw_content)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _get_message_segment(
        self,
        messages: List[BaseMessage],
        window_start: int,
        window_end: int,
    ) -> List[BaseMessage]:
        """Get messages within the observation window.

        Uses message index as a proxy for turn count (the last N messages
        corresponding to the turns since last observation).

        Args:
            messages: Full message list
            window_start: Start turn (inclusive)
            window_end: End turn (exclusive)

        Returns:
            Subset of messages within window, capped at _MAX_OBSERVATION_WINDOW
        """
        # Use turn-based slicing: take messages from the end
        # corresponding to the window size
        window_size = window_end - window_start
        if window_size <= 0:
            return []

        # Take the last window_size*2 messages (rough heuristic: ~2 messages per turn)
        slice_count = min(window_size * 2, _MAX_OBSERVATION_WINDOW)
        return messages[-slice_count:] if len(messages) > slice_count else messages

    @staticmethod
    def _format_messages_for_extraction(messages: List[BaseMessage]) -> str:
        """Format messages into readable text for the extraction LLM.

        Filters out injection messages (workspace, memory, instruction)
        to focus on actual conversation content.

        Args:
            messages: Messages to format

        Returns:
            Formatted conversation text
        """
        from src.core.workspace_injection import is_workspace_injection_message

        lines = []
        for msg in messages:
            # Skip injection messages (workspace, memory, instructions)
            if is_workspace_injection_message(msg):
                continue

            role = _get_message_role(msg)
            content = msg.content if hasattr(msg, "content") else ""

            # Skip empty messages
            if not content or not content.strip():
                continue

            # Truncate very long tool results
            if isinstance(msg, ToolMessage) and len(content) > 1000:
                content = content[:1000] + "... [truncated]"

            lines.append(f"[{role}] {content}")

        return "\n\n".join(lines)

    @staticmethod
    def _parse_extraction_response(raw: str) -> List[Dict[str, Any]]:
        """Parse the LLM's extraction response into validated memory dicts.

        Handles common LLM output quirks: markdown code fences, extra text
        around JSON, etc.

        Args:
            raw: Raw LLM response text

        Returns:
            List of validated memory dicts
        """
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            # Remove opening fence (with optional language tag)
            first_newline = text.index("\n") if "\n" in text else len(text)
            text = text[first_newline + 1:]
            # Remove closing fence
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].rstrip()

        # Try to find JSON array in the text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON array from surrounding text
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    logger.warning("Observer: failed to parse extraction response as JSON")
                    return []
            else:
                logger.warning("Observer: no JSON array found in extraction response")
                return []

        if not isinstance(data, list):
            logger.warning(f"Observer: expected JSON array, got {type(data).__name__}")
            return []

        # Validate each entry
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if "content" not in item or not item["content"]:
                continue

            # Clamp importance to valid range
            importance = item.get("importance", 0.5)
            try:
                importance = float(importance)
                importance = max(0.0, min(1.0, importance))
            except (TypeError, ValueError):
                importance = 0.5

            # Validate type
            mem_type = item.get("type", "factual")
            if mem_type not in _VALID_TYPES:
                mem_type = "factual"

            # Ensure keywords is a list of strings
            keywords = item.get("keywords", [])
            if not isinstance(keywords, list):
                keywords = []
            keywords = [str(k).lower() for k in keywords if k][:8]

            validated.append({
                "content": str(item["content"]).strip(),
                "summary": str(item.get("summary", ""))[:100] if item.get("summary") else None,
                "keywords": keywords,
                "importance": importance,
                "type": mem_type,
            })

        return validated


def _get_message_role(msg: BaseMessage) -> str:
    """Get a human-readable role label for a message."""
    if isinstance(msg, HumanMessage):
        return "User"
    elif isinstance(msg, AIMessage):
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_names = ", ".join(tc.get("name", "?") for tc in msg.tool_calls)
            return f"Agent (calls: {tool_names})"
        return "Agent"
    elif isinstance(msg, ToolMessage):
        return "Tool Result"
    return "System"
