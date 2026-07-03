---
tags:
  - knowledge-management
  - data-management
  - agent-architecture
  - git-integration
  - tool-development
status: draft
created: 2026-07-03
aliases:
  - OKF knowledge base
  - markdown knowledge base
  - files-canonical KB
  - KB as datasource
related:
  - "[[knowledge_base_substrate_decision]]"
  - "[[obsidian]]"
  - "[[project_knowledge_base]]"
  - "[[repo_datasource]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[kb_convergence_ttl_reverification]]"
  - "[[auxiliary]]"
  - "[[agent_memory_overhaul]]"
---

# OKF Knowledge Base — Files-Canonical KB as a Datasource

**Status:** DRAFT, delivery pipeline LIVE — origin: design discussion 2026-07-03,
building on the substrate findings in [[knowledge_base_substrate_decision]].
[[loop_repo_compounding_v2]] shipped the same day, which changes this doc's footing: the
squash-merge flow, the post-merge hook point, and `retros/` (orchestrator-written notes
with OKF frontmatter, `type: retro`) are **in production**. Anything an agent writes
under `knowledge/` on its job branch already reaches `main` with zero further
orchestrator work — the loop is a ready delivery pipeline waiting for the notes.

## TL;DR

- **A knowledge base = an OKF/markdown git repo + a disposable Postgres index + a
  toolset.** Files are canonical; the index is a rebuildable cache over git; tools enforce
  at write time what a filesystem can't.
- **KBs become datasources.** A KB datasource is a repository datasource with OKF
  conventions, attachable to any number of projects (org wiki, a customer handoff vault,
  this repo's own `docs/`). The *project KB* stops being a special Neo4j subsystem and
  becomes an auto-provisioned KB datasource attached at project creation — one mechanism,
  and cross-project knowledge sharing falls out for free.
- **Access is split by operation, not by "files vs. tools":** reads go straight at the
  cloned filesystem; writes go through `kb_*` tools that enforce integrity; queries go
  through the index; maintenance is a background gardener, because a year of running our
  own `docs/` vault proved the format does not self-maintain.
- **Sync is one-way and incremental**: the index stores the commit SHA it was built from;
  `git diff` against HEAD yields exactly the changed notes (git's object model already *is*
  the Merkle tree you'd otherwise build). No bidirectional sync, ever.
- This answers [[knowledge_base_substrate_decision]] §8 for the KB workload: **no graph
  scaffolding; Neo4j goes dormant for KBs** (the citation network remains the separate
  open case). It flips the substrate doc's default from "Postgres-canonical ⇄ OKF export"
  to "OKF-canonical ⇄ Postgres index" — §5 below argues why.

---

## 1. Lineage — three docs converging

| Doc | Model | What it got right | Why it didn't stick |
|---|---|---|---|
| [[obsidian]] | Files-first: `knowledge/` vault in the jobs repo is canonical, notes merge to `main` per job, Neo4j is a synced mirror | The vision this doc restores: git-versioned, human-editable, per-job merge flow | Superseded before implementation by the doc below |
| [[project_knowledge_base]] | Inverted: "Neo4j is Source of Truth, pgvector is a Search Index"; markdown demoted to export | The curator, retrieval pipeline, note taxonomy — all kept | Implemented; but the git/merge mechanics vanished with the flip, and Neo4j turned out to serve zero graph queries |
| [[knowledge_base_substrate_decision]] | Re-opened empirically: Neo4j does no graph work; proposes Postgres-canonical + OKF export; "Lite tier" = pure markdown | The evidence, the three-roles split (truth/retrieval/interchange), the §3 filesystem pains, the Appendix B schema | Leaves §8 open; its default keeps truth in the DB |
| [[repo_datasource]] | Datasource design whose *first motivating use case* is "an Obsidian vault in a git repo as persistent agent memory across jobs" | The datasource framing this doc generalizes | Repo datasources shipped; the KB specialization never did |

This doc is the synthesis: **[[obsidian]]'s file-first model, with Postgres instead of
Neo4j as the mirror, delivered through [[repo_datasource]]'s attachment mechanics, with
the integrity pains of [[knowledge_base_substrate_decision]] §3 answered by tool-mediated
writes instead of DB-canonical storage.**

## 2. The reframe

```
KB  =  OKF git repo (canonical)  +  Postgres index (disposable)  +  kb_* toolset
```

Consequences:

- **KB datasource** — a new datasource type wrapping a git repo (clone/credential plumbing
  shared with `repository`), flagged as OKF-conventioned so the `kb_*` tools bind to it.
- **Project KB = a KB datasource auto-provisioned and auto-attached at project creation**,
  exactly like the jobs repo and the default project's home Space. Deleting the special
  case means: same tools, same index, same maintenance loop whether the KB is
  project-private or an org-wide vault attached to 40 projects.
- **The loop's blackboard is the project's KB datasource.** The per-job-branch +
  squash-merge mechanics live in [[loop_repo_compounding_v2]]; scholar/critic KB notes and
  the retro collection are just notes merging to the KB's `main`.
- **Interchange is free.** The repo *is* the OKF interchange artifact — handing a customer
  their knowledge base is `git clone`. No export step (the export *is* step zero of
  migration, see §9).

## 3. Access model — split by operation

The "clone-and-grep vs. abstraction layer" question dissolves when split per operation:

| Operation | Layer | Rationale |
|---|---|---|
| **Read / navigate** | Plain filesystem on the clone | Agents are natively strong at grep/read/glob over a repo; zero new tools; works in the Lite tier; humans use Obsidian on the same clone. Don't abstract what already works. |
| **Write** | `kb_write` / `kb_update` (tool-mediated) | Where [[knowledge_base_substrate_decision]] §3's pains actually live. The tool enforces unique id, valid frontmatter, resolvable links, supersede semantics — then writes the `.md` and updates the index in one step. |
| **Query** | `kb_query` / `search_knowledge` (index-backed) | Semantic search, `status != 'superseded'` filtering, anti-joins ("unanswered questions"), backlinks, tag algebra — the Appendix B schema of the substrate doc, nearly verbatim. |
| **Maintenance** | Gardener utilities (background / curator), not inline tools | Lint (orphans, broken links, missing/invalid frontmatter, duplicate candidates), hierarchical `index.md` regeneration (OKF progressive disclosure), TTL re-verification sweeps ([[kb_convergence_ttl_reverification]]), stats. |

**The gardener redeems the curator.** Today the curator's only verb is "write more notes,"
which produced the KB noise findings (F33/F38 in [[loop_review]]). Given lint/dedup/index
utilities, the curator becomes the maintenance loop the format needs — our own `docs/`
vault (417 files, 203 without frontmatter, a tag taxonomy frozen at "48 documents
analyzed") is the existence proof that an LLM *authoring* markdown does not keep a vault
coherent; only a loop that *runs* does.

> **Curator refactoring (2026-07-03): make it a proper auxiliary, modeled on the memory
> auxiliaries.** The subjob→auxiliary migration already happened ([[auxiliary]]:
> `CurateKnowledgeTask`, agent-mode, fired from `archive_phase`) — but it stopped at
> "one ungated task": extract from phase artifacts, `kb_write`, done. The memory side
> ([[agent_memory_overhaul]]) evolved the same starting point into a **pipeline**:
> extraction → **ingestion verdict** — each candidate is compared against its nearest
> existing entries and an auxiliary decides *add / update / supersede / discard*
> (`IngestionVerdictService`, `src/services/memory/`) — with event-driven triggers and
> config-driven prompts read at event time. The curator's ungated write path is precisely
> where F33/F38 noise enters (reconstructed deliverables, double-written proposals,
> immortal learning/retro notes). The refactor: mirror the memory pipeline for KB writes
> (curation candidates pass a verdict gate against `search_knowledge` neighbors before
> any `kb_write`/`kb_update`), give the curator the gardener verbs (`kb_lint`,
> `kb_index`, dedup, TTL sweep) as its tool surface, and hang it on the same event points
> the memory auxiliaries use (archive phase; post-merge once slice 3's index exists).
> Same package shape as `src/services/memory/` — a `src/services/knowledge/` service
> package, not more logic inside the task class.

## 4. The core commitment — the index is a cache, never a source of truth

**Postgres is a disposable index over git.** Files are canonical; the index is rebuildable
from `main` HEAD at any time; sync is strictly one-way (push/merge → reindex).

What this buys:

- **Bidirectional sync never exists.** The classic failure mode of dual-representation
  designs (including [[obsidian]] v1's open "conflict resolution" questions) is gone by
  construction. A human pushing from Obsidian is just another commit; the reindex picks it
  up; the linter *surfaces* convention violations it could not prevent.
- **Integrity moves to the write path, set algebra moves to the index.** The §3 pains of
  the substrate doc are arguments about *where enforcement happens*, not where truth must
  live: uniqueness → refused by `kb_write` (and flagged by lint for out-of-band edits);
  atomicity → one note = one file = one commit (multi-note updates = one commit);
  `WHERE NOT IN` → the index.
- **The degenerate failure case is "rebuild the cache."** Index corruption, schema
  migration, force-push — all resolve to a full reindex from HEAD.

**Why file-first rather than the substrate doc's DB-first default:** datasource KBs force
it. An org wiki that humans edit in Obsidian and foreign agents clone cannot be an "export
skin" over our database — the repo is the shared surface, so the repo must be the truth.
Since datasource KBs must be file-first, making the project KB file-first too yields one
model everywhere instead of two. The honest residual cost vs. DB-first: a *human's*
out-of-band duplicate/malformed note is detected at lint time, not refused at write time.

## 5. Incremental indexing — git is the Merkle tree

The staleness/cost concern has a clean answer: **git's object model already is the
level-by-level tree hashing you would otherwise build.** Tree object SHAs are recursive
folder hashes; the commit SHA is the root hash; `git diff --name-status A..B` is "compare
roots, descend only into changed subtrees," maintained by git on every push.

Design:

- The index stores **one watermark per KB: `indexed_commit`** (plus `blob_sha` per note
  row for idempotency and embedding gating).
- **Reindex** = `git diff --name-status indexed_commit..HEAD` (Gitea `get_compare` API —
  already on `GiteaClient`) → upsert added/modified notes, delete removed ones, re-embed
  only rows whose blob SHA changed → advance the watermark. Cost is **O(changed files)**,
  never O(vault): a 10k-document org vault with 500 changed notes touches 500 files and
  re-embeds only the content-changed subset.
- **Staleness detection is O(1)**: compare `indexed_commit` to the branch HEAD SHA.
- **Triggers** (cheap enough to be aggressive): Gitea push webhook → reindex job; job-start
  check (SHA compare, catch-up reindex before the agent reads); the loop's post-merge hook
  — now a concrete call site: `_advance_project_loop` directly after
  `merge_loop_job_branch` returns `merged` ([[loop_repo_compounding_v2]], implemented). Between triggers, reads-of-truth can always hit files.
- **Force-push / history rewrite**: if `indexed_commit` is no longer an ancestor of HEAD
  (merge-base check), fall back to full rebuild — safe because the index is disposable.
- **Interrupted reindex self-heals**: per-row `blob_sha` makes re-runs skip already-current
  rows; the watermark only advances at the end.

## 6. Note identity — the one deliberate OKF deviation

OKF says *file path = identity*. Renames break that: links, supersede chains, and the
index PK need a stable reference, and `git diff -M` can only tell you a rename happened.

**Proposal:** a stable `id` slug in frontmatter is the primary identity; the path is the
note's *location*. The index maintains the `path ↔ id` mapping; `kb_write` generates
`id` from the initial path-slug (so the common case is OKF-compatible: id == path-derived
slug) and enforces uniqueness per KB; renames keep the id and update the location.
Wikilinks/relative links keep working via the mapping; lint flags links that resolve by
neither path nor id.

This is frontmatter-only (OKF tolerates extra fields), and it is the price of making
supersede/provenance chains survive reorganization.

## 7. Toolset

Signature-stable evolution of the existing tools where possible (the substrate doc's
"backend swap, not UX change" principle), plus the new maintenance surface:

| Tool | Op | Notes |
|---|---|---|
| `kb_write` | write | Creates a note: validates frontmatter (OKF `type` required), enforces id uniqueness, resolves links, writes `.md` + updates index + embedding in one step, commits. |
| `kb_update` | write | Edit + supersede semantics (`status: superseded`, `superseded_by`), bi-temporal columns in the index if wanted (RecallStore parity). |
| `search_knowledge` | query | Semantic + lexical over the index (pgvector), status-filtered by default. Unchanged signature. |
| `kb_query` | query | Structured: by type/tag/status, backlinks, unanswered questions, contradictions — the Appendix B SQL surface. |
| `kb_lint` | maintenance | Orphans, dead links, missing/invalid frontmatter, duplicate-id and near-duplicate-content candidates. Gardener/curator-facing; also runnable as a merge gate. |
| `kb_index` | maintenance | Regenerate hierarchical `index.md` per directory (OKF progressive disclosure — fixes the flat-index budget blowout seen in both `docs/` and MEMORY.md). |

Reads need no tools: the KB is a cloned directory in the workspace.

## 8. Tiers — one toolset, one knob

| Tier | Substrate | What degrades |
|---|---|---|
| **Full** (default) | OKF repo + Postgres index | Nothing — this doc. |
| **Lite** | OKF repo only | `search_knowledge`/`kb_query` unavailable (or degrade to grep); writes still tool-validated; lint still runs. Zero-infra / air-gapped / human-primary vaults. |
| **Graph** (opt-in) | + Neo4j | Neo4j as a **second derived index** beside Postgres — rebuilt from the repo, one-way sync off the same commit watermark (§5), disposable. Buys graph-backed `kb_related`/`kb_provenance`/`kb_contradictions` (multi-hop traversal); with it off, those degrade to 1-hop link-table queries on the index. Reserved for a genuinely graph-shaped workload (citation network); per [[knowledge_base_substrate_decision]] §8: only with the router + text-to-Cypher scaffolding investment. |

This resolves §8 of the substrate doc for the KB workload: **no** — no graph investment
for KBs; Neo4j goes dormant there. The hard rule (resolved 2026-07-03): **Neo4j is never
canonical again, not even in the Graph tier.** Enabling it adds a derived view; it never
replaces the markdowns. A DB-canonical KB cannot serve a vault that humans edit in
Obsidian and foreign agents clone — letting Neo4j be truth again would reintroduce
exactly the split-brain this design exists to kill.

## 9. Migration & compatibility

1. **Export once**: `get_all_notes_for_export` (the ~half-built Obsidian export) becomes
   the one-time Neo4j → OKF migration per project: wikilinks stay, add `type:` + `id:`
   frontmatter, write into the project's KB repo, initial index build.
2. **Tool cutover**: `kb_write`/`search_knowledge` signatures survive; the backend swaps.
   The curator gains the maintenance verbs.
3. **RecallStore semantics carry over**: supersede/TTL/verification state lives in
   frontmatter (`status`, `remaining_cycles`, `last_verified_cycle` — fixing the F39 gap)
   and is *indexed* for filtering; the bi-temporal columns are optional index-side parity.
4. **Neo4j coexistence & decommission (audited 2026-07-03)** — side-by-side is the plan,
   not an option; there is no cutover day:
   - **Migration risk is edges-only.** `kb_write` already dual-writes the full note body
     into pgvector `knowledge_index` (`src/tools/knowledge/knowledge_tools.py:203-236`,
     Neo4j-first then upsert) — the only data living *solely* in Neo4j is the
     relationship edge set, and the exporter serializes exactly that (grouped wikilinks).
     The chart's Neo4j PVC is `keep`-policy; disabling the StatefulSet destroys nothing.
   - **The helm toggle exists but is a trap today**: `databases.neo4j.enabled: false` is
     supported (`helm/templates/databases/neo4j.yaml:1`), but `create_kb_tools`
     hard-raises without a graph and the registry then skips the entire knowledge
     category (`src/tools/registry.py:516-520`) — an agent on a Neo4j-less deployment
     silently loses every `kb_*` tool. **Slice 3 is what makes the existing toggle
     real.**
   - **Per-deployment, at leisure**: slices 1–2 don't touch Neo4j; slice 3 flips
     retrieval to the index; slice 4 migrates pre-existing notes. Each deployment
     disables Neo4j whenever *its* projects are migrated — or never (Graph tier).
   - **Untouched by all of this**: the `neo4j` *datasource type* and its
     `cypher_query`/`cypher_execute` tools (users' own graph DBs at whatever URL they
     provide — no in-cluster instance needed), and every other subsystem — citations and
     memory are already pgvector. The system KB is the in-cluster Neo4j's only workload.

## 10. Concurrency & limits (honest costs)

- **Within a job**: `kb_write` enforces id uniqueness against index + working tree.
- **Across parallel jobs**: two branches can create the same id; the merge is where it
  surfaces. The sequential loop is conflict-free by construction
  ([[loop_repo_compounding_v2]]); [[loop_parallel_execution]] would need an id-claim step
  or merge-time lint gate.
- **Human out-of-band edits**: detected (lint), not prevented — the file-first price (§4).
- **Scale ceiling**: files + O(changed) reindex comfortably cover org-wiki scale (10⁴–10⁵
  notes). This is not a planet-scale design and doesn't try to be.
- **Embedding cost** is the only real reindex cost and is gated on content-blob change.

## 11. Slices (independently shippable — reordered 2026-07-03 after v2 shipped)

0. **Loop delivery pipeline** — DONE via [[loop_repo_compounding_v2]]: per-job branches,
   squash-merge to `main`, post-merge hook point, `retros/` as the first OKF note family.
1. **Dual-write** (the strangler-fig first step): `kb_write`/`kb_update` additionally
   materialize each note as `knowledge/<type>/<slug>.md` in the workspace — **via the
   workspace backend, never local I/O** (files live on the remote workspace pod/VM; the
   citation engine's stub-mode bug is the cautionary tale). The DB stays the retrieval
   substrate; the v2 merge delivers the files to `main`. Agent-side only, no schema, no
   orchestrator change. An overnight loop run is the natural E2E.
2. **OKF lint/gardener toolset + curator-as-proper-auxiliary** — `kb_lint`/`kb_index`;
   useful against today's `repository` datasources (point it at a vault — or this repo's
   `docs/`). Includes the curator refactor (§3 note): verdict-gated writes mirroring the
   memory ingestion pipeline, gardener verbs as its tool surface, `src/services/knowledge/`
   package shape.
3. **Postgres index + query tools + retrieval cutover** — Appendix B schema +
   watermark/incremental sync; `search_knowledge` backend swap; DB write path retired
   (files become canonical, completing the strangler fig).
4. **KB datasource type + auto-attach as project KB** — the unification; Neo4j export
   migration for pre-existing notes. Mechanics (audited 2026-07-03 — most of the
   substrate is already live):
   - **Clone plumbing exists**: `repository` datasources are cloned to `repos/<name>/`
     on the workspace backend at dispatch (`src/core/datasource_setup.py:659`). The KB
     type adds only the OKF binding (`kb_*` tools take the root), index provisioning +
     watermark reindex, and gardener coverage.
   - **Org-wide vaults are nearly free**: `datasources.is_global` already means
     "visible to all users" and `list_eligible_datasources` already returns
     owned + global + project-linked — an org wiki is a KB datasource with
     `is_global: true`. No new sharing machinery.
   - **Lite/backend tiers**: repository datasources currently *reject* shell-less
     workspace backends; a KB datasource should instead degrade to query-only
     (index-backed `search_knowledge`/`kb_query`, no clone, no filesystem reads).

## Open questions

- **Datasource shape**: new `kb` type vs. `repository` + `format: okf` flag. Leaning new
  type (UX + tool-gating clarity), sharing the repository clone/credential plumbing.
- **Org-vault write policy**: `is_global` solves *visibility*, not authorship. Leaning:
  global KB datasources are read-only by default except owner/admin (the `read_only`
  flag exists on the project link table, but a global KB reachable via the job picker
  without a project link needs its own answer). Decide when slice 4 lands.
- **Where the default project KB lives** — RESOLVED 2026-07-03 by v2 shipping: the jobs
  repo is already the coordination repo and `retros/` already lives on its `main`, so the
  default project KB is `knowledge/` in the jobs repo. The general shape: **a KB root =
  a (repo, path-prefix) pair** — the toolset takes a root, so standalone KB-datasource
  repos (root = repo top level) are the same thing to every tool. No entangling: the KB
  is *content* on the coordination repo, not a coupling to it.
- **Merge gate** — RESOLVED: merge-then-flag, matching v2's implemented merge-failure
  posture (flag + continue; loss visible, never silent). The gardener sweeps.
- **Embedding locality**: index/embeddings per-KB or shared pgvector tables with
  `kb_id` scoping (leaning shared tables, consistent with current pgvector usage).
- **Note volume**: dual-write makes today's KB noise (F33/F38 — 374 notes in one run-6
  night) *visible* as files on `main`. That's a feature (you can finally see and prune
  it), but expect a fat `knowledge/` tree until the gardener (slice 2) ships. Loop jobs
  run bare (no curator), so loop-night volume is agent-authored only.

## Related

- [[knowledge_base_substrate_decision]] — the evidence base; this doc answers its §8 for
  the KB workload and flips its default tier to files-canonical.
- [[obsidian]] — the original files-first vision this restores (with Postgres, not Neo4j,
  as the mirror).
- [[project_knowledge_base]] — the implemented DB-first KB this would supersede; curator
  and retrieval pipeline carry over.
- [[repo_datasource]] — the datasource mechanics this specializes.
- [[loop_repo_compounding_v2]] — the loop's git model; consumes this KB as its blackboard.
- [[kb_convergence_ttl_reverification]] — TTL/verification semantics that move into
  frontmatter + index.
