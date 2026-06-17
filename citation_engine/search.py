"""
Unified Search for Citation Engine
====================================
Implements keyword search (FTS), semantic search (pgvector), hybrid retrieval
(RRF fusion), and explainable evidence labeling.

All search is scoped to a job via the job_sources join table.
"""

import logging
from dataclasses import dataclass

from .models import SearchResult

log = logging.getLogger(__name__)

# RRF constant — standard value from the original RRF paper
RRF_K = 60

# Evidence label thresholds (on normalized RRF scores)
HIGH_THRESHOLD = 0.6
MEDIUM_THRESHOLD = 0.3


@dataclass
class _RankedHit:
    """Internal intermediate result before evidence labeling."""

    source_id: int
    source_name: str
    source_type: str
    chunk_text: str
    page_reference: str | None
    rrf_score: float
    in_keyword: bool
    in_semantic: bool


def keyword_search(
    cursor,
    db_type: str,
    query: str,
    job_id: str,
    top_k: int = 20,
    source_type: str | None = None,
    tags: list[str] | None = None,
    scope: str = "content",
) -> list[_RankedHit]:
    """
    Full-text search using PostgreSQL tsvector or SQLite LIKE fallback.

    Args:
        cursor: Database cursor
        db_type: "sqlite" or "postgresql"
        query: Search query text
        job_id: Job ID for scoping
        top_k: Maximum results
        source_type: Optional source type filter
        tags: Optional tag filter (AND logic)
        scope: "content", "annotations", or "all"

    Returns:
        List of ranked hits
    """
    results = []

    if scope in ("content", "all"):
        results.extend(
            _keyword_search_content(
                cursor, db_type, query, job_id, top_k, source_type, tags
            )
        )

    if scope in ("annotations", "all"):
        results.extend(
            _keyword_search_annotations(
                cursor, db_type, query, job_id, top_k, source_type, tags
            )
        )

    # Deduplicate by (source_id, chunk_text) keeping highest score
    seen = {}
    for hit in results:
        key = (hit.source_id, hit.chunk_text[:200])
        if key not in seen or hit.rrf_score > seen[key].rrf_score:
            seen[key] = hit

    deduped = sorted(seen.values(), key=lambda h: h.rrf_score, reverse=True)
    return deduped[:top_k]


def _keyword_search_content(
    cursor,
    db_type: str,
    query: str,
    job_id: str,
    top_k: int,
    source_type: str | None,
    tags: list[str] | None,
) -> list[_RankedHit]:
    """Search source content using FTS."""
    if db_type == "postgresql":
        return _pg_fts_content(cursor, query, job_id, top_k, source_type, tags)
    else:
        return _sqlite_like_content(cursor, query, job_id, top_k, source_type, tags)


def _pg_fts_content(
    cursor,
    query: str,
    job_id: str,
    top_k: int,
    source_type: str | None,
    tags: list[str] | None,
) -> list[_RankedHit]:
    """PostgreSQL full-text search on source content."""
    params: list = [job_id, query]
    sql = """
        SELECT s.id AS source_id, s.name AS source_name, s.type AS source_type,
               substring(s.content, 1, 1000) AS chunk_text,
               ts_rank(to_tsvector('simple', s.content), plainto_tsquery('simple', %s)) AS rank
        FROM sources s
        JOIN job_sources js ON s.id = js.source_id AND js.job_id = %s
        WHERE to_tsvector('simple', s.content) @@ plainto_tsquery('simple', %s)
    """
    # Reorder: job_id first in JOIN, query params for ts_rank and WHERE
    sql = """
        SELECT s.id AS source_id, s.name AS source_name, s.type::text AS source_type,
               substring(s.content, 1, 1000) AS chunk_text,
               ts_rank(to_tsvector('simple', s.content), plainto_tsquery('simple', %s)) AS rank
        FROM sources s
        JOIN job_sources js ON s.id = js.source_id AND js.job_id = %s
        WHERE to_tsvector('simple', s.content) @@ plainto_tsquery('simple', %s)
    """
    params = [query, job_id, query]

    if source_type:
        sql += " AND s.type = %s::source_type"
        params.append(source_type)

    if tags:
        for i, tag in enumerate(tags):
            alias = f"st{i}"
            sql += f" AND EXISTS (SELECT 1 FROM source_tags {alias} WHERE {alias}.source_id = s.id AND {alias}.job_id = %s AND {alias}.tag = %s)"
            params.extend([job_id, tag])

    sql += " ORDER BY rank DESC LIMIT %s"
    params.append(top_k)

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()

    hits = []
    for row in _rows_to_dicts(rows, cursor):
        hits.append(
            _RankedHit(
                source_id=row["source_id"],
                source_name=row["source_name"],
                source_type=row["source_type"],
                chunk_text=row["chunk_text"],
                page_reference=None,
                rrf_score=float(row["rank"]),
                in_keyword=True,
                in_semantic=False,
            )
        )
    return hits


def _sqlite_like_content(
    cursor,
    query: str,
    job_id: str,
    top_k: int,
    source_type: str | None,
    tags: list[str] | None,
) -> list[_RankedHit]:
    """SQLite fallback: case-insensitive LIKE search on source content."""
    params: list = [job_id, f"%{query}%"]
    sql = """
        SELECT s.id AS source_id, s.name AS source_name, s.type AS source_type,
               substr(s.content, 1, 1000) AS chunk_text
        FROM sources s
        JOIN job_sources js ON s.id = js.source_id AND js.job_id = ?
        WHERE s.content LIKE ?
    """

    if source_type:
        sql += " AND s.type = ?"
        params.append(source_type)

    if tags:
        for i, tag in enumerate(tags):
            alias = f"st{i}"
            sql += f" AND EXISTS (SELECT 1 FROM source_tags {alias} WHERE {alias}.source_id = s.id AND {alias}.job_id = ? AND {alias}.tag = ?)"
            params.extend([job_id, tag])

    sql += " LIMIT ?"
    params.append(top_k)

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()

    hits = []
    for row in _rows_to_dicts(rows, cursor):
        hits.append(
            _RankedHit(
                source_id=row["source_id"],
                source_name=row["source_name"],
                source_type=row["source_type"],
                chunk_text=row["chunk_text"],
                page_reference=None,
                rrf_score=1.0,  # No ranking in SQLite LIKE
                in_keyword=True,
                in_semantic=False,
            )
        )
    return hits


def _keyword_search_annotations(
    cursor,
    db_type: str,
    query: str,
    job_id: str,
    top_k: int,
    source_type: str | None,
    tags: list[str] | None,
) -> list[_RankedHit]:
    """Search annotation content."""
    if db_type == "postgresql":
        params: list = [query, job_id, query]
        sql = """
            SELECT sa.source_id, s.name AS source_name, s.type::text AS source_type,
                   sa.content AS chunk_text, sa.page_reference,
                   ts_rank(to_tsvector('simple', sa.content), plainto_tsquery('simple', %s)) AS rank
            FROM source_annotations sa
            JOIN sources s ON s.id = sa.source_id
            JOIN job_sources js ON s.id = js.source_id AND js.job_id = %s
            WHERE sa.job_id = %s
              AND to_tsvector('simple', sa.content) @@ plainto_tsquery('simple', %s)
        """
        params = [query, job_id, job_id, query]

        if source_type:
            sql += " AND s.type = %s::source_type"
            params.append(source_type)

        sql += " ORDER BY rank DESC LIMIT %s"
        params.append(top_k)
    else:
        params = [job_id, job_id, f"%{query}%"]
        sql = """
            SELECT sa.source_id, s.name AS source_name, s.type AS source_type,
                   sa.content AS chunk_text, sa.page_reference
            FROM source_annotations sa
            JOIN sources s ON s.id = sa.source_id
            JOIN job_sources js ON s.id = js.source_id AND js.job_id = ?
            WHERE sa.job_id = ?
              AND sa.content LIKE ?
        """

        if source_type:
            sql += " AND s.type = ?"
            params.append(source_type)

        sql += " LIMIT ?"
        params.append(top_k)

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()

    hits = []
    for row in _rows_to_dicts(rows, cursor):
        hits.append(
            _RankedHit(
                source_id=row["source_id"],
                source_name=row["source_name"],
                source_type=row["source_type"],
                chunk_text=row["chunk_text"],
                page_reference=row.get("page_reference"),
                rrf_score=float(row.get("rank", 1.0)),
                in_keyword=True,
                in_semantic=False,
            )
        )
    return hits


def semantic_search(
    cursor,
    db_type: str,
    query_embedding: list[float],
    job_id: str,
    top_k: int = 20,
    source_type: str | None = None,
    tags: list[str] | None = None,
) -> list[_RankedHit]:
    """
    Vector similarity search using pgvector cosine distance.

    Only works on PostgreSQL with pgvector extension.

    Args:
        cursor: Database cursor
        db_type: Must be "postgresql"
        query_embedding: Query embedding vector
        job_id: Job ID for scoping
        top_k: Maximum results
        source_type: Optional source type filter
        tags: Optional tag filter (AND logic)

    Returns:
        List of ranked hits ordered by similarity
    """
    if db_type != "postgresql":
        log.debug("Semantic search requires PostgreSQL with pgvector")
        return []

    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    params: list = [embedding_str, job_id]
    sql = """
        SELECT se.source_id, s.name AS source_name, s.type::text AS source_type,
               se.chunk_text,
               1 - (se.embedding <=> %s::vector) AS similarity
        FROM source_embeddings se
        JOIN sources s ON s.id = se.source_id
        JOIN job_sources js ON s.id = js.source_id AND js.job_id = %s
        WHERE se.job_id = %s AND se.embedding IS NOT NULL
    """
    params.append(job_id)

    if source_type:
        sql += " AND s.type = %s::source_type"
        params.append(source_type)

    if tags:
        for i, tag in enumerate(tags):
            alias = f"st{i}"
            sql += f" AND EXISTS (SELECT 1 FROM source_tags {alias} WHERE {alias}.source_id = s.id AND {alias}.job_id = %s AND {alias}.tag = %s)"
            params.extend([job_id, tag])

    sql += " ORDER BY se.embedding <=> %s::vector LIMIT %s"
    params.extend([embedding_str, top_k])

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()

    hits = []
    for row in _rows_to_dicts(rows, cursor):
        hits.append(
            _RankedHit(
                source_id=row["source_id"],
                source_name=row["source_name"],
                source_type=row["source_type"],
                chunk_text=row["chunk_text"],
                page_reference=None,
                rrf_score=float(row["similarity"]),
                in_keyword=False,
                in_semantic=True,
            )
        )
    return hits


def rrf_merge(
    keyword_hits: list[_RankedHit],
    semantic_hits: list[_RankedHit],
    k: int = RRF_K,
    top_k: int = 10,
) -> list[_RankedHit]:
    """
    Merge keyword and semantic results using Reciprocal Rank Fusion.

    RRF score = sum(1 / (k + rank)) across all result lists where the item appears.

    Args:
        keyword_hits: Results from keyword search (ordered by relevance)
        semantic_hits: Results from semantic search (ordered by similarity)
        k: RRF constant (default 60)
        top_k: Number of results to return

    Returns:
        Merged and re-ranked list of hits
    """
    # Build map by (source_id, chunk_text_prefix) to avoid exact string matching issues
    merged: dict[tuple[int, str], _RankedHit] = {}

    def _key(hit: _RankedHit) -> tuple[int, str]:
        return (hit.source_id, hit.chunk_text[:200])

    # Score keyword results
    for rank, hit in enumerate(keyword_hits):
        key = _key(hit)
        rrf_score = 1.0 / (k + rank + 1)  # rank is 0-based, RRF uses 1-based
        if key in merged:
            merged[key].rrf_score += rrf_score
            merged[key].in_keyword = True
        else:
            hit_copy = _RankedHit(
                source_id=hit.source_id,
                source_name=hit.source_name,
                source_type=hit.source_type,
                chunk_text=hit.chunk_text,
                page_reference=hit.page_reference,
                rrf_score=rrf_score,
                in_keyword=True,
                in_semantic=False,
            )
            merged[key] = hit_copy

    # Score semantic results
    for rank, hit in enumerate(semantic_hits):
        key = _key(hit)
        rrf_score = 1.0 / (k + rank + 1)
        if key in merged:
            merged[key].rrf_score += rrf_score
            merged[key].in_semantic = True
        else:
            hit_copy = _RankedHit(
                source_id=hit.source_id,
                source_name=hit.source_name,
                source_type=hit.source_type,
                chunk_text=hit.chunk_text,
                page_reference=hit.page_reference,
                rrf_score=rrf_score,
                in_keyword=False,
                in_semantic=True,
            )
            merged[key] = hit_copy

    # Sort by fused score and return top_k
    sorted_hits = sorted(merged.values(), key=lambda h: h.rrf_score, reverse=True)
    return sorted_hits[:top_k]


def label_evidence(hits: list[_RankedHit]) -> list[SearchResult]:
    """
    Assign explainable evidence labels (HIGH/MEDIUM/LOW) to search results.

    Labeling criteria:
    - HIGH: Found in both keyword AND semantic results, or top-scoring result
    - MEDIUM: Found in one list only, or middle-range score
    - LOW: Weak similarity, bottom of results

    Args:
        hits: Ranked hits from RRF merge or single-mode search

    Returns:
        List of SearchResult with evidence labels
    """
    if not hits:
        return []

    # Normalize scores to 0-1 range for thresholding
    max_score = max(h.rrf_score for h in hits)
    if max_score == 0:
        max_score = 1.0

    results = []
    for hit in hits:
        normalized = hit.rrf_score / max_score

        if hit.in_keyword and hit.in_semantic:
            # Found by both methods — strong evidence
            label = "HIGH"
            reason = "matched by keyword and semantic search"
        elif normalized >= HIGH_THRESHOLD:
            label = "HIGH"
            if hit.in_semantic:
                reason = "strong semantic similarity"
            else:
                reason = "strong keyword match"
        elif normalized >= MEDIUM_THRESHOLD:
            label = "MEDIUM"
            if hit.in_semantic:
                reason = "moderate semantic similarity"
            elif hit.in_keyword:
                reason = "partial keyword match"
            else:
                reason = "moderate relevance"
        else:
            label = "LOW"
            reason = "weak similarity"

        results.append(
            SearchResult(
                source_id=hit.source_id,
                source_name=hit.source_name,
                source_type=hit.source_type,
                chunk_text=hit.chunk_text,
                page_reference=hit.page_reference,
                evidence_label=label,
                evidence_reason=reason,
                score=hit.rrf_score,
            )
        )

    return results


def overall_label(results: list[SearchResult]) -> str:
    """
    Generate an aggregate evidence summary.

    Args:
        results: Labeled search results

    Returns:
        Human-readable overall evidence label
    """
    if not results:
        return "No results found"

    high_count = sum(1 for r in results if r.evidence_label == "HIGH")
    medium_count = sum(1 for r in results if r.evidence_label == "MEDIUM")
    total = len(results)

    # Find strongest match
    best = results[0]  # Already sorted by score
    best_desc = f'strongest match from "{best.source_name}"'

    if high_count >= 2:
        return f"HIGH \u2014 {high_count} sources support this, {best_desc}"
    elif high_count == 1:
        return f"HIGH \u2014 1 strong source, {best_desc}"
    elif medium_count >= 2:
        return f"MEDIUM \u2014 {medium_count} partial matches found"
    elif medium_count == 1:
        return f"MEDIUM \u2014 1 source found, {results[0].evidence_reason}"
    else:
        return f"LOW \u2014 {total} weak match{'es' if total != 1 else ''} found"


def _rows_to_dicts(rows, cursor) -> list[dict]:
    """Convert cursor rows to list of dicts (handles both SQLite and psycopg2)."""
    if not rows:
        return []

    # If rows are already dicts (SQLite Row or psycopg2 RealDictCursor)
    if isinstance(rows[0], dict):
        return rows

    # Try to get column names from cursor description
    if cursor.description:
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in rows]

    return []
