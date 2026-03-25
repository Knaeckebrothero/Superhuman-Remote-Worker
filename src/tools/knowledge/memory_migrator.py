"""Memory Migrator — Migrate Memory Light memories to knowledge notes.

Standalone migration tool that reads Memory Light memories from the
PostgreSQL ``memories`` table, maps them to knowledge note types,
deduplicates against existing KB content, and writes through to
Neo4j + pgvector via the standard write-through pattern.

Part of the project knowledge base Phase 3 migration toolkit.

Usage:
    ```python
    from src.tools.knowledge.memory_migrator import migrate_memories

    stats = await migrate_memories(
        project_id="proj-456",
        db=postgres_db,
        knowledge_graph=kg,
        knowledge_store=ks,
        embedding_service=embedding_svc,
        min_importance=0.3,
        dry_run=False,
    )
    # stats = {"migrated": 12, "skipped": 3, "duplicates": 2, "errors": 1}
    ```
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from ...services.embedding_service import EmbeddingService
    from ...services.knowledge_graph import KnowledgeGraphDB
    from ...services.knowledge_store import KnowledgeStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Memory-type to note-type mapping
# ---------------------------------------------------------------------------

_MEMORY_TYPE_MAP: Dict[str, str] = {
    "observer": "learning",
    "compaction_summary": "state",
    "phase_archive": "retrospective",
    "tool_error": "learning",
    "free": "learning",
    # "todo_completion" is handled dynamically — see _map_todo_completion()
}

_CODE_KEYWORDS = re.compile(
    r"\b(code|implementation|module|class|function|method|refactor|bug|patch)\b",
    re.IGNORECASE,
)


def _map_todo_completion(content: str) -> str:
    """Classify a todo_completion memory as 'code' or 'learning'.

    If the content mentions code/implementation-related terms, treat it
    as a code note; otherwise default to learning.

    Args:
        content: Memory content text.

    Returns:
        Note type string.
    """
    if _CODE_KEYWORDS.search(content):
        return "code"
    return "learning"


def _map_memory_type(memory_type: str, content: str) -> str:
    """Map a memory_type string to a knowledge note type.

    Args:
        memory_type: Value from the memories.memory_type column.
        content: Memory content text (used for dynamic classification).

    Returns:
        Knowledge note type string.
    """
    if memory_type == "todo_completion":
        return _map_todo_completion(content)
    return _MEMORY_TYPE_MAP.get(memory_type, "learning")


# ---------------------------------------------------------------------------
# Tag generation
# ---------------------------------------------------------------------------


def _generate_tags(memory: Dict[str, Any]) -> List[str]:
    """Generate tags from a memory row.

    Pulls from the memory's keywords field (if present) and adds
    provenance tags.

    Args:
        memory: Row dict from the memories table.

    Returns:
        List of tag strings.
    """
    tags: List[str] = []

    # Use existing keywords if available
    keywords = memory.get("keywords") or memory.get("tags") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    for kw in keywords:
        tag = kw.lower().strip()
        if tag and tag not in tags:
            tags.append(tag)

    # Add provenance tags
    tags.append("migrated-from-memory")

    memory_type = memory.get("memory_type", "")
    if memory_type == "tool_error":
        if "error-solution" not in tags:
            tags.append("error-solution")

    # Add channel tag for traceability
    if memory_type and f"memory-{memory_type}" not in tags:
        tags.append(f"memory-{memory_type}")

    return tags


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------


def _slugify(text: str, max_length: int = 80) -> str:
    """Generate a URL-safe slug from text.

    Args:
        text: Raw text (title or content snippet).
        max_length: Maximum slug length.

    Returns:
        Slug string.
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length]


def _generate_title(memory: Dict[str, Any]) -> str:
    """Generate a concise title from memory content.

    Uses the first line of content (up to 80 chars) as the title.

    Args:
        memory: Row dict from the memories table.

    Returns:
        Title string.
    """
    content = memory.get("content", "") or ""
    # Take first non-empty line
    for line in content.split("\n"):
        line = line.strip()
        if line:
            # Strip markdown heading markers
            line = re.sub(r"^#+\s*", "", line)
            if len(line) > 80:
                return line[:77] + "..."
            return line

    # Fallback: use memory type + truncated ID
    memory_type = memory.get("memory_type", "memory")
    memory_id = str(memory.get("id", ""))[:8]
    return f"{memory_type} {memory_id}"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

# Similarity threshold for considering a memory a duplicate of an existing note
_DUPLICATE_THRESHOLD = 0.92


async def _is_duplicate(
    content: str,
    project_id: str,
    knowledge_store: "KnowledgeStore",
) -> bool:
    """Check if content is semantically duplicated in the KB.

    Performs a hybrid search and checks if the top result exceeds the
    similarity threshold.

    Args:
        content: Memory content to check.
        project_id: Project UUID string.
        knowledge_store: KnowledgeStore instance.

    Returns:
        True if a near-duplicate exists.
    """
    try:
        # Use a short snippet for the search query to avoid noise
        query = content[:500] if len(content) > 500 else content
        results = await knowledge_store.hybrid_search(
            project_id=uuid.UUID(project_id),
            query=query,
            match_count=1,
        )

        if not results:
            return False

        top = results[0]
        # Simple content overlap heuristic: if >92% of words match, treat as duplicate
        query_words = set(content.lower().split())
        result_words = set(top.content.lower().split())
        if not query_words:
            return False

        overlap = len(query_words & result_words) / max(len(query_words), 1)
        return overlap >= _DUPLICATE_THRESHOLD

    except Exception as e:
        logger.debug("Duplicate check failed (non-fatal): %s", e)
        return False


# ---------------------------------------------------------------------------
# SQL query for reading memories
# ---------------------------------------------------------------------------

_FETCH_MEMORIES_SQL = """
SELECT m.* FROM memories m
JOIN jobs j ON m.job_id = j.id
WHERE j.project_id = $1
AND m.importance >= $2
ORDER BY m.created_at
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def migrate_memories(
    project_id: str,
    db,  # PostgresDB or asyncpg connection
    knowledge_graph: "KnowledgeGraphDB",
    knowledge_store: "KnowledgeStore",
    embedding_service: "EmbeddingService",
    min_importance: float = 0.3,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Migrate Memory Light memories to knowledge notes.

    Reads memories for all jobs belonging to a project, filters by
    importance, deduplicates against existing KB content, and writes
    through to Neo4j + pgvector.

    Args:
        project_id: Project UUID string.
        db: PostgresDB instance (async, with fetch method).
        knowledge_graph: KnowledgeGraphDB instance (connected).
        knowledge_store: KnowledgeStore instance.
        embedding_service: EmbeddingService instance (used by knowledge_store).
        min_importance: Minimum importance score (memories below this are skipped).
        dry_run: If True, parse and classify but do not write to databases.

    Returns:
        Stats dict: { migrated: int, skipped: int, duplicates: int, errors: int }
    """
    stats: Dict[str, Any] = {
        "migrated": 0,
        "skipped": 0,
        "duplicates": 0,
        "errors": 0,
    }

    # 1. Fetch memories for this project's jobs
    try:
        rows = await db.fetch(
            _FETCH_MEMORIES_SQL,
            uuid.UUID(project_id) if isinstance(project_id, str) else project_id,
            min_importance,
        )
    except Exception as e:
        logger.error("Failed to fetch memories for project %s: %s", project_id, e)
        stats["errors"] += 1
        return stats

    if not rows:
        logger.info(
            "No memories found for project %s (min_importance=%.2f)",
            project_id,
            min_importance,
        )
        return stats

    logger.info(
        "Found %d memories for project %s (min_importance=%.2f)",
        len(rows),
        project_id,
        min_importance,
    )

    now = datetime.now(timezone.utc)

    for row in rows:
        memory = dict(row)
        content = memory.get("content", "") or ""
        memory_type = memory.get("memory_type", "free")

        # Skip empty content
        if not content.strip():
            stats["skipped"] += 1
            continue

        # 2. Map to note type
        note_type = _map_memory_type(memory_type, content)

        # 3. Generate metadata
        title = _generate_title(memory)
        slug = _slugify(title)
        if not slug:
            slug = f"memory-{str(memory.get('id', ''))[:8]}"
        tags = _generate_tags(memory)

        # 4. Check for duplicates in existing KB
        try:
            if await _is_duplicate(content, project_id, knowledge_store):
                logger.debug("Skipping duplicate memory: %s", slug)
                stats["duplicates"] += 1
                continue
        except Exception as e:
            logger.debug("Duplicate check error (proceeding): %s", e)

        # 5. Dry run — just log and count
        if dry_run:
            logger.info(
                "[DRY RUN] Would migrate: %s (type=%s, tags=%s)",
                slug,
                note_type,
                tags,
            )
            stats["migrated"] += 1
            continue

        # 6. Write to Neo4j (source of truth)
        try:
            job_id_str = str(memory["job_id"]) if memory.get("job_id") else None
            importance = memory.get("importance")
            confidence = None
            if importance is not None:
                if importance >= 0.8:
                    confidence = "high"
                elif importance >= 0.5:
                    confidence = "medium"
                else:
                    confidence = "low"

            created_slug = knowledge_graph.create_note(
                project_id=project_id,
                title=title,
                note_type=note_type,
                content=content,
                tags=tags,
                confidence=confidence,
                job_id=job_id_str,
                phase=memory.get("phase"),
            )

            # 7. Write-through to pgvector search index
            try:
                job_uuid = memory.get("job_id")
                if isinstance(job_uuid, str):
                    job_uuid = uuid.UUID(job_uuid)

                await knowledge_store.upsert_note(
                    note_id=created_slug,
                    project_id=uuid.UUID(project_id)
                    if isinstance(project_id, str)
                    else project_id,
                    title=title,
                    note_type=note_type,
                    content=content,
                    tags=tags,
                    confidence=confidence,
                    job_id=job_uuid,
                    phase=memory.get("phase"),
                    created_at=memory.get("created_at", now),
                    modified_at=now,
                )
            except Exception as e:
                logger.warning(
                    "pgvector write-through failed for %s: %s (Neo4j note exists)",
                    created_slug,
                    e,
                )

            stats["migrated"] += 1

        except Exception as e:
            logger.error("Failed to migrate memory %s: %s", memory.get("id"), e)
            stats["errors"] += 1

    logger.info(
        "Memory migration complete for project %s: "
        "migrated=%d, skipped=%d, duplicates=%d, errors=%d",
        project_id,
        stats["migrated"],
        stats["skipped"],
        stats["duplicates"],
        stats["errors"],
    )
    return stats
