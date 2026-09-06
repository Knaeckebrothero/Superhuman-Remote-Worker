-- migration:     0010_knowledge_links.sql
-- description:   OKF files-canonical KB — slice 3 PR4c (docs/features/okf_knowledge_base.md
--                §7 / §8 / §11 slice-3 PR4). The link edge set that lets the
--                graph query tools (kb_related, and the 1-hop provenance degrade)
--                work WITHOUT Neo4j — the "1-hop link-table queries" the Full tier
--                falls back to when the Graph tier is off.
--
--                In the files-canonical model the note body's markdown links ARE
--                the emergent graph (OKF §7 — standard `[text](slug.md)` links, not
--                wikilinks). The reindexer extracts each changed note's outbound
--                link targets and rewrites this table per note (delete + re-insert,
--                mirroring knowledge_chunks), so the edge set is a disposable
--                derived view of the vault, exactly like the chunk index.
--
--                Design choices:
--                  - target_id is stored as a SLUG, not a row FK. Resolution to a
--                    concrete note happens at READ time via a join on
--                    (kb_id, note_id) — so a link to a not-yet-indexed note during
--                    an incremental pass is kept (not dropped), and dead links
--                    (no active target) simply fall out of the read-time join.
--                    This is the "dead links stored as rows (target = NULL)"
--                    intent from §5.1, minus a brittle FK that mid-rebuild ordering
--                    would violate.
--                  - source_note_row FK ON DELETE CASCADE off knowledge_index, so
--                    deleting/replacing a note reaps its outbound edges (the
--                    reindexer's delete_kb_note and replace_note_links both rely
--                    on this).
--                  - source_id denormalized alongside source_note_row so the
--                    bidirectional 1-hop query (note as source OR target) is two
--                    index lookups, no join back to the note row.
--                  - rel_type defaults to 'references' (all body links are generic
--                    references in v1; typed edges — SUPERSEDES etc. — live in the
--                    superseded_by column from 0008 and in frontmatter, not here).
-- depends-on:    0009_knowledge_chunk_hybrid_search.sql
-- expected:      < 1s — one CREATE TABLE (starts empty) plus three indexes. No
--                existing data touched.
-- transactional: YES.

CREATE TABLE IF NOT EXISTS knowledge_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_note_row UUID NOT NULL REFERENCES knowledge_index(id) ON DELETE CASCADE,
    kb_id UUID NOT NULL,                         -- KB scope (retrieval filters here)
    source_id VARCHAR(100) NOT NULL,             -- source note slug (denormalized)
    target_id VARCHAR(100) NOT NULL,             -- target note slug (resolved at read time)
    rel_type TEXT NOT NULL DEFAULT 'references', -- generic body-link edge in v1
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Outbound arm of the 1-hop query (note as source) + inbound arm (note as target).
CREATE INDEX IF NOT EXISTS idx_knowledge_links_source ON knowledge_links (kb_id, source_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_target ON knowledge_links (kb_id, target_id);
-- Cascade + per-note replace both key off the source row.
CREATE INDEX IF NOT EXISTS idx_knowledge_links_note_row ON knowledge_links (source_note_row);
