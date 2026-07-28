---
tags:
  - issue
  - knowledge-base
  - okf
  - reindex
  - orchestrator
  - cost
related:
  - "[[okf_knowledge_base]]"
  - "[[kb_convergence_ttl_reverification]]"
  - "[[project_backlog_pipeline]]"
---

# Every KB reindex reports `failed` and never advances its watermark, so each run re-embeds the whole vault

**Filed:** 2026-07-28, from live dev evidence while verifying the project-backlog
pipeline. Line numbers are develop @ 2026-07-28. **Not caused by the backlog
feature** — `git log -L` dates the defect to `e27a9313` (2026-07-11, "feat: add OKF
knowledge base datasources"), 16 days earlier.

## Symptom

A reindex does its work correctly — notes are parsed, rows are written, chunks are
embedded — and then dies at the very last step. The KB is left with:

- `kb_index_watermark.status = 'failed'`
- `indexed_commit` **empty** (never advanced)
- `last_success_at` **NULL** (no run has ever been recorded as successful)
- `last_error = 'inconsistent types deduced for parameter $4 / DETAIL: text versus character varying'`

Observed live on two projects:

| kb_id | project | rows written | watermark after |
| --- | --- | --- | --- |
| `becb5a96-1d8d-4916-a2e2-755dfd86cb3a` | Research RAG technologies | **292** | `failed`, `indexed_commit` empty |
| `5538a8e3-ceeb-4b4b-8bdc-19b57b713096` | Einkauf Confidential | — | `failed`, same error |

## Root cause

`KnowledgeStore.upsert_watermark` (`src/services/knowledge_store.py:539-580`) binds `$4`
(`indexed_commit`) in two contexts whose types Postgres resolves differently:

```sql
VALUES ($1, $2, $3, $4, $5, COALESCE($6, $4), $7, NOW(), ...)
...
   indexed_commit = $4,
   source_head    = COALESCE($6, $4),
```

`$4` is assigned directly to `indexed_commit` (deduced `character varying`) and also
appears inside `COALESCE($6, $4)` feeding `source_head`. Both arguments of that COALESCE
are untyped parameters, so Postgres resolves the expression to its preferred unknown
type, `text`. `$4` therefore has to be both `varchar` and `text`, and asyncpg refuses
the statement.

**The sibling `set_watermark_status` (`:591-625`) has a similar-looking COALESCE and
works fine** — and the difference is the whole diagnosis. There the expression is
`COALESCE($4, kb_index_watermark.source_head)`: the second argument is a *column*, which
pins the type to `varchar`, matching `$4`'s other use. Only `upsert_watermark` puts two
untyped parameters in the same COALESCE.

Both columns are genuinely `character varying` in the live schema — verified — so this
is **not** a schema mismatch. It is parameter-type deduction across the two usages.

## Why it matters

1. **Recurring embedding spend.** The watermark is the incremental-reindex cursor. It
   never advances, so every reindex re-diffs from zero and re-embeds the entire vault.
   The RAG run above embedded 292 notes and recorded none of it; the next run will do
   all 292 again.
2. **The status field is useless as a signal.** `failed` is reported even when every
   row was written successfully, so nothing downstream (or nobody watching) can
   distinguish a real failure from this one.
3. **It masks genuine reindex failures**, including the duplicate-key errors tracked in
   [[kb_reindex_duplicate_key_on_legacy_notes]].

## Suggested fix

Cast `$4` explicitly at every use so both contexts agree, e.g. `$4::varchar` in the
direct assignments and `COALESCE($6::varchar, $4::varchar)` for `source_head`. Verify
against a real pgvector container rather than unit tests alone — the failure only
appears at statement-prepare time against Postgres, so a mocked store will not
reproduce it.

Add a regression test that calls `set_watermark` against a live (or containerised)
database with `$6` both NULL and non-NULL; the NULL case is the one that triggers it.

## Verification

After the fix, a reindex of `becb5a96` should leave `status='ready'`, a non-empty
`indexed_commit`, and a populated `last_success_at`; a second immediate reindex should
be a near-no-op rather than a full re-embed.
