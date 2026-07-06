# OKF KB — migration-hygiene remediation worklist (task #34)

**Source:** read-only audit of the Better Resavio KB (project `68137e29`) on the
dev cluster, 2026-07-06. Evidence and raw numbers in the memory note
`srw-okf-kb-migration-hygiene-audit` and `docs/features/okf_knowledge_base.md`
§11 (PR4). This file is the **execution tracker** — one item at a time, checked
off as landed. Nothing here is committed yet.

## Standing rules for this work
- Work on `develop`, no feature branches. Commit only when explicitly authorized;
  never push without asking.
- Code fixes are strict TDD (failing test first).
- Live mutations (dev vector DB / the actively-running loop repo) need explicit
  per-item authorization. The loop is *writing to that repo continuously* — prefer
  the `kb_update` tool (clean dual-write) or a quiet inter-iteration window over a
  raw hand-commit that could race the loop.

## Why most of this is not urgent
The whole kg-less read/write surface (where ghosts and resurrection actually bite)
is **dormant on every Neo4j-enabled deployment** — it only activates when a
deployment sets `databases.neo4j.enabled=false` (the #35 E2E, or an OSS self-host).
So these are **pre-flip hygiene**, not live-on-dev bugs. Exception: **C-1** (the
renderer bug) *is* live now — it silently drops agent notes from the index and
re-warns every sweep on dev today.

---

## The worklist

Legend — **Type:** `code` (my repo, TDD) · `data` (live mutation, needs authz) ·
`deferred` (blocked on a deploy). **Status:** ☐ todo · ◐ in progress · ☑ done.

| # | Item | Type | Status | Depends on |
|---|------|------|--------|-----------|
| C-1 | Renderer: quote `tags`/`keywords` flow-seq elements | code | ☐ | — |
| D-1 | Apply the 0.97 near-dup floor at the `kb_lint` call site | code | ☐ | — |
| D-2 | `find_near_duplicate_pairs`: add `embedding_version` equality guard | code | ☐ | — |
| B-1 | `list_notes`: add `path IS NOT NULL` filter (durable ghost guard) | code | ☐ | — |
| B-2 | Archive the 396 active pathless ghost rows | data | ☐ | — |
| A-1 | Repair 2 resurrection files (`status: superseded`) | data | ☐ | — |
| C-2 | Backfill the 5 invalid-YAML files | data | ☐ | C-1 landed+deployed |
| C-3 | Triage 8 missing-frontmatter + 8 no-`id` files | data | ☐ | — |
| D-3 | Final near-dup floor tuning against the centroid index | deferred | ☐ | PR4d deploy + reindex |
| R-1 | Close the DELETE dual-write gap (root cause of ghosts) | code | ☐ | design decision |

---

## Item detail

### C-1 — Renderer YAML safety  ·  code · **live bug, do first**
`_render_note_md` (`src/tools/knowledge/knowledge_tools.py:260,262`) emits
`tags: [{', '.join(...)}]` / `keywords: [...]` **unquoted**. Agent keyword strings
contain flow-breakers (`@dataclass(frozen=True, …)`, `pick-first proposal #1`,
`config.py:`) → the file is unparseable → the reindexer **skips it and re-warns
every 15-min sweep, forever** (5 such files today, more accruing).
- **Fix:** quote each element with the existing `_yaml_quote` (always double-quotes
  → valid flow scalar): `', '.join(_yaml_quote(t) for t in note['tags'])`, same for
  keywords.
- **Test (RED first):** render a note whose keywords include `@x`, `a: b`, `c, d`,
  `[e]`; assert the emitted frontmatter round-trips through `yaml.safe_load` /
  `gardener.parse_note` and the list is recovered intact.
- **Blast radius:** write path only; no schema change. Neo4j-agnostic.

### D-1 — Apply the 0.97 floor  ·  code
The 07-05 decision (raise 0.9→0.97) was **never applied**: `kb_lint`
(`knowledge_tools.py:1422`) calls `find_near_duplicate_pairs(project_id)` with no
`min_similarity`, so it uses the store default `0.9`. Live floor read confirms
0.97 (7 pairs, tractable) vs 0.90 (307, noise).
- **Fix:** pass `min_similarity=0.97` from the `kb_lint` call site (leave the store
  default as the documented library default, or bump both — decide when
  implementing). Prefer the call site so the *lint policy* owns the floor.
- **Test:** `kb_lint` invokes the store with `0.97`.

### D-2 — Version guard on the self-join  ·  code
`find_near_duplicate_pairs` (`src/services/knowledge_store.py:1082`) filters
`status='active'` + `embedding IS NOT NULL` but **not `embedding_version`** → it
cosine-compares vectors from different embedding models (ghost `null-version` vs
`qwen3…c1`). Meaningless similarities.
- **Fix:** add `AND a.embedding_version = b.embedding_version` (and, optionally, a
  param to scope to the query-time version). Keeps the fn byte-compatible otherwise.
- **Test:** two rows with differing `embedding_version` are never returned as a pair.

### B-1 — `list_notes` path filter  ·  code
`list_notes` (`knowledge_store.py:859`) filters only `kb_id` → after the kg-less
flip, `kb_list` would return all 396 active ghosts. (`get_note_by_slug` keys on
`(kb_id, note_id)` and is a much smaller surface — decide whether it needs the same
guard; leaning yes for symmetry.)
- **Fix:** add `AND path IS NOT NULL` to the `list_notes` WHERE. Files-canonical:
  the store lists what a file backs. Durable — handles future ghosts regardless of
  B-2/R-1.
- **Test:** a pathless active row is excluded from `list_notes`; a file-backed row
  of every status is included.

### B-2 — Archive the ghosts  ·  data · needs authz
396 active pathless rows (532 across all statuses), **0 matching any live file** →
pure orphans. Invisible to chunk search; pollute the near-dup self-join; would flood
kg-less `kb_list` (mitigated by B-1). Archiving de-pollutes the self-join and
**unblocks D-3**.
- **Op (reversible, matches the doc's language):**
  `UPDATE knowledge_index SET status='archived', invalidated_at=now()
   WHERE project_id='68137e29-…' AND path IS NULL AND status='active';`
- **Decision:** archive (reversible) vs delete (they're dead weight: no file, no
  chunks, stale vectors). With B-1 in place, archival's only remaining value is the
  near-dup de-pollution → could also just skip and rely on D-2's version guard.

### A-1 — Repair 2 resurrection files  ·  data · needs authz
`proposal-iter-19-001-…`, `proposal-iter-19-002-…`: file says `active`, DB says
`superseded`. On next reindex the file wins → they resurrect. Fix must be on the
**file** (reindexer treats files as truth).
- **Op:** set `status: superseded` (+ `superseded_by` if recoverable) in the two
  files. Prefer `kb_update` over a raw commit to avoid racing the loop.

### C-2 — Backfill 5 invalid-YAML files  ·  data · needs authz · **after C-1**
Repair the 5 existing unparseable files so they reindex and stop re-warning.
Sequence after C-1 is landed+deployed, else a curator re-write could re-break them.
- **Op:** rewrite each frontmatter with quoted keywords (hand-fix or re-emit via the
  fixed renderer). The 5 slugs are in the audit output / memory note.

### C-3 — Triage 8 missing-frontmatter + 8 no-`id` files  ·  data · low priority
Also unindexable (reindexer needs `id`/`type`). Some may be intentional
(`index.md` is reserved and correctly excluded). Triage: real notes get frontmatter;
genuine non-notes get moved/removed.

### D-3 — Final near-dup floor tuning  ·  deferred (PR4d deploy + reindex)
After PR4d ships and a full reindex writes centroids, re-run the self-join
(ghosts archived, version-guarded) and confirm 0.97. Record the outcome in
`okf_knowledge_base.md` §11.1. Preliminary read already supports 0.97.

### R-1 — Close the DELETE dual-write gap  ·  code · root cause, needs design
Ghosts accrue because pathless rows are invisible to the reindexer's path-keyed
tree-diff, and note deletes never propagate to the DB. Without this, B-2 is a
recurring chore. Options to weigh: reindexer archives rows whose backing file
vanished; or `kb_delete` (+ curator supersede) propagates to the row. Bigger change
— scope separately once the smaller items land.

---

## Recommended sequence
1. **C-1** (live bug — stops new index dropouts) → verify green.
2. **D-1 + D-2 + B-1** (cheap, independent code fixes) → one commit or three.
3. Commit the code batch when authorized.
4. **B-2 / A-1 / C-2** (live mutations) — per-item authz, prefer tool-driven writes.
5. **D-3** after the next deploy + reindex.
6. **R-1** as a follow-up design item so ghosts stop recurring.
