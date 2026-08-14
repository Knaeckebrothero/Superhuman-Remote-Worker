# A long wikilink silently deletes a note's links

**Status:** **Open, observed live on dev 2026-08-14.** No workaround applied; no code fix.
**Severity:** **Medium** — narrow (a handful of notes) but silently *destructive*: the affected
note ends up with fewer links than it had before, and it is re-indexed on every sweep forever.

## What happens

`knowledge_links.source_id` and `.target_id` are `character varying(100)`
(`orchestrator/database/vector_schema_current.sql:695`). Targets are not note ids the system
mints — they are `[[wikilink]]` texts lifted out of note bodies, so their length is bounded by
nothing at all.

When one exceeds 100 characters, PostgreSQL rejects the insert:

```
ERROR:  value too long for type character varying(100)
STATEMENT:  INSERT INTO knowledge_links
                (source_note_row, kb_id, source_id, target_id, rel_type)
            VALUES ($1, $2, $3, $4, $5)
```

## Why it destroys data rather than skipping a link

`replace_note_links` (`src/services/knowledge_store.py:1131`) rewrites a note's edges
wholesale — delete-all, then re-insert one at a time:

```python
await self.db.execute(
    "DELETE FROM knowledge_links WHERE source_note_row = $1", source_note_row
)
for target in targets:
    ...
    await self.db.execute("INSERT INTO knowledge_links ...")
```

The delete has already committed when the loop raises. So a note whose 12th link is 101
characters long keeps 11 edges and loses the rest — including every edge that was valid a
moment earlier. The failure is **not** idempotent: it deletes real edges and replaces them
with a prefix.

It then propagates out of the reindexer's per-note handler, which logs
`kb_reindex[...]: error on <path>` and — by the deliberate design noted at
`orchestrator/services/kb_reindex.py:888` ("Embed BEFORE writing: a failed embed leaves the
stale blob_sha in place, so the next run retries this note") — leaves `blob_sha` unstamped.
The note is therefore re-embedded and re-written on **every subsequent sweep**, forever,
spending embedding-API calls to fail in the same place.

## Observed

- Firing roughly twice per 20 minutes on dev after the pgvector recovery of 2026-08-14.
- `SELECT max(length(target_id)) FROM knowledge_links` → **99**. Pinned exactly at the ceiling
  is what silent truncation pressure looks like; nothing longer has ever been storable.
- 35,270 edges stored across 7,896 notes.

## Suggested fix

1. **Widen both columns.** They hold free text from note bodies; `text` is the honest type and
   costs nothing in PostgreSQL. A migration is `ALTER TABLE ... TYPE text` on both columns —
   no rewrite is required for a varchar→text widening.
2. **Make the rewrite atomic.** Wrap the delete + re-inserts in one transaction so a failure
   leaves the previous edge set intact. Wholesale replace is the right shape; doing it
   non-transactionally is not.
3. **Do not let one bad edge fail the note.** Even with (1), a target can be pathological
   (a whole paragraph inside `[[ ]]`). Skip and count the offending edge rather than aborting
   the note — a note that indexes with 11 of 12 links is worth far more than one that never
   stamps and re-embeds forever.
4. **Consider bounding at extraction.** The link extractor could drop targets beyond a sane
   length before they reach the store, so the DB constraint stops being load-bearing.

Fixes (2) and (3) matter independently of (1): they are what turn "one bad link" from a
data-destroying, budget-burning failure into a logged skip.

## Related

- [`kb_sweep_indexes_archived_projects_and_starves_connectors`](kb_sweep_indexes_archived_projects_and_starves_connectors.md)
  — the perpetual-retry consequence compounds with sweep fairness; fixed in `d215e727`.
- `docs/features/better_resavio_restart_status.md` §7 — found while auditing the KB after the
  2026-08-14 pgvector disk-full outage.
