#!/usr/bin/env python3
"""Backfill source_embeddings for registered sources that missed auto-embed.

The overflow defect (knowledge-base/knowledge/issues/
embedding_batch_overflow_skips_citation_source_embeddings.md) skipped the
automatic embedding of ~1/3 of registered sources: whole-source chunk lists
hit the TEI backend as one >64-input request, the 422 was swallowed, and the
source stayed registered but invisible to semantic search. The batching seam
in ``EmbeddingService.embed_batch`` stops new occurrences; this script is the
sweep for rows already missing (fix 6).

Coverage unit is the (job_id, source_id) pair — ``source_embeddings`` is
keyed per job — and ``sources.content`` is persisted, so a backfill needs no
re-fetch. Idempotent: candidates are pairs with zero embedding rows, and the
insert is the same ON CONFLICT upsert the engine uses, so a re-run reports
zero candidates.

Dry-run by default (lists coverage per job; needs only the vector DB).
``--apply`` embeds and needs the EMBEDDING_* env of a real backend — on
clusters without one (local k3d) use dry-run only. Run from the repo root
(imports ``src.services.embedding_service`` + the citation chunker), e.g.
against port-forwarded DBs (scripts/port-forward-dbs.sh):

    VECTOR_DB_URL=postgresql://user:pass@localhost:5433/srw_vector \
        python scripts/backfill_source_embeddings.py
    ... --apply   # with EMBEDDING_BASE_URL/EMBEDDING_API_KEY set
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill_source_embeddings")


def _resolve_dsn() -> str:
    url = os.getenv("VECTOR_DB_URL")
    if url:
        return url
    user = os.getenv("VECTOR_POSTGRES_USER")
    password = os.getenv("VECTOR_POSTGRES_PASSWORD")
    host = os.getenv("VECTOR_POSTGRES_HOST", "localhost")
    port = os.getenv("VECTOR_POSTGRES_PORT", "5433")
    db = os.getenv("VECTOR_POSTGRES_DB", "srw_vector")
    if not (user and password):
        log.error(
            "No vector DB credentials: set VECTOR_DB_URL or "
            "VECTOR_POSTGRES_USER/PASSWORD (+HOST/PORT/DB)"
        )
        sys.exit(2)
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


CANDIDATES_SQL = """
SELECT js.job_id, js.source_id,
       length(s.content) AS content_len,
       s.metadata->'embedding_state'->>'status' AS state,
       s.metadata->'embedding_state'->>'reason_type' AS reason_type
FROM job_sources js
JOIN sources s ON s.id = js.source_id
WHERE NOT EXISTS (
    SELECT 1 FROM source_embeddings se
    WHERE se.source_id = js.source_id AND se.job_id = js.job_id
)
ORDER BY js.job_id, js.source_id
"""

UPSERT_SQL = """
INSERT INTO source_embeddings (source_id, job_id, chunk_index, chunk_text, embedding)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (source_id, job_id, chunk_index)
DO UPDATE SET chunk_text = EXCLUDED.chunk_text, embedding = EXCLUDED.embedding
"""


async def _set_state(conn, source_id: int, state: dict) -> None:
    state = {**state, "updated_at": datetime.now(timezone.utc).isoformat()}
    await conn.execute(
        """
        UPDATE sources
        SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb),
                                 '{embedding_state}', $2::jsonb)
        WHERE id = $1
        """,
        source_id,
        json.dumps(state),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="embed missing pairs (default: dry-run)"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="cap pairs processed in --apply (0 = all)"
    )
    args = parser.parse_args()

    import asyncpg

    conn = await asyncpg.connect(_resolve_dsn())
    try:
        rows = await conn.fetch(CANDIDATES_SQL)
        by_job: dict = {}
        for r in rows:
            by_job.setdefault(str(r["job_id"]), []).append(r)

        log.info(
            "Coverage gap: %d (job, source) pairs across %d job(s) have no "
            "source_embeddings rows",
            len(rows),
            len(by_job),
        )
        for job_id, items in sorted(by_job.items()):
            states = {}
            for r in items:
                key = r["state"] or "unrecorded"
                states[key] = states.get(key, 0) + 1
            log.info("  job %s: %d missing (%s)", job_id[:8], len(items), states)

        if not args.apply:
            log.info("Dry-run — pass --apply (with EMBEDDING_* env) to backfill.")
            return 0

        # Apply mode mirrors CitationEngine._embed_source_content: same
        # chunker, the shared batching seam, the same upsert.
        from agent.citation_engine.chunking import SemanticChunker
        from shared.runtime.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        chunker = SemanticChunker(embedding_service=None)

        try:
            from pgvector.asyncpg import register_vector

            await register_vector(conn)
        except Exception:
            log.warning("pgvector codec unavailable — inserting via ::vector cast")

        done = failed = 0
        for r in rows:
            if args.limit and done + failed >= args.limit:
                break
            source_id, job_id = r["source_id"], r["job_id"]
            content = await conn.fetchval(
                "SELECT content FROM sources WHERE id = $1", source_id
            )
            try:
                chunks = chunker.chunk(content or "")
                if not chunks:
                    await _set_state(
                        conn, source_id, {"status": "failed", "reason_type": "empty"}
                    )
                    failed += 1
                    continue
                vectors = await service.embed_batch(chunks)
                async with conn.transaction():
                    for idx, (chunk_text, vector) in enumerate(zip(chunks, vectors)):
                        await conn.execute(UPSERT_SQL, source_id, job_id, idx, chunk_text, vector)
                await _set_state(
                    conn, source_id, {"status": "complete", "chunks": len(chunks)}
                )
                done += 1
                log.info("embedded source %s for job %s (%d chunks)", source_id, str(job_id)[:8], len(chunks))
            except Exception as e:  # noqa: BLE001 — keep sweeping, record typed state
                failed += 1
                reason_type = type(e).__name__
                await _set_state(
                    conn,
                    source_id,
                    {
                        "status": "failed",
                        "reason_type": reason_type,
                        "reason": str(e)[:500],
                    },
                )
                log.warning("source %s failed (%s): %s", source_id, reason_type, e)

        log.info("Backfill finished: %d embedded, %d failed", done, failed)
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
