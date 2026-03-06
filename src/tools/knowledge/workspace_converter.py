"""Workspace Converter — Migrate workspace.md into knowledge notes.

Standalone migration tool that parses a workspace.md file, splits it
into sections by heading, classifies each section by content heuristics,
and produces knowledge note dicts ready for KnowledgeGraphDB.create_note()
or the kb_write tool.

Part of the project knowledge base Phase 3 migration toolkit.

Usage:
    ```python
    from src.tools.knowledge.workspace_converter import convert_workspace, convert_and_write

    # Parse only (no DB writes)
    notes = convert_workspace(content=workspace_text, job_id="abc-123")

    # Parse and write to knowledge base
    count = await convert_and_write(
        content=workspace_text,
        project_id="proj-456",
        knowledge_graph=kg,
        knowledge_store=ks,
        job_id="abc-123",
    )
    ```
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ...services.knowledge_graph import KnowledgeGraphDB
    from ...services.knowledge_store import KnowledgeStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section type classification rules
# ---------------------------------------------------------------------------

_TYPE_RULES: List[tuple] = [
    ("decision", re.compile(r"\b(decision|chose|picked|selected)\b", re.IGNORECASE)),
    ("learning", re.compile(r"\b(learned|discovered|found|issue|error|fix)\b", re.IGNORECASE)),
    ("goal", re.compile(r"\b(goal|objective|milestone)\b", re.IGNORECASE)),
    ("plan", re.compile(r"\b(plan|roadmap|phase)\b", re.IGNORECASE)),
    ("question", re.compile(r"\b(question|investigate|TODO|open)\b", re.IGNORECASE)),
    ("state", re.compile(r"\b(status|progress|current)\b", re.IGNORECASE)),
    ("code", re.compile(r"\b(code|implementation|module|class|function)\b", re.IGNORECASE)),
]

_DEFAULT_TYPE = "learning"

# ---------------------------------------------------------------------------
# Keyword extraction helpers
# ---------------------------------------------------------------------------

# Common stop-words to exclude from tag extraction
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "about", "between",
    "through", "during", "before", "after", "above", "below", "and", "but",
    "or", "nor", "not", "so", "if", "then", "than", "that", "this",
    "these", "those", "it", "its", "we", "our", "they", "their", "them",
    "you", "your", "he", "she", "his", "her", "i", "me", "my",
})


def _slugify(title: str, max_length: int = 80) -> str:
    """Generate a URL-safe slug from a title.

    Lowercase, hyphens for spaces, strip special characters, truncate.

    Args:
        title: Raw heading text.
        max_length: Maximum slug length.

    Returns:
        Slug string (e.g. "chose-jwt-over-oauth").
    """
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length]


def _classify_section(content: str) -> str:
    """Classify a section's content into a note type using keyword heuristics.

    Tries rules in priority order; first match wins.

    Args:
        content: Section body text.

    Returns:
        Note type string.
    """
    for note_type, pattern in _TYPE_RULES:
        if pattern.search(content):
            return note_type
    return _DEFAULT_TYPE


def _extract_tags(content: str, max_tags: int = 10) -> List[str]:
    """Extract simple keyword-based tags from content.

    Pulls capitalized multi-word phrases and standalone significant words.

    Args:
        content: Section body text.
        max_tags: Maximum number of tags to return.

    Returns:
        List of lowercase tag strings.
    """
    tags: List[str] = []

    # Extract inline code identifiers (e.g., `SomeClass`, `my_function`)
    code_refs = re.findall(r"`([A-Za-z_]\w+)`", content)
    for ref in code_refs:
        tag = ref.lower().replace("_", "-")
        if tag not in tags and len(tag) > 2:
            tags.append(tag)

    # Extract capitalized phrases that look like proper nouns / terms
    capitalized = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", content)
    for phrase in capitalized:
        tag = phrase.lower().replace(" ", "-")
        if tag not in tags and tag not in _STOP_WORDS and len(tag) > 2:
            tags.append(tag)

    # Extract words after common marker phrases
    marker_words = re.findall(
        r"(?:using|via|with|chose|selected|implemented)\s+(\w+)",
        content,
        re.IGNORECASE,
    )
    for word in marker_words:
        tag = word.lower()
        if tag not in tags and tag not in _STOP_WORDS and len(tag) > 2:
            tags.append(tag)

    return tags[:max_tags]


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)


def _parse_sections(content: str) -> List[Dict[str, str]]:
    """Split workspace.md content into sections by ## and ### headings.

    Args:
        content: Full workspace.md text.

    Returns:
        List of dicts with ``title``, ``level``, and ``body`` keys.
    """
    matches = list(_HEADING_RE.finditer(content))

    if not matches:
        # No headings — treat entire content as a single section
        stripped = content.strip()
        if stripped:
            return [{"title": "Workspace Notes", "level": "2", "body": stripped}]
        return []

    sections: List[Dict[str, str]] = []

    for idx, match in enumerate(matches):
        level = str(len(match.group(1)))
        title = match.group(2).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        if body:
            sections.append({"title": title, "level": level, "body": body})

    return sections


# ---------------------------------------------------------------------------
# Cross-reference detection
# ---------------------------------------------------------------------------


def _infer_links(
    sections: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, str]]]:
    """Detect cross-references between sections.

    If one section's body mentions another section's heading text, we
    record a REFERENCES link from the mentioner to the mentioned.

    Args:
        sections: List of section dicts (must already have ``slug`` key).

    Returns:
        Mapping of source slug -> list of link dicts.
    """
    links: Dict[str, List[Dict[str, str]]] = {}

    for src in sections:
        src_slug = src["slug"]
        src_body_lower = src["body"].lower()
        for tgt in sections:
            if tgt["slug"] == src_slug:
                continue
            # Check if the target's title appears in the source's body
            tgt_title_lower = tgt["title"].lower()
            if len(tgt_title_lower) >= 4 and tgt_title_lower in src_body_lower:
                links.setdefault(src_slug, []).append({
                    "target": tgt["slug"],
                    "type": "REFERENCES",
                })

    return links


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_workspace(
    content: str,
    job_id: Optional[str] = None,
    config_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert workspace.md content into knowledge note dicts.

    Returns list of dicts ready to pass to KnowledgeGraphDB.create_note() or kb_write.
    Each dict has: title, note_type, content, tags, slug, links

    Args:
        content: Raw workspace.md text.
        job_id: Optional job UUID for provenance tagging.
        config_name: Optional agent config name for tagging.

    Returns:
        List of note dicts.
    """
    if not content or not content.strip():
        return []

    # 1. Parse into sections
    raw_sections = _parse_sections(content)
    if not raw_sections:
        return []

    # 2. Enrich each section with classification, slug, tags
    sections: List[Dict[str, Any]] = []
    seen_slugs: set = set()

    for sec in raw_sections:
        title = sec["title"]
        body = sec["body"]
        slug = _slugify(title)

        # Handle slug collisions by appending a counter
        if not slug:
            slug = "untitled"
        base_slug = slug
        counter = 2
        while slug in seen_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        seen_slugs.add(slug)

        note_type = _classify_section(title + " " + body)
        tags = _extract_tags(body)

        # Add provenance tags
        if config_name:
            tags.append(f"agent-{config_name}")
        if job_id:
            tags.append("migrated-from-workspace")

        sections.append({
            "title": title,
            "note_type": note_type,
            "content": body,
            "tags": tags,
            "slug": slug,
            "body": body,  # kept for link inference
            "links": [],
        })

    # 3. Infer cross-references
    link_map = _infer_links(sections)

    # 4. Build final output (drop internal-only keys)
    notes: List[Dict[str, Any]] = []
    for sec in sections:
        note: Dict[str, Any] = {
            "title": sec["title"],
            "note_type": sec["note_type"],
            "content": sec["content"],
            "tags": sec["tags"],
            "slug": sec["slug"],
            "links": link_map.get(sec["slug"], []),
        }
        notes.append(note)

    logger.info(
        "Converted workspace.md into %d knowledge note(s) (job_id=%s)",
        len(notes),
        job_id,
    )
    return notes


async def convert_and_write(
    content: str,
    project_id: str,
    knowledge_graph: "KnowledgeGraphDB",
    knowledge_store: "KnowledgeStore",
    job_id: Optional[str] = None,
    config_name: Optional[str] = None,
) -> int:
    """Convert workspace.md and write notes to KB. Returns count of notes written.

    Performs the full pipeline: parse -> classify -> write-through to
    Neo4j (source of truth) + pgvector (search index).

    Args:
        content: Raw workspace.md text.
        project_id: Project UUID string.
        knowledge_graph: KnowledgeGraphDB instance (connected).
        knowledge_store: KnowledgeStore instance.
        job_id: Optional originating job UUID.
        config_name: Optional agent config name.

    Returns:
        Number of notes successfully written.
    """
    notes = convert_workspace(content, job_id=job_id, config_name=config_name)
    if not notes:
        logger.info("No sections found in workspace.md — nothing to write.")
        return 0

    written = 0
    now = datetime.now(timezone.utc)

    for note in notes:
        try:
            # 1. Write to Neo4j (source of truth)
            slug = knowledge_graph.create_note(
                project_id=project_id,
                title=note["title"],
                note_type=note["note_type"],
                content=note["content"],
                tags=note["tags"],
                confidence=None,
                job_id=job_id,
                phase=None,
                links=note["links"] if note["links"] else None,
            )

            # 2. Write-through to pgvector search index
            try:
                await knowledge_store.upsert_note(
                    note_id=slug,
                    project_id=uuid.UUID(project_id),
                    title=note["title"],
                    note_type=note["note_type"],
                    content=note["content"],
                    tags=note["tags"],
                    job_id=uuid.UUID(job_id) if job_id else None,
                    created_at=now,
                    modified_at=now,
                )
            except Exception as e:
                logger.warning(
                    "pgvector write-through failed for %s: %s (Neo4j note exists)",
                    slug,
                    e,
                )

            written += 1

        except Exception as e:
            logger.error(
                "Failed to write note '%s': %s",
                note.get("title", "?"),
                e,
            )

    logger.info(
        "Wrote %d/%d notes from workspace.md to project %s",
        written,
        len(notes),
        project_id,
    )
    return written
