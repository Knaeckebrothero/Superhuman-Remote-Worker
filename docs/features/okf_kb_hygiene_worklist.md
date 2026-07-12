# OKF KB — migration-hygiene remediation worklist (task #34)

**Source:** read-only audit of the Better Resavio KB (project `68137e29`) on the
dev cluster, 2026-07-06. Evidence and raw numbers in the memory note
`srw-okf-kb-migration-hygiene-audit` and `docs/features/okf_knowledge_base.md`
§11 (PR4). This file is the **execution tracker** — one item at a time, checked
off as landed.

## Status — 2026-07-12 (FEATURE COMPLETE — trust this over the blocks below)
- **D-3 — DONE (no reindex needed).** Measured the live centroid index read-only
  (port-forward to `srw-pgvector-0`, loop left running, zero mutation): active-note
  centroid coverage is already **97.4 %** (781/802). The running loop churns the
  corpus fast enough that the PR4d centroids are effectively complete, so the
  deferred "full reindex first" precondition turned out unnecessary. Near-dup
  distribution over **300,700** version-matched active pairs: ≥0.99→**2**, ≥0.98→**5**,
  **≥0.97→8**, ≥0.96→26, ≥0.95→50, ≥0.93→173, ≥0.90→685. The two ≥0.99 pairs are true
  slug-suffix/job-id duplicates (`…-334bac.md`, `…-job-968785c8-…`); the 0.97–0.98
  band is sequential archive-finding phase-twins (boilerplate-heavy but distinct
  records). **0.97 sits at the natural precision elbow (26→8→5→2) and is validated —
  keep it.** Lowering to 0.95 triples findings into topical noise; raising to 0.99
  drops near-dupes at 0.97–0.98. Residual: 21 active notes + 5 null-version rows lack
  centroids — self-heals as the loop touches them; not worth racing the active loop
  with a full reindex.
- **C-3 — SKIPPED (cosmetic, by decision).** The no-frontmatter / no-`id` files are
  **not broken**: unlike the C-2 invalid-YAML files (which the reindexer *skips*),
  these index fine — `note_fields` derives the id from the filename stem, the title
  from the first H1, and applies default type/status. Only cost is less-rich
  metadata; no lost or broken notes. Same disposition as B-2.
- **#34 CLOSED.** Every actionable finding is remediated: C-1/D-1/D-2/B-1 deployed,
  R-1 verified (594 ghosts archived), A-1 resolved, C-2 done + adopted, B-2
  auto-retired, D-3 validated, C-3 triaged → skip. #35 (Neo4j-less E2E) closed
  separately.

## Status — 2026-07-11 (superseded by the 07-12 block above)
- **All code fixes deployed & R-1 verified in prod-dev.** C-1/D-1/D-2/B-1 live on
  `sha-ae0cddc`; **R-1** committed (`3d882661`) + deployed (rode `sha-f1f32eb`) and
  **verified live**: 594 pathless ghosts self-archived, loop project down to a
  steady-state handful. **B-2 retired** (R-1 automated it).
- **A-1 — DONE (no edit needed).** Re-scan (07-11) showed the 2 (now 3) resurrection
  files already read `status: superseded`, matching the DB. The loop re-committed
  them correctly since the audit; the divergence was stale. No action taken.
- **C-2 — DONE.** Not 5 but **6** invalid-YAML files (pre-C-1 notes the loop never
  rewrote). Repaired frontmatter (quote `#`/`@`/`:`-bearing flow-seq elements,
  paren-aware split, promote plain-CSV → lists) and committed to the loop repo
  `main @ 551b0a73`. All 6 parse via the real `parse_note_md`; bodies untouched.
  Bonus: 5 of the 6 had **pathless ghost rows** (adoption was blocked by the bad
  YAML) — the fix makes them adoptable, so the next reindex converts ghosts → notes.
- **Remaining:** **C-3** (low-pri file triage — 9 no-frontmatter + 11 no-`id`),
  **D-3** (floor re-tune — needs a full reindex; do deliberately, not during an
  active loop), plus separate task **#35** (Neo4j-less k3d E2E).
- **Ops note:** a memory-heavy in-pod scan OOM-killed an orchestrator pod (HA
  absorbed it). Heavy KB scans now run **locally via port-forward**, never in the
  orchestrator pod. New issue doc: `docs/issues/pod_oom_kill_protection.md`.

## Status — 2026-07-06
- **Shipped + deployed** on dev (`sha-ae0cddc`): **C-1** (`33396baa`), **D-1**, **D-2**,
  **B-1** (+ `get_note_by_slug`) — all four code guards are live.
- **Implemented, uncommitted:** **R-1** (ghost reconciliation pass) — the root-cause
  fix. Spec: `docs/superpowers/specs/2026-07-06-kb-ghost-reconciliation-design.md`.
- **Skipped:** **B-2** (bulk ghost archival) — the code guards make it cosmetic, and
  R-1 automates it on the next clean reindex.
- **Blocked on dev-cluster access** (tunnel down 2026-07-06): **A-1**, **C-2** (live
  loop-repo edits).
- **Low priority / deferred:** **C-3** (file triage), **D-3** (floor tuning — needs
  PR4d deploy + a full reindex).

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
So these are **pre-flip hygiene**, not live-on-dev bugs. The one exception was **C-1**
(the renderer bug) — it silently dropped agent notes from the index on dev; **fixed and
deployed** (`33396baa`, `sha-ae0cddc`). D-1/D-2 (`kb_lint` near-dup) also run live on
dev regardless of the flip, and are deployed too.

---

## The worklist

Legend — **Type:** `code` (my repo, TDD) · `data` (live mutation, needs authz) ·
`deferred` (blocked on a deploy). **Status:** ☐ todo · ◐ in progress · ☑ done.

| # | Item | Type | Status | Depends on |
|---|------|------|--------|-----------|
| C-1 | Renderer: quote `tags`/`keywords` flow-seq elements | code | ☑ | — |
| D-1 | Apply the 0.97 near-dup floor at the `kb_lint` call site | code | ☑ | — |
| D-2 | `find_near_duplicate_pairs`: add `embedding_version` equality guard | code | ☑ | — |
| B-1 | `list_notes`: add `path IS NOT NULL` filter (durable ghost guard) | code | ☑ | — |
| B-2 | Archive the 384 active pathless ghost rows | data | ⊘ skip | R-1 (do once, after) |
| A-1 | Repair 2 resurrection files (`status: superseded`) | data | ☑ resolved 07-11 (files already correct) | — |
| C-2 | Backfill the invalid-YAML files (6, not 5) | data | ☑ done 07-11 (`main @ 551b0a73`) | C-1 landed+deployed |
| C-3 | Triage 8 missing-frontmatter + 8 no-`id` files | data | ⊘ skip 07-12 (index fine, cosmetic) | — |
| D-3 | Final near-dup floor tuning against the centroid index | deferred | ☑ 07-12 (0.97 validated, no reindex; 97.4% covered) | PR4d deploy + reindex |
| R-1 | Ghost reconciliation pass (root cause) | code | ☑ | — |

---

## Item detail

### C-1 — Renderer YAML safety  ·  code · ☑ **DONE (committed `33396baa`)**
Committed on `develop` in `33396baa` (bundled with unrelated Packer changes):
both flow-seqs now emit
`', '.join(_yaml_quote(x) for x in …)`. RED test
`test_roundtrips_tags_keywords_with_yaml_flow_breakers` (renders `@dataclass(...)`,
`a: b`, `[nested]`, `#1` → round-trips through `parse_note_md`) failed on the `@`
scalar, passes after the fix. Existing `test_emits_tags_keywords_confidence_and_superseded_by`
updated to the quoted form. Full gardener+tools+reindex suites green (201), ruff clean.

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

### D-1 — Apply the 0.97 floor  ·  code · ☑ **DONE (deployed `sha-ae0cddc`)**
The 07-05 decision (raise 0.9→0.97) was **never applied**: `kb_lint`
(`knowledge_tools.py:1422`) calls `find_near_duplicate_pairs(project_id)` with no
`min_similarity`, so it uses the store default `0.9`. Live floor read confirms
0.97 (7 pairs, tractable) vs 0.90 (307, noise).
- **Fix:** pass `min_similarity=0.97` from the `kb_lint` call site (leave the store
  default as the documented library default, or bump both — decide when
  implementing). Prefer the call site so the *lint policy* owns the floor.
- **Test:** `kb_lint` invokes the store with `0.97`.

### D-2 — Version guard on the self-join  ·  code · ☑ **DONE (deployed `sha-ae0cddc`)**
`find_near_duplicate_pairs` (`src/services/knowledge_store.py:1082`) filters
`status='active'` + `embedding IS NOT NULL` but **not `embedding_version`** → it
cosine-compares vectors from different embedding models (ghost `null-version` vs
`qwen3…c1`). Meaningless similarities.
- **Fix:** add `AND a.embedding_version = b.embedding_version` (and, optionally, a
  param to scope to the query-time version). Keeps the fn byte-compatible otherwise.
- **Test:** two rows with differing `embedding_version` are never returned as a pair.

### B-1 — `list_notes` (+ `get_note_by_slug`) path filter  ·  code · ☑ **DONE (deployed `sha-ae0cddc`)**
`list_notes` (`knowledge_store.py:859`) filtered only `kb_id` → after the kg-less
flip, `kb_list` would return all 396 active ghosts. **Decision resolved:**
`get_note_by_slug` (the `kb_read` backend) got the same guard — a direct read of a
slug that only exists as a pathless ghost would otherwise resolve it. Both now carry
`AND path IS NOT NULL`; status stays unfiltered so superseded/archived *files* still
read.
- **Fix:** add `AND path IS NOT NULL` to the `list_notes` WHERE. Files-canonical:
  the store lists what a file backs. Durable — handles future ghosts regardless of
  B-2/R-1.
- **Test:** a pathless active row is excluded from `list_notes`; a file-backed row
  of every status is included.

### B-2 — Archive the ghosts  ·  data · **SKIPPED for now (2026-07-06)**
384 active pathless rows (520 across all statuses; count fluctuates with loop
activity), **0 matching any live file** → pure orphans. **Decision: skip.** With
D-2 (version guard) and B-1 (path filter) now *deployed*, the ghosts are already
neutralized in code — invisible to chunk search, the near-dup self-join, `kb_list`,
and `kb_read`. Archiving is now cosmetic DB honesty only, and it **recurs until R-1**
closes the DELETE gap. Left as a one-liner we can run anytime; not worth a live
mutation today. Op preserved below for when R-1 lands (do it once, after the gap
is closed, so it stays clean).
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

### C-3 — Triage 8 missing-frontmatter + 8 no-`id` files  ·  data · ⊘ **SKIPPED 07-12 (cosmetic)**
**Premise correction:** these files are **not** unindexable. Verified against the
live reindexer: `parse_note_md` returns `(None, text)` for a no-frontmatter file (it
only *raises* on a *present-but-invalid* YAML block — that's the C-2 class), and
`note_fields` (`kb_reindex.py:147`) does `fm = fm or {}`, deriving the `id` from the
filename stem, the title from the first H1, and safe `type`/`status` defaults. So the
no-frontmatter / no-`id` files index fine with derived metadata — the only cost is
less-rich fields (no tags/type), never a lost or skipped note. `index.md`/`log.md`
are reserved and correctly excluded. **Disposition: skip** — same as B-2; not worth a
live loop-repo edit for a cosmetic metadata backfill.

### D-3 — Final near-dup floor tuning  ·  deferred → ☑ **DONE 07-12 (0.97 validated, no reindex)**
Resolved by a **read-only** measurement of the live centroid index (port-forward to
`srw-pgvector-0`, loop untouched) — the deferred "full reindex first" precondition
proved unnecessary because centroid coverage is already **97.4 %** (781/802 active
notes; the running loop keeps them fresh). Version-matched self-join over 300,700
active pairs: ≥0.99→2, ≥0.98→5, **≥0.97→8**, ≥0.96→26, ≥0.95→50, ≥0.93→173, ≥0.90→685.
Top-of-distribution eyeball: the 2 pairs ≥0.99 are true slug-suffix/job-id duplicates;
0.97–0.98 are sequential archive-finding phase-twins (boilerplate-heavy, distinct).
**0.97 sits at the precision elbow (26→8→5→2) and is confirmed — no change.** 0.95
triples findings into topical noise; 0.99 misses the 0.97–0.98 near-dupes. Residual
21 active notes + 5 null-version rows lack centroids; self-heals as the loop touches
them. Query: session scratchpad `d3_measure.sql`.

### R-1 — Ghost reconciliation  ·  code · ☑ **DONE (uncommitted)**
Reframed by the design as an **adoption/reconciliation gap** (not a delete gap): the
agent write-through (`upsert_note`) is born pathless; the reindexer adopts it
(slug-keyed) once the file lands; un-adopted rows are invisible to the path-keyed
delete. Spec: `docs/superpowers/specs/2026-07-06-kb-ghost-reconciliation-design.md`.
- **Fix (implemented):** new `KnowledgeStore.reconcile_orphans(project_id, tree_slugs,
  grace=1h)` — soft-archives pathless active rows whose slug is absent from the tree
  and past the adoption grace. Keyed on `project_id` (ghosts have `kb_id` NULL). Called
  once per reindex after the delete loop, non-fatally; `reconciled` surfaced in the
  summary + log. TDD: 2 store + 3 reindexer tests, RED→GREEN; 290 pass, ruff clean.
- **Rejected (scope):** path-at-birth (fights the intentional provisional pattern) and
  a dedicated "prompt tool-delete" (no delete tool; `kb_update` retire already
  dual-writes status; self-heals via adoption). See spec §Non-goals.
- **Makes B-2 automatic:** on the first clean reindex after deploy, the 384 existing
  ghosts archive themselves.

---

## Remaining work
Code side is **complete**: C-1/D-1/D-2/B-1 deployed (`sha-ae0cddc`); R-1 implemented
(uncommitted). What's left:

1. **Commit R-1** (store method + reindexer pass + spec) on `develop`, then push/deploy
   so the reconciliation pass goes live and the 384 ghosts self-archive.
2. **A-1 / C-2** (live loop-repo edits) — **blocked** on dev-cluster access (tunnel down
   2026-07-06). Per-item authz, prefer `kb_update` / a quiet window over a raw commit.
3. **C-3** (file triage) — low priority, whenever.
4. **D-3** (confirm the 0.97 floor) — **deferred** until PR4d is deployed and a full
   reindex has written centroids; record the result in `okf_knowledge_base.md` §11.1.
5. **B-2** — retired: R-1 makes the bulk archival automatic. The one-liner op above
   stays only as a manual fallback.

### History (done)
- **C-1** renderer YAML safety → `33396baa`.
- **D-1 / D-2 / B-1** (+ `get_note_by_slug`) code batch → `ae0cddc9`, deployed
  `sha-ae0cddc`.
- **R-1** ghost reconciliation → implemented, spec written, tests green (uncommitted).
