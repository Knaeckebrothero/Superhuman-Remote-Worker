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

**Status:** ✅ RESOLVED 2026-07-29 — `::varchar` casts on `$4`/`$6`, committed
`f3f01b5d` on `develop` (pending push + deploy). Diagnosis below was correct on
the mechanism; two details are refined in "Resolution" — the failure is
**value-independent** (parse-time, so the NULL-`source_head` note under
"Suggested fix" is wrong), and the re-embed cost runs through
`pipeline_version`, not the commit diff.

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

## Resolution

**Shipped** (`f3f01b5d`, `develop`, not yet pushed): `$4` and `$6` cast to
`::varchar` at *every* occurrence in `upsert_watermark`, so neither parameter is
deduced at all and the type no longer depends on which use Postgres resolves
first. Unqualified `::varchar` rather than `::varchar(64)` on purpose — a cast to
a length-qualified type truncates silently, whereas assignment to the column
keeps its length check and errors loudly on an over-long commit id.
`set_watermark_status` was left alone: its `COALESCE`'s second argument is a
column, which pins the type on its own.

Regression test: `tests/test_kb_watermark_bindings.py` (real pgvector container +
the real vector migrations). Four of its six tests fail against the unfixed
statement with the production error text; a fifth is the `set_watermark_status`
control and the sixth is a canary asserting the pre-fix SQL is still rejected, so
the suite cannot go quietly vacuous if the fixture is ever loosened.

### Correction 1 — the failure is value-independent

"Suggested fix" above says the NULL-`source_head` case is the one that triggers
it. It isn't. The statement is rejected at **parse** time, so `$6`'s value never
enters into it: NULL and non-NULL, fresh INSERT and `ON CONFLICT`, all fail
identically. The proof is the live caller itself — `kb_reindex.py:836` passes
`source_head=head`, **never NULL**, and still died. If the bug were
value-dependent the reindexer would never have hit it.

### Correction 2 — the cost ran through `pipeline_version`, not the diff

"Why it matters" describes each run re-diffing from zero. The real mechanism is
sharper, and worse. `upsert_watermark` is the **only** writer of
`pipeline_version` (and of `indexed_commit`); `set_watermark_status` writes
neither. With it always failing, `pipeline_version` stayed NULL, so

```python
full = force_full or wm is None or wm.pipeline_version != current_version  # always True
```

every run took the **full** path — and `plan_reindex(..., full=True)` re-upserts
every path in the tree, *bypassing the per-row `blob_sha` guard* that otherwise
makes re-runs cheap. So the one mechanism that could have limited the damage was
disabled, and the `up-to-date` short-circuit (`kb_reindex.py` ~584) was
unreachable.

### Verified end-to-end on k3d, 2026-07-29

Six real vault notes added under `knowledge/` in project `9acaf531`'s jobs repo,
driven through `POST /api/projects/{id}/knowledge/reindex`:

| run | request | result | elapsed |
| --- | --- | --- | --- |
| A | incremental, 6 new notes | `completed`, upserted=6, cursor → `e12e9eef1fc3` | 3335 ms |
| B | immediate re-run | `up-to-date`, upserted=0 | **121 ms** |
| C | after restoring a bug-shaped row | `full=true`, upserted=6 with *zero* git changes | 2512 ms |
| D | immediate re-run | `up-to-date`, upserted=0 | 152 ms |
| E | after deleting the notes | `completed`, deleted=6 | — |

Run B is the criterion above: 27× faster, zero re-embeds. Run C reproduces the
old waste on demand and then **self-heals the row**, which is why no backfill of
existing broken watermarks is needed — the next successful reindex fixes them.
Test notes were removed afterwards (run E); the KB is back to its pre-test shape.

**Related:** [[kb_reindex_duplicate_key_on_legacy_notes]] tracked its own
diagnosis behind this one. Note that `kb_index_watermark.status` only becomes a
usable signal once this fix is **deployed** — committing it is not enough, so
that document's row-level queries stay the right tool until then.

**Environment note:** file-backed KB reindex cannot run on local k3d as shipped —
the chunker's `tiktoken` fetches its BPE vocab from `openaipublic.blob.core.windows.net`
at runtime and pods have no external DNS, so every note errors and the run
reports `partial`. It hides because KBs with only agent-written notes
(`path IS NULL`) never invoke the chunker. Workaround used here: seed
`/tmp/data-gym-cache/<sha1(url)>` in the orchestrator pod (no env var or restart
needed). Baking the vocab into the image would fix it properly.
