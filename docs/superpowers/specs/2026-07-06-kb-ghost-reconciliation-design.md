# KB ghost reconciliation (R-1) — design

**Date:** 2026-07-06
**Status:** approved, pre-implementation
**Context:** OKF files-canonical KB. Closes the root cause behind the migration-hygiene
audit's "ghost rows" (task #34 → worklist item R-1 in
`docs/features/okf_kb_hygiene_worklist.md`). Sibling code fixes B-1/D-1/D-2 and C-1
already shipped on `develop` (`ae0cddc9`, `33396baa`).

## Problem

A `knowledge_index` row has **two independent writers** that key it differently:

1. **Agent `kb_write` / `kb_update`** → `KnowledgeStore.upsert_note` inserts by
   `(project_id, note_id)` and sets **neither `kb_id` nor `path`**. Every agent write
   is therefore born **pathless** (search-available immediately, but "unconfirmed").
2. **Reindexer** (`orchestrator/services/kb_reindex.py`, 15-min sweep) reads the KB's
   git tree and, when it sees the committed file, calls `adopt_legacy_row(kb_id,
   note_id, path)` — which stamps `kb_id = project_id` and `path` onto the pathless
   row (docstring: *"project_id doubles as the kb id"*). From then on the row is
   path-backed and the tree-diff manages it, including `delete_kb_note` when the file
   later vanishes.

**The gap:** adoption is **slug-keyed** (`project_id + note_id`), deletion is
**path-keyed** (`kb_id + path`). A pathless row only gets adopted if a file with its
exact slug lands in the indexed tree. If that never happens — failed/rolled-back
commit, squash-merge dropping the file, `filename ≠ frontmatter id`, or
created-then-deleted between sweeps — the row is **orphaned forever**: the path-keyed
delete pass literally cannot see it (the reindexer's own docstring: *"legacy pathless
rows … are invisible to the diff"*).

This is a **reconciliation gap, not a delete gap.** Evidence from the 2026-07-06 audit
(project `68137e29`): 384 active pathless rows, **0 matching any live file slug** — the
un-adopted residue. A one-off cleanup (worklist B-2) only drains today's pile; without
closing this, it refills (the count was seen drifting 250 → 396 → 384).

## Goal / non-goals

**Goal:** the reindexer retires pathless rows that the git tree can never adopt, so
ghosts stop accumulating and the existing 384 drain automatically on the next clean
reindex. B-2 becomes a no-op.

**Non-goals (explicitly out of scope, with rationale):**

- **Path-at-birth** (making `upsert_note` set the path immediately). Rejected: the
  pathless-until-adopted state is an *intentional* pattern — a note is search-available
  before its file is confirmed in the indexed tree. Setting the path at birth risks
  premature reaping of in-flight rows and fights the existing design.
- **"Prompt tool-delete" propagation.** Rejected as redundant: there is no agent
  delete tool, and `kb_update` retire (supersede/archive) *already* dual-writes status
  to the DB via `upsert_note`'s metadata path. The one residual edge (the pgvector
  write-through is deliberately non-fatal, so a flaky write can leave file/DB status
  drift) self-heals: for path-backed rows the reindexer treats the file as truth; for a
  pathless row whose file exists, the next reindex adopts it and writes the file's
  status; and Component 1 mops up genuine orphans. No surface justifies a dedicated
  mechanism under the current feature-freeze. If a real hard-delete tool ever lands, DB
  propagation gets wired then.

## Design — one reconciliation pass

### New store method

```python
async def reconcile_orphans(
    self,
    project_id: uuid.UUID,
    tree_slugs: Sequence[str],
    grace: datetime.timedelta = datetime.timedelta(hours=1),
) -> int:
    """Archive pathless active rows the git tree can never adopt.

    A row written by the agent write-through (`upsert_note`) carries
    `(project_id, note_id)` but no `kb_id`/`path`. The reindexer adopts it once a
    file with `note_id`'s slug appears in the tree. A pathless active row whose
    `note_id` matches NO current tree slug, and that has sat unadopted past the
    adoption grace, is an orphan (failed/squashed commit, slug mismatch,
    create-then-delete). Soft-archive it — reversible, and it drops out of every
    functional surface (search/near-dup via status, list/read via the path guard).

    Keyed on `project_id`, NOT `kb_id`: unadopted ghosts have `kb_id IS NULL`.
    Returns the number of rows archived.
    """
```

SQL:

```sql
UPDATE knowledge_index
   SET status = 'archived', invalidated_at = now()
 WHERE project_id = $1
   AND path IS NULL
   AND status = 'active'
   AND note_id <> ALL($2::text[])          -- $2 = every slug in the current tree
   AND indexed_at < now() - $3::interval    -- adoption grace
RETURNING id
```

### Reindexer integration

In `_reindex_kb_unlocked` (`orchestrator/services/kb_reindex.py`), after the delete
loop and **before** the watermark write:

```python
# Reconcile orphaned provisional rows (R-1): pathless active rows the tree can
# never adopt. current_map holds EVERY knowledge file in the tree, so the slug
# set is complete even on incremental runs.
tree_slugs = [
    posixpath.basename(p)[: -len(".md")] for p in current_map
]
reconciled = await store.reconcile_orphans(project_id=kb_id, tree_slugs=tree_slugs)
```

- `kb_id` passed to the reindexer **is** the project id (`adopt_legacy_row` relies on
  the same identity), so it is the correct `project_id` argument.
- Runs only when the tree fetch succeeded (already guaranteed here — a `None` tree
  returns early at `tree-fetch-failed`). It trusts `current_map` to be the *complete*
  set of tree files — exactly the same trust the existing path-keyed delete pass
  already places in it (a truncated tree would mis-delete today too), so reconciliation
  introduces no new trust boundary.
- Independent of per-note upsert errors: it needs only the complete tree slug set + the
  DB, so a partial upsert run should still reconcile. It does **not** gate the
  watermark; the watermark keeps its `errors == 0` rule.
- **Non-fatal:** wrap the call in `try/except`, log a warning on failure, and do **not**
  increment `errors` — a hygiene pass must never wedge the watermark or fail the
  reindex. (Matches the reindexer's defensive posture around the near-duplicate/link
  passes.)
- Add `reconciled` to the returned summary dict and the log line, alongside
  `upserted` / `deleted` / `skipped` / `errors`.

### Key decisions

- **Disposition: archive, not hard-delete.** This is an *automated* pass firing every
  sweep on an *inferred* condition, unlike the existing path-keyed hard-delete (which
  acts on the *demonstrated* fact that a file that provably existed is now gone).
  Archiving makes any misfire recoverable, leaves an `invalidated_at` audit trail, is
  consistent with the B-2 decision, and still fully neutralizes the ghost. The existing
  hard-delete for vanished path-backed files is unchanged.
- **Adoption grace = 1 hour, anchored on `indexed_at`.** Protects a just-written
  provisional row whose file is committed but not yet in the fetched HEAD. `indexed_at`
  (last write) rather than `created_at` (first write) is the anchor, so a recently
  re-touched row is never reaped — a true orphan is never re-touched (no file, agent
  moved on), so its `indexed_at` ages out and it reaps. Comfortably exceeds the 15-min
  sweep plus commit/merge latency. Single constant; no new state.
- **Slug set from filenames (`basename` minus `.md`).** In files-canonical layout the
  renderer writes `knowledge/<id>.md`, so `filename == frontmatter id == adoptable
  key`. A hand-authored file whose filename differs from its frontmatter id is the only
  divergence; it is lint-flagged already, and archive is reversible, so the residual
  false-reap risk is negligible. (Using exact frontmatter ids would require parsing
  every file on every run — rejected as not worth the cost.)

## Edge cases

- **Empty tree, ghost rows present:** `note_id <> ALL('{}')` is TRUE in Postgres → all
  pathless active rows past grace archive. Correct: no files means every pathless row
  is an orphan.
- **In-flight provisional row (file committed after the fetched HEAD):** excluded by
  the 1-hour grace; adopted on a later run.
- **Row whose file exists but was never adopted (slug in tree):** `note_id` is in
  `tree_slugs` → not reaped; the same reindex adopts it normally.
- **Re-run idempotence:** an already-archived row has `status <> 'active'` → never
  re-touched.
- **`kb_id`-keyed vs `project_id`-keyed:** the query MUST use `project_id`; a `kb_id`
  filter would match zero ghosts (their `kb_id` is NULL). Called out because every
  other reindexer query keys on `kb_id`.

## Testing (TDD)

Store unit tests (`tests/test_knowledge_store.py`, mock db, assert SQL + params):

- `reconcile_orphans` issues an UPDATE filtered on `project_id`, `path IS NULL`,
  `status = 'active'`, `note_id <> ALL(...)`, and an `indexed_at` grace bound; sets
  `status = 'archived'` + `invalidated_at`.
- Passes the tree-slug array and grace interval as params; returns the row count.
- Keyed on `project_id`, never `kb_id` (guard against the landmine).

Reindexer test (`tests/test_kb_reindex.py`):

- A pathless active row whose slug is absent from the tree is reconciled (call
  observed), while an adoptable/path-backed row is not; `reconciled` appears in the
  summary dict.
- Reconciliation still runs on a partial (some-error) upsert run; still skipped on
  `tree-fetch-failed` (early return).

All new behavior RED-first. Existing reindexer/store suites stay green.

## Rollout

- Pure additive code in this repo; ships via the normal `develop` → CI → dev deploy.
- On the first clean reindex after deploy, the 384 existing ghosts (slug not in tree,
  well past 1h) archive automatically → **B-2 is retired as a no-op.**
- Dormant-safe: the change is in the reindexer, which runs regardless of
  `databases.neo4j.enabled`; it does not touch the kg-less read/write flip.

## Future (not now)

- Optional housekeeping to hard-delete rows that have been archived-and-pathless for a
  long window (e.g., 30 days) if the inert-row count ever matters. Deliberately
  deferred — archived pathless rows are harmless.
