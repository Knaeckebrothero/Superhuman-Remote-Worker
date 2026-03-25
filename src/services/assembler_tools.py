"""Assembler tools — memory DB tools for the memory assembler agent task.

The assembler reviews recent conversation and adjusts memory TTLs:
- memory_search: hybrid search the memory DB for relevant memories
- memory_boost: increase TTL to keep a memory pinned longer
- memory_deprecate: decrease TTL to fade an irrelevant memory

These tools wrap RecallStore methods and are passed to the
AssembleMemoriesTask (AuxAgentTask) for agent-mode execution.

See docs/features/memory_light.md for architecture.
"""

import logging
import uuid
from typing import Any, List

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def create_assembler_tools(recall_store: Any) -> List[Any]:
    """Create memory assembler tools with injected RecallStore.

    Args:
        recall_store: RecallStore instance for memory operations

    Returns:
        List of LangChain tool functions
    """

    @tool
    async def memory_search(query: str) -> str:
        """Search the memory database for relevant memories.

        Uses hybrid search (dense vector + sparse keyword + recency)
        to find memories matching the query.

        Args:
            query: Natural language search query
        """
        try:
            embedding = await recall_store.embedding_service.embed(query)
            results = await recall_store.hybrid_search(
                query_text=query,
                query_embedding=embedding,
                match_count=20,
            )

            if not results:
                return "No memories found matching the query."

            lines = [f"Found {len(results)} memories:\n"]
            for i, mem in enumerate(results, 1):
                ttl_info = ""
                if mem.remaining_turns is not None and mem.remaining_turns > 0:
                    ttl_info = f", TTL: {mem.remaining_turns} turns"
                lines.append(
                    f"[{i}] ID: {mem.id}\n"
                    f"    Type: {mem.memory_type}, Importance: {mem.importance:.1f}"
                    f"{ttl_info}\n"
                    f"    Content: {mem.content[:200]}"
                    f"{'...' if len(mem.content) > 200 else ''}\n"
                )
            return "\n".join(lines)

        except Exception as e:
            return f"Error searching memories: {e}"

    @tool
    async def memory_boost(memory_id: str, turns: int) -> str:
        """Increase a memory's TTL to keep it pinned in injection longer.

        Use this when a memory is highly relevant to the current work
        and should be guaranteed to appear in the agent's context.

        Args:
            memory_id: UUID of the memory to boost
            turns: Number of turns to add to the TTL
        """
        try:
            mem_uuid = uuid.UUID(memory_id)
            success = await recall_store.boost_ttl(mem_uuid, turns)
            if success:
                return f"Boosted memory {memory_id} TTL by {turns} turns."
            return f"Memory {memory_id} not found."
        except ValueError:
            return f"Invalid UUID: {memory_id}"
        except Exception as e:
            return f"Error boosting memory: {e}"

    @tool
    async def memory_deprecate(memory_id: str, turns: int) -> str:
        """Decrease a memory's TTL to let it fade from injection sooner.

        Use this when a pinned memory is no longer relevant to the
        current work and is consuming injection budget unnecessarily.

        Args:
            memory_id: UUID of the memory to deprecate
            turns: Number of turns to subtract from the TTL
        """
        try:
            mem_uuid = uuid.UUID(memory_id)
            success = await recall_store.deprecate_ttl(mem_uuid, turns)
            if success:
                return f"Deprecated memory {memory_id} TTL by {turns} turns."
            return f"Memory {memory_id} not found."
        except ValueError:
            return f"Invalid UUID: {memory_id}"
        except Exception as e:
            return f"Error deprecating memory: {e}"

    @tool
    async def memory_add_triggers(memory_id: str, messages: list[str]) -> str:
        """Add retrieval trigger phrases to an existing memory.

        Trigger phrases are synthetic queries that represent situations where
        the memory should be retrieved. They are embedded and used during
        hybrid search to improve recall.

        Args:
            memory_id: UUID of the memory to add triggers to
            messages: List of 3-5 trigger phrases
        """
        try:
            mem_uuid = uuid.UUID(memory_id)
            if not messages or len(messages) == 0:
                return "No trigger phrases provided."
            # Cap at 7 to avoid excessive embedding costs
            phrases = messages[:7]
            count = await recall_store.store_retrieval_messages(mem_uuid, phrases)
            return f"Added {count} retrieval trigger phrases to memory {memory_id}."
        except ValueError:
            return f"Invalid UUID: {memory_id}"
        except Exception as e:
            return f"Error adding trigger phrases: {e}"

    return [memory_search, memory_boost, memory_deprecate, memory_add_triggers]
