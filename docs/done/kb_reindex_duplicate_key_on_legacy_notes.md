---
tags:
  - issue
  - knowledge-base
  - okf
  - reindex
  - orchestrator
  - resolved
related:
  - "[[okf_knowledge_base]]"
  - "[[kb_reindex_watermark_never_advances]]"
  - "[[okf_kb_hygiene_worklist]]"
  - "[[kb_duplicate_frontmatter_ids_collide_on_reindex]]"
---

# `kb_export` turned a note filename into a directory inside the vault, giving every note a duplicate OKF id

**Filed:** 2026-07-28, from live dev logs observed while verifying the project-backlog
pipeline. **Root cause confirmed and vault repaired:** 2026-07-29. Line numbers refreshed
against develop @ 2026-08-05.

**Status: CLOSED.** Prevention shipped — `a9d406b4` is on `origin/develop` with several
deploy commits after it, and the guard survived the KB-materialisation rework
(`571fbc8c`). Corrupt data cleaned up (vault `project-68137e29-jobs` @ `2cc5f7c3`),
verified at 0 nested rows. One unrelated residual is tracked separately —
see [Residual: four duplicate frontmatter ids](#residual-four-duplicate-frontmatter-ids).

## Symptom

`kb_reindex` logged a stream of per-note errors on project
`68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a` ("Better Resavio"):

```
kb_reindex[68137e29-…]: error on knowledge/iter-1-proposal-002-stammgast-….md:
  duplicate key value violates unique constraint "uq_knowledge_project_note"
  DETAIL: Key (project_id, note_id)=(68137e29-…, iter-1-proposal-002-stammgast-…)
          already exists.
```

**1811 such errors per full reindex**, one per affected note, on every run.

## Root cause

Not the agent-write/reindexer identity seam, and **not** `adopt_legacy_row` — the
original hypothesis in this document blamed adoption for "silently missing" these rows.
Adoption is correct. It claims a row that is pathless or whose path is in this run's
delete set; a row already sitting at a second *live* path is neither, so adoption
correctly declines, and `upsert_kb_note`'s `ON CONFLICT (kb_id, path)` arbiter cannot
absorb a conflict raised by the *different* constraint `uq_knowledge_project_note`.
The INSERT dies. Zero duplicate `note_id`s are ever persisted: the constraint is
working, and the reindexer is retrying an impossible insert.

**The vault was corrupt.** `kb_export` (`src/tools/knowledge/knowledge_tools.py`)
documents its `path` argument as a directory but never validated it:

```python
export_rel = path.rstrip("/")
workspace.create_directory(export_rel)          # no check that this IS a directory
for note in notes:
    workspace.write_file(f"{export_rel}/{note['id']}.md", _render_note_md(note))
```

Given a *note filename*, it creates a directory whose name ends in `.md` and fills it
with one re-rendered copy of every note in the graph. Two things then break:

1. **Git cannot hold a blob and a tree at one name**, so the note that path was named
   after ceases to exist as a file.
2. **The reindexer globs `knowledge/**/*.md` recursively** (`knowledge_blob_map`,
   `kb_reindex.py:132`), so every copy inside the vault carries the same OKF id as the
   real note. One of each pair wins the `(project_id, note_id)` key; the other fails
   forever.

On this project it happened twice:

| when | destination | files | indexed? |
| --- | --- | --- | --- |
| 2026-07-06 | `archive/kb_index_regenerated_2026-07-06.md/` | 1092 | no — outside `knowledge/`, inert |
| 2026-07-16 | `knowledge/iter-33-developer-plan-v1-adapt.md/` | 2716 | **yes — 1811 duplicate pairs** |

## Evidence chain

1. `slugify()` (`src/services/knowledge_graph.py:100`) strips `/` and `.`, so
   `_dual_write_note`'s flat `knowledge/{slug}.md` cannot nest. Ruled out at source.
2. Only three code paths write note `.md` files. `kb_index` writes exactly one
   (`index.md`); `_dual_write_note` is flat; **`kb_export` is the only one that appends
   `/{note_id}.md` to a caller-supplied path.**
3. Git pins creation of the `knowledge/` one to a single commit — `add0204c`, the
   squash-merge of job `38a4375e` (developer, loop iter 33, 2026-07-16) — adding 2716
   files, all `.md`, all flat at depth 3. `git ls-tree` shows the path as mode `040000`
   (a tree); it was never a blob at any commit on any branch.
4. **Frontmatter is the discriminator.** Canonical notes carry `author:`/`job:`/`branch:`
   because `_dual_write_note` injects provenance (`_note_provenance`). The nested copies
   carry none — matching `kb_export`, which renders `kg.get_all_notes_for_export()`
   straight through `_render_note_md`. Their `created:` also has *nanosecond* precision
   (`2026-06-27T16:53:32.898765000+00:00`), a Neo4j datetime, not Python's microseconds.
5. **The mechanism is proven end-to-end by an audited call.** The audit store holds
   exactly one successful `kb_export` ever: job `2a16e5d1` (scholar, same project,
   2026-07-06 13:17) called `kb_export(path="archive/kb_index_regenerated_2026-07-06.md")`
   and returned ``Exported 1092 note(s) to `archive/kb_index_regenerated_2026-07-06.md/` ``.
   That path is a tree in the repo holding exactly 1092 files. A note filename became a
   directory.
6. Those were the **only two** `.md`-shaped directories in the entire Gitea instance,
   both in this project, both `kb_export`-shaped.

### Alternatives ruled out

- **Orchestrator `/api/projects/{id}/knowledge/export`** — writes to a
  `tempfile.mkdtemp()`, emits wikilinks and no `description:` key. Wrong shape, wrong
  destination.
- **`workspace_converter`** — writes to Neo4j + pgvector only, no file I/O.
- **A copy of the 07-06 directory** — the 07-06 set is a strict subset of the 07-16 set,
  but **585 of the 1092 common notes differ byte-for-byte**. A copy would give 1092
  identical blobs. This is a second export ten days later against a grown, edited KB;
  `_render_note_md` is deterministic, so the 507 unchanged notes render identically.
- **A shell `cp`/`mv`** — no `run_command` calls in the write window.
- **A subagent** — `llm_requests` for that job shows `call_type='main'` only.

### Known gap

The 07-16 invocation has **no audit record**. Job `38a4375e`'s audit is continuous
through the write window (only `read_file`/`write_file` on `plan.md`), and no LLM
request or response anywhere on 2026-07-16 contains that path. The mechanism and the
output shape are proven; the invoking agent for that second occurrence is not
identified. Most likely an audit gap rather than a second mechanism — nothing else in
the codebase produces this output shape.

## Scale (measured 2026-07-28, before cleanup)

```
knowledge/iter-33-developer-plan-v1-adapt.md/   2716 files
  ├─ 1811 duplicated an existing knowledge/<name>.md   → 1811 collisions/run
  └─  905 existed nowhere else

knowledge_index rows pointing into it: 957
  ├─  52 shadowed a canonical file that existed (the canonical one lost)
  └─ 905 nested-only
```

## Fix

**Prevention** — `_export_dir_error` (`knowledge_tools.py`) rejects, before any Neo4j or
workspace work, a destination that is (a) a `*.md` filename or (b) anywhere under
`knowledge/`. The vault's own files already *are* the canonical OKF export, so a copy
inside it can only duplicate ids. The failure mode is a thousand files removed by hand,
so this validates first and writes nothing on rejection.

**Diagnosability** — the raw Postgres error names the note id and nothing else: not the
path being indexed, not the path the index holds, not the fact that two files claim one
identity. Read literally it suggests a write that should have succeeded is being
retried; the truth is the opposite. `_log_duplicate_note_id` (`kb_reindex.py`) detects
that specific constraint (asyncpg `constraint_name`, falling back to the message) and
emits **one line** carrying the id, both paths, and which one wins.
`find_note_id_owner` (`knowledge_store.py`) is the diagnostic-only lookup behind it —
keyed on `project_id`, not `kb_id`, because a pathless legacy row has no `kb_id`.

Committed as `a9d406b4` with 16 regression tests (`_export_dir_error`
`knowledge_tools.py:688`, `_log_duplicate_note_id` `kb_reindex.py:321`). Pushed to
`origin/develop` and shipped; both survived the `571fbc8c` KB-materialisation rework,
which moved note writes to a dedicated KB repo but left `kb_export`'s
workspace-write path — and therefore this guard — in place.

## Cleanup (executed 2026-07-29, owner-approved)

Vault `project-68137e29-jobs`, `2d1b9f1d..2cc5f7c3`:

- deleted the 1811 nested copies that duplicated an existing `knowledge/<name>.md`
- `git mv`'d the 905 nested-only notes to `knowledge/<id>.md`, their canonical location
- removed `archive/kb_index_regenerated_2026-07-06.md/` (1092 files, inert clutter)

`knowledge/` went 2111 → 3016 note files. No `.md`-shaped directories remain anywhere.
No note content was lost. The reindexer's own rename path handled the 905 moves:
`adopt_legacy_row` moves a row when the old path is in the same run's delete set, which
it was.

| | before | after |
| --- | --- | --- |
| rows at a nested path | 957 | **0** |
| duplicate-key errors per reindex | 1811 | **4** |
| `.md`-shaped directories | 2 | **0** |
| notes lost | — | **0** |

## Residual: four duplicate frontmatter ids

The remaining 4 errors are a **different defect**, previously drowned in the 1811, and
now tracked separately as [[kb_duplicate_frontmatter_ids_collide_on_reindex]]. Four
pairs of distinct files carry the same frontmatter `id`, and since `note_fields` takes
`note_id` from frontmatter rather than the filename, both claim one identity:

```
knowledge/iter-4-phase-7-5-blocker-diagnosis.md
knowledge/phase-75-blocker-diagnosis-ac-3ac-4-pdf-prefix-oracles-mathematically-impossible.md
   both:  id: phase-75-blocker-diagnosis-ac-3ac-4-pdf-prefix-oracles-mathematically-impossible
```

The vault holds no duplicate *files* there — every note is at one path — so it is not
this bug and not `kb_export`'s doing. See the split-out issue for all four pairs and
the disposition.

## Not a symptom: the 128 pathless rows

Rows with `path IS NULL` on this project broke down as 114 `archived`, 7 `resolved`,
4 `superseded`, 3 `active`. This is healthy:

- the 114 are `reconcile_orphans` (R-1) already doing its job, dated 07-16 → 07-28;
- the 3 active were written the same day, and none has a file on `main` **or any branch,
  ever** — so adoption can never claim them. They are exactly the case R-1 exists for
  and get archived on the next sweep past the 1 h grace;
- the 11 closed ones are inert: excluded from search by status and from list/read by the
  path guard. `reconcile_orphans` only touches `active`, so they persist harmlessly.

## Verification

Row-level queries. Note `kb_index_watermark.status` was unusable while
[[kb_reindex_watermark_never_advances]] (`f3f01b5d`) was undeployed; that fix has since
shipped too, but the row-level checks remain the honest test:

```sql
-- must be 0
SELECT count(*) FROM knowledge_index
 WHERE project_id = '<project>' AND path LIKE '%.md/%';

-- no note id may appear at two paths
SELECT note_id, count(*) FROM knowledge_index
 WHERE project_id = '<project>' AND path IS NOT NULL
 GROUP BY note_id HAVING count(*) > 1;
```

Confirmed 2026-07-29: nested rows 0, `.md`-shaped trees 0, reindex errors 4 (all from
the duplicate-frontmatter-id residual above).
