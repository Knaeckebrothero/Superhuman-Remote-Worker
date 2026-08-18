-- migration:     0008_kb_index_chunking.sql
-- description:   OKF files-canonical KB — slice 3 PR1 (docs/features/okf_knowledge_base.md
--                §5.1 / §11 slice-3 PR1). The SCHEMA half of the retrieval
--                cutover, additive-only and inert: it adds the columns and tables
--                the chunk-granular git-tree reindexer will WRITE, but nothing
--                READS them until PR4 flips `search_knowledge` over. Mirrors the
--                0006/0007 ALTER-ADD-COLUMN precedent — no table rewrite, no data
--                touched on the existing hot path, behaviour-preserving by
--                construction (every new column is NULL for existing rows and no
--                current query references it).
--
--                (1) knowledge_index gains the note-level columns the reindexer
--                    needs. The note row becomes metadata-only in the new model —
--                    the embedding moves to knowledge_chunks (2):
--                      - kb_id UUID           KB scope. For a project-scoped KB
--                                             this equals project_id (backfilled
--                                             below so kb-scoped reads see the
--                                             corpus a project already has).
--                      - path TEXT            note location relative to KB root
--                                             (§6: path is *location*, the `id`
--                                             slug in note_id is *identity*).
--                                             UNIQUE (kb_id, path).
--                      - blob_sha VARCHAR(64) git blob sha of the source note —
--                                             per-row self-heal so an interrupted
--                                             reindex skips already-current rows
--                                             (§5). The watermark only advances at
--                                             the end; blob_sha makes re-runs cheap.
--                      - superseded_by VARCHAR(100)  slug of the replacement note
--                                             (same KB). The supersede edge lives
--                                             only in Neo4j today; this column
--                                             carries the chain when the Neo4j
--                                             write path is retired in PR4 (§5.1 —
--                                             required, not optional). Soft slug
--                                             reference (no FK): the target may not
--                                             be indexed yet mid-rebuild.
--                      - invalidated_at TIMESTAMPTZ  transaction-time of retirement.
--                      - embedding_version TEXT  pipeline stamp (model id + dims +
--                                             chunker version). Retrieval will
--                                             filter WHERE embedding_version =
--                                             current so mixed-model vectors can't
--                                             drift silently (great results on
--                                             fresh rows, garbage on stale, no
--                                             error anywhere). Bumping it = per-KB
--                                             rebuild; the index is disposable.
--                (2) knowledge_chunks — the chunk-granular embed rows. Each note
--                    fans out into >= 1 heading-aware chunk (short notes stay
--                    single-chunk); the dense vector lives HERE, not on the note
--                    row. ON DELETE CASCADE off the note row so deleting a note
--                    reaps its chunks. The halfvec HNSW index mirrors 0005's shape
--                    (subvector prefix — pgvector's plain `vector` HNSW caps at
--                    2000 dims); built in-transaction because the table is empty
--                    at migration time, so it lands atomically with the table.
--                (3) kb_index_watermark — one row per KB: the git commit the index
--                    was built as-of, plus repo/branch/pipeline_version. The
--                    incremental reindexer (PR3) diffs HEAD against indexed_commit
--                    and re-embeds only the changed blobs.
--
--                No function rewrites here: PR4 owns the retrieval cutover
--                (knowledge_hybrid_search over chunk rows with kb_id). This
--                migration only lays down structure.
-- depends-on:    0007_knowledge_index_ttl.sql
-- expected:      < 1s on dev/eval — six ALTER ADD COLUMN (fast default), one
--                UPDATE backfill of kb_id, two partial/plain indexes, two CREATE
--                TABLE plus their indexes. No vector data on knowledge_index
--                touched, no table rewrite.
-- locks:         brief ACCESS EXCLUSIVE on knowledge_index for the ALTERs; the new
--                tables start empty so their index builds are instant.
-- transactional: YES.

-- ---------------------------------------------------------------------------
-- 1. knowledge_index note-level columns
-- ---------------------------------------------------------------------------
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS kb_id UUID;
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS path TEXT;
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS blob_sha VARCHAR(64);
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS superseded_by VARCHAR(100);
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS embedding_version TEXT;

-- Backfill kb_id = project_id for existing rows so kb-scoped reads (PR4) see the
-- corpus a project already accumulated. A project-scoped KB's id IS its
-- project_id; a future multi-KB-per-project split assigns distinct ids then.
UPDATE knowledge_index SET kb_id = project_id WHERE kb_id IS NULL;

-- UNIQUE (kb_id, path): a KB holds at most one note per location. Partial so the
-- pre-reindex rows (path still NULL) don't participate — Postgres already treats
-- NULLs as distinct, but the predicate makes the intent explicit and keeps the
-- index tight. upsert_kb_note targets this arbiter (always non-null kb_id+path).
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_kb_path
    ON knowledge_index (kb_id, path)
    WHERE kb_id IS NOT NULL AND path IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_knowledge_kb ON knowledge_index (kb_id);

-- ---------------------------------------------------------------------------
-- 2. knowledge_chunks — chunk-granular embed rows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    note_row UUID NOT NULL REFERENCES knowledge_index(id) ON DELETE CASCADE,
    kb_id UUID NOT NULL,                       -- denormalized KB scope (retrieval filters here, no join)
    chunk_ix INT NOT NULL,                     -- 0-based position within the note
    heading_path TEXT,                         -- breadcrumb ("Design > Storage > Chunking")
    content TEXT NOT NULL,                     -- chunk body (display + reranking)
    embedding vector(4096),                    -- dense embedding (qwen3-embedding-8b, 4096-dim)
    search_doc tsvector,                       -- full-text arm for chunk-granular RRF
    embedding_version TEXT,                    -- pipeline stamp (model id + dims + chunker version)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_knowledge_chunk UNIQUE (note_row, chunk_ix)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_note ON knowledge_chunks (note_row);
-- Retrieval scopes by (kb_id, embedding_version) — the exact filter PR4 applies
-- alongside the vector search, covered here so the residual equality is free.
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_kb_version
    ON knowledge_chunks (kb_id, embedding_version);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_search
    ON knowledge_chunks USING GIN (search_doc);
-- Dense arm: HNSW over the 4000-dim halfvec prefix (mirrors 0005 on
-- knowledge_index; the plain `vector` opclass rejects > 2000 dims). In-transaction
-- is safe here — the table is empty at migration time.
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding ON knowledge_chunks
    USING hnsw ((subvector(embedding, 1, 4000)::halfvec(4000)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- 3. kb_index_watermark — one row per KB, the as-of commit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kb_index_watermark (
    kb_id UUID PRIMARY KEY,
    repo_name TEXT,
    branch TEXT,
    indexed_commit VARCHAR(64),
    pipeline_version TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
