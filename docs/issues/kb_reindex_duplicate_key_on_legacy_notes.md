---
tags:
  - issue
  - knowledge-base
  - okf
  - reindex
  - orchestrator
related:
  - "[[okf_knowledge_base]]"
  - "[[kb_reindex_watermark_never_advances]]"
  - "[[okf_kb_hygiene_worklist]]"
---

# Reindex throws `duplicate key value violates uq_knowledge_project_note` on notes that already exist from the agent write path

**Filed:** 2026-07-28, from live dev logs observed while verifying the project-backlog
pipeline. Line numbers are develop @ 2026-07-28.

## Symptom

`kb_reindex` logs a stream of per-note errors for a project whose notes were originally
written by agents rather than by the reindexer:

```
kb_reindex[68137e29-…]: error on knowledge/iter-1-proposal-002-stammgast-….md:
  duplicate key value violates unique constraint "uq_knowledge_project_note"
  DETAIL: Key (project_id, note_id)=(68137e29-…, iter-1-proposal-002-stammgast-…)
          already exists.
```

Observed repeatedly on project `68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a` ("Better
Resavio") — at least three distinct notes in a ten-minute window, and the pattern
suggests it hits every agent-authored note in that vault.

## Root cause (suspected — needs confirmation)

`knowledge_index` carries two mutually-unaware identity schemes:

- **Agent write path** — `KnowledgeStore.upsert_note`, keyed on
  `(project_id, note_id)` via `uq_knowledge_project_note`, writes rows with
  `path IS NULL`.
- **Reindexer path** — `KnowledgeStore.upsert_kb_note`, keyed on `(kb_id, path)` via a
  partial unique index, writes rows carrying a real `path`.

`adopt_legacy_row` exists to reconcile the two — a pathless row for the same note should
be adopted rather than re-inserted. The final review of the backlog feature explicitly
checked this seam and concluded adoption covered it. **These logs show it does not**, at
least for this vault: the reindexer reaches the INSERT and collides on the
project-scoped constraint.

What needs establishing: whether adoption is not being attempted for these rows, is
attempted and silently misses (e.g. a `note_id` derivation mismatch between the agent's
slug and the reindexer's filename stem), or races another writer.

## Why it matters

- The affected notes never gain their `path`-keyed row, so they stay invisible to any
  query that keys on `(kb_id, path)`.
- Their chunk index is not rebuilt, so hybrid search silently degrades for exactly the
  notes an active loop wrote.
- The errors are per-note and non-fatal, so the run continues and the damage is
  partial and quiet — currently doubly so, because the watermark bug
  ([[kb_reindex_watermark_never_advances]]) marks every run `failed` regardless, which
  masks this signal completely.

**Fix the watermark bug first** — until it is fixed, `status` cannot be used to tell
whether this issue is still occurring.

## Repro

Take a project whose notes were written by loop agents (`upsert_note`, `path IS NULL`)
and run a reindex over its vault. Compare, before and after:

```sql
SELECT count(*) FILTER (WHERE path IS NULL) AS pathless,
       count(*) FILTER (WHERE path IS NOT NULL) AS pathed
  FROM knowledge_index WHERE project_id = '<project>';
```

A healthy adoption converts pathless rows to pathed ones; the bug leaves the pathless
count unchanged while logging one duplicate-key error per note.
