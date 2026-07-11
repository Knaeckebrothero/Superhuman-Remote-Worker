---
tags:
  - knowledge-management
  - data-management
  - agent-architecture
  - git-integration
  - tool-development
status: active
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

**Status:** SHIPPING; **slices 1+2 COMPLETE** — slice 1 (dual-write, `68fc0603`),
slice 2 PR1 (gardener tools, `d0125805`), PR2 (curator verdict gate, `16105a96`),
the §11.1 hardening batch (`e1183757`: deterministic collision suffix, exact-dup
no-op, double-H1 fix, `duplicate-h1` lint) all COMMITTED, pushed and DEPLOYED to
dev; all TDD-built, ruff clean. **Slice 1 is LIVE-VERIFIED at production scale**
(151 interlinked OKF files in one night, §11.1). **The verdict gate was ENABLED
loop-wide 2026-07-04** (`7ae56e6f`, `curate_knowledge.verdict: true`; fires only
where `curator.enabled` — loops) and is **LIVE-VERIFIED**: over iters 7–10 on
Better Resavio the KB moved superseded 66→102 / archived 76→88, and an intra-job
duplicate written 42 s after its twin was SUPERSEDE'd at write time (§11.1
addendum). **Slice-2 stragglers landed 2026-07-05** (see §11.1 addendum):
`oversized-note`, `slug-forked`, embedding-backed `near-duplicate` (one pgvector
self-join, `KnowledgeStore.find_near_duplicate_pairs`) and opt-in
`dead-external-url` lint rules, plus distill-don't-dump + Garden-step direction
in all five curation prompt forks. **The Slice 3 substrate landed 2026-07-05/06**
(chunk schema, structural chunker, git-watermarked reindexer, chunk-RRF retrieval,
link index, Neo4j-optional tool paths, centroid near-duplicate support, and orphan
reconciliation). **Slice 4 was decision-locked and implemented on 2026-07-11**:
first-class `kb` datasources, provider-neutral credential-contained git ingestion,
initial/manual/periodic reindexing with operational status, explicit runtime bindings,
qualified handles, protected multi-KB passive retrieval, lite-tier support, and the
Cockpit authoring/status flow. The automated feature suites are green; deployment/live
verification remains separate operational evidence. Origin: design discussion 2026-07-03, building on the
substrate findings in [[knowledge_base_substrate_decision]]; refined the same day by a
six-agent research sweep (three codebase audits, three web — sources in §12).
[[loop_repo_compounding_v2]] shipped the same day, which changes this doc's footing: the
squash-merge flow, the post-merge hook point, and `retros/` (orchestrator-written notes
with OKF frontmatter, `type: retro`) are **in production**. Anything an agent writes
under `knowledge/` on its job branch already reaches `main` with zero further
orchestrator work — the loop is a ready delivery pipeline, and slice 1 now writes the
notes into it.

## TL;DR

- **A knowledge base = an OKF/markdown git repo + a disposable Postgres index + a
  toolset.** Files are canonical; the index is a rebuildable cache over git; tools enforce
  at write time what a filesystem can't.
- **KBs become datasources.** A KB datasource is a first-class `kb` datasource backed by
  an OKF git repository, attachable to any number of projects (org wiki, a customer handoff vault,
  this repo's own `docs/`). In the end state, the *project KB* stops being a special
  subsystem and becomes an auto-provisioned KB datasource attached at project creation
  — one mechanism, and cross-project knowledge sharing falls out for free. Slice 4 first
  proves external read-only datasources; native-project migration follows separately.
- **Access is split by operation, not by "files vs. tools":** native/project KB reads can
  go straight at the filesystem; writes go through `kb_*` tools that enforce integrity;
  queries go through the index; maintenance is a background gardener. External
  datasource KBs are index-backed and credential-isolated in the first Slice 4 delivery
  (no agent-side clone); external filesystem/write access is a later slice.
- **Sync is one-way and incremental**: the index stores the commit SHA it was built from;
  a git tree-diff against HEAD yields exactly the changed notes (git's object model
  already *is* the Merkle tree you'd otherwise build). No bidirectional sync, ever.
- This answers [[knowledge_base_substrate_decision]] §8 for the KB workload: **no graph
  scaffolding; Neo4j goes dormant for KBs** (the citation network remains the separate
  open case). It flips the substrate doc's default from "Postgres-canonical ⇄ OKF export"
  to "OKF-canonical ⇄ Postgres index" — §5 below argues why.

---

## Slice 4 decision lock (2026-07-11)

The following decisions are final for the first KB-datasource delivery. They replace
the corresponding "leaning" language and open questions later in this document. See the
[Slice 4 implementation plan](../superpowers/plans/2026-07-11-okf-kb-datasource-slice-4.md).

1. **Name and format:** the product and code call this an **OKF Knowledge Base**. OKF
   remains the formal format name; it is not relabelled as an Obsidian datasource.
   The UI may explain that the repository is ordinary Markdown with frontmatter and
   links, but `okf`/`kb` are the implementation terms.
2. **Datasource shape:** add a first-class datasource type, `type = "kb"`. It reuses
   repository URL, branch, token/SSH credential validation, and repository discovery,
   but is not `repository + format=okf`. A separate type gives clear tool gating,
   indexing lifecycle, access semantics, and Cockpit copy.
3. **Identity and location:** for an external KB, `datasources.id` is its stable
   `kb_id`. Its canonical location is the tuple **(repository URL, branch,
   `config.root_path`)**. `root_path` is a normalized relative POSIX path, defaults to
   the repository root, rejects `..`, and participates in the index pipeline version
   so changing it forces a clean rebuild.
4. **Attachment and rollout:** the existing project KB remains implicit, writable, and
   scoped by `project_id` during the first delivery. External KB datasources follow the
   datasource system's explicit-only selection model: project linkage makes them
   eligible, while a job/session receives one only when selected. Runtime reads search
   the native project KB plus the selected external KB ids. Turning the native project
   KB into an auto-provisioned datasource is the end state, but a later migration — not
   part of the first external-datasource slice.
5. **Index ownership and freshness:** the orchestrator owns ingestion. A
   provider-neutral git reader resolves HEAD, lists the tree, and fetches changed blobs;
   the existing commit watermark/blob-SHA reindexer remains the indexing engine. Initial
   indexing, a manual reindex endpoint, and the leader-gated periodic sweep ship first.
   Provider webhooks are deferred. An external KB is searchable without first running
   an agent and remains queryable on shell-less/lite workspaces. Reconciliation compares
   the indexed path/blob map directly with the current immutable tree, so a force-push
   does not require commit ancestry; parser/root/embedding pipeline changes still force
   a full rebuild.
6. **Credential boundary and v1 writes:** external KB datasources are **read-only in the
   first delivery**. Their repository credentials stay in the orchestrator indexing
   path and are not dispatched to agents; private external KBs are therefore not cloned
   into agent workspaces in v1. Agents use `kb_search`, `kb_read`, `kb_list`, and related
   query tools against the indexed snapshot. `kb_write`/`kb_update` continue targeting
   the native project KB only. External write-back (branch/PR policy, conflicts, and
   authenticated pushes) is a separate follow-up slice.
7. **Source-aware tools:** runtime context carries explicit KB bindings (id, stable
   alias/name, and access), separate from `project_ids`. Search and passive
   injection may span all authorized bindings, but every result identifies its source.
   Canonical note handles are `kb-alias:note-slug`; an unqualified slug is accepted only
   when it resolves uniquely and otherwise returns an ambiguity error. Optional `kb`
   selectors narrow search/list/read without proliferating tools. External read/search/
   list/related surfaces fetch the live index watermark best-effort and show an explicit
   indexed-snapshot marker; a binding's cached watermark is only a fallback.
8. **Retrieval policy:** the native project KB keeps a protected share of the passive
   injection budget so a large organization KB cannot drown out local project context.
   Results from external KBs carry the last fully reconciled commit and are explicitly
   described as indexed content. During `partial`/`indexing`/`failed` convergence, tools
   also show status and the attempted source HEAD: successful per-note mutations may
   already be present, so the cache must not be mislabeled as an immutable snapshot of
   the prior commit. Full-note reads come from this indexed content in v1.
9. **Backend tiers and Neo4j:** external KB query tools work on every workspace tier; no
   repository clone is required. Neo4j is neither required nor canonical. Slice 4 builds
   on the Postgres chunk/link index and does not add new graph behavior.
10. **Embedding identity:** centrally indexed KB chunks and runtime KB queries use one
    authoritative system-owned `KB_EMBEDDING_*` profile, distinct from the user's
    `EMBEDDING_*` memory profile. That credential is dispatched only when a native or
    selected external KB is in scope; reused agents clear stale KB profile fields. A
    selected but unusable catalog profile fails loudly and never falls through to a
    different environment model.
11. **Git and lifecycle boundary:** public datasource URLs accept only HTTP(S), SSH, or
    strict SCP syntax to an exact trusted host/port (`KB_GIT_ALLOWED_HOSTS`, with a small
    default public-provider set); local/file/remotely executable helpers, IP literals,
    loopback, arbitrary internal hosts, and URL-embedded credentials are rejected.
    Tokens/passwords require HTTPS. SSH is deploy-key-only and isolated from ambient
    agents/config. Deletion cancels local builds and takes the same local +
    cross-replica index lock across vector cleanup and app-row deletion; stale sweep work
    rechecks datasource liveness under that lock and cannot resurrect the index.
12. **Attachment authorization:** persisted project/datasource IDs are selections, not
    frozen grants. Thread creation, attach, and resume recheck the owner's current access;
    child jobs re-authorize explicit and inherited IDs. Agent-created jobs derive their
    user/project scope from an authoritative thread or parent (MCP uses its authenticated
    forwarded user), and an originless shared-key job POST is rejected. External KBs also
    reject legacy `job_id` assignment, preserving explicit-only attachment.

These choices deliberately stage the end-state unification. They deliver the reusable
datasource and retrieval model without coupling it to native-project migration or the
much larger external-write conflict/security problem.

### Slice 4 operational envelope

- Periodic freshness uses `KB_REINDEX_SWEEP_SECONDS` (default 900 seconds); creation and
  source-affecting updates also schedule an immediate best-effort full build, and owners
  can trigger incremental/full rebuilds manually.
- External Git bounds are 30 seconds for HEAD/tree/blob operations and 120 seconds for a
  fetch. Captured output is capped at 1 MiB for HEAD/fetch diagnostics, 16 MiB for the
  tree, and 8 MiB per Markdown blob. Timeout, output-limit, and cancellation paths kill
  the full Git/SSH/helper process group and delete snapshot/auth directories.
- Every changed run reads one immutable commit snapshot. Repository auth material stays
  in temporary mode-restricted files/environment; SSH ignores ambient agents, user/system
  SSH config, proxy commands, password auth, and default identities. Operators can set
  `KB_GIT_SSH_KNOWN_HOSTS` to switch deploy-key remotes from isolated first-use
  acceptance to strict host-key pinning.
- A full rebuild may incur embedding cost, so Cockpit requires confirmation. V1 has no
  provider webhook, separate note-count quota, or fetched-pack disk quota. Tree/blob
  limits bound parser inputs and diagnostics, and fetch time is capped, but an unusually
  large unfiltered-fallback pack remains an operational capacity consideration. Distinct
  external Git/embed runs are bounded by `KB_EXTERNAL_REINDEX_CONCURRENCY` (default 2 per
  orchestrator process); the leader sweeper itself remains sequential.
- The first rollout of the effective-profile fingerprint changes every existing KB row
  stamp. Trigger a full reindex during deployment or allow the default 15-minute sweep
  to do so; old-stamp vectors are deliberately invisible to new-profile queries while
  notes rebuild. A provider changing model weights behind the same unchanged
  provider/model/endpoint identity is not externally observable, so that case requires
  an operator-requested full rebuild.

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

- **KB datasource** — a new datasource type wrapping a git repo (URL/auth validation
  shared with `repository`), flagged as OKF-conventioned so the `kb_*` tools bind to it.
  The first external-datasource slice indexes centrally and does not clone into agents.
- **End state: Project KB = a KB datasource auto-provisioned and auto-attached at project
  creation**, exactly like the jobs repo and the default project's home Space. Deleting
  the special case means: same tools, same index, same maintenance loop whether the KB
  is project-private or an org-wide vault attached to 40 projects. Per the 2026-07-11
  lock, that native migration follows the external read-only datasource delivery.
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

**Slice 4 v1 exception for external datasources:** their canonical repository is read by
the orchestrator indexer, while agents read the indexed snapshot through KB tools. The
repository credentials are not dispatched and the private repo is not cloned into the
agent workspace. Native project KB behavior stays filesystem-backed and writable.

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
>
> Refinements from the code audit + field research (2026-07-03): the gate is a
> **chain-mode** call (one structured-output adjudication like `IngestionVerdictTask` —
> not another agent loop), with memory's **cost guard** (no neighbour above the
> similarity floor → straight add, zero LLM calls) plus a **content-hash pre-filter**
> (cognee's pattern) so unchanged candidates never reach the LLM; degrade to
> conservative-add on aux failure; the verdict prompt is read **at event time** (the
> runtime-field pattern — not memory's attach-time `load_prompt`, which can't honour
> `config.update` in persistent sessions). Economics caveat from the field: mem0
> retreated from verdict-gating to add-only in 2026 because gating fails at
> chat-message volume — it doesn't at curated-note volume, and add-only's
> "contradictions never reconciled" is exactly what we're preventing.

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
- **No index-only state** (SilverBullet's shipped invariant, adopted as a design rule):
  anything the index knows must be derivable from the repo. The bi-temporal columns are
  the one watched case — they stay "index-side parity," never a second truth.

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

Design (mechanism corrected 2026-07-03 after the code audit — see §12):

- The index stores **one watermark per KB: `indexed_commit`** (plus `blob_sha` per note
  row for idempotency and embedding gating), alongside a **`pipeline_version`** (config
  hash: embedding model + chunker/parser version + path-prefix). A changed embedding
  model, parser, chunker, or root invalidates every row and forces a full rebuild. Commit
  ancestry is deliberately unnecessary: the index's stored path/blob map is reconciled
  against the complete current tree, which is correct even after a force-push.
- **Reindex = tree diff, NOT the compare API.** Gitea's `get_compare` drops the `files[]`
  array (`gitea.py:841-889` keeps only commits) and Gitea 1.22's compare endpoint 404s
  on raw SHAs — the original "`git diff --name-status` via `get_compare`" plan does not
  work. The production-proven mechanism already exists: `_diff_files_by_tree`
  (`orchestrator/services/job_cloud_baseline.py:327-376`) builds `{path: blob_sha}` maps
  from `list_tree` at two refs and set-diffs them into added/modified/deleted (modified
  = blob-SHA inequality — the re-embed gate for free). Generalize its path-prefix filter
  to the KB root; upsert added/modified (bodies via `get_file_content(ref=HEAD)`),
  delete removed, re-embed only changed blobs, advance the watermark. Cost is
  **O(tree listing + changed files)**: a 10k-document org vault with 500 changed notes
  fetches and re-embeds only the 500.
- **Renames**: the tree diff reports a rename as delete+add. A stable frontmatter `id`
  (§6) may move the existing row only when its former path is confirmed absent from the
  current tree; this prevents a duplicate-id file from stealing a still-canonical row.
  Slice 4 re-embeds the added path even when the blob is unchanged (a bounded cost that
  keeps the per-note durability pipeline simple), then deletes the vanished path. A
  duplicate id makes the run partial and leaves the prior watermark unchanged.
- **Staleness detection is O(1)**: compare `indexed_commit` to the branch HEAD SHA
  (`get_branch_head_sha`). Query results are honestly *as-of the watermark* — expose
  `indexed_commit` in `kb_query`/`search_knowledge` responses so agents can see
  staleness themselves. Native writes still target canonical files; external v1 full
  reads deliberately come from the credential-isolated indexed snapshot and show its
  watermark.
- **Trigger end state (proposed in the original design):** the loop's post-merge hook — concrete call site `_advance_project_loop`
  directly after `merge_loop_job_branch` returns `merged` (`orchestrator/main.py:10095`,
  a `vector_db` handle already in scope there for TTL decrement); job-start catch-up
  check (SHA compare before the agent reads); a leader-gated periodic sweeper (the
  `run_when_leader` + tick shape of `stale_verification_sweeper.py`). A Gitea push
  webhook would be entirely new plumbing (no `create_hook` client support, no receiver
  endpoint exists anywhere) — defer it. **As built**, native indexing has post-merge +
  periodic + manual triggers (job-start catch-up remains deferred), while external Slice
  4 has create/source-update + periodic + manual triggers. Human out-of-band pushes are
  caught by the sweeper.
- **Force-push / history rewrite / pipeline change**: history rewrites reconcile safely
  from the current full tree without ancestry assumptions; pipeline/root changes force
  a full rebuild because prior vectors or scope are incompatible. The index remains
  disposable, and the operator-facing **`kb reindex --full`** escape hatch handles
  recovery or deliberate rebuilds.
- **Interrupted reindex self-heals**: per-row `blob_sha` makes re-runs skip already-current
  rows; the watermark only advances at the end.

### 5.1 Index internals (slice-3 spec, research-refined)

- **Chunk-granular rows**: a note-level row (`kb_id`, `id`, `path`, `blob_sha`, full
  frontmatter columns) plus chunk rows (`note_row`, `chunk_ix`, `heading_path`,
  `embedding`, `embedding_version`). Heading-aware splitting at a ~400–512-token target,
  merging sibling sections up to target, 10–15 % overlap only when forced to split
  mid-section; short notes stay single-chunk naturally — the common case, and the
  correct outcome. Semantic chunking benchmarks *worse* than structural; skip it.
  Anthropic-style contextual retrieval: skip for v1 (notes are short and self-titled;
  revisit as opt-in for the >1k-token minority).
- **Embed text = breadcrumb + chunk**: prepend title, `type`, tags, and the heading path
  as natural text (never raw YAML) — the highest-ROI cheap trick in the 2025-26
  literature (15–25-pt QA-accuracy gains reported; Khoj ships exactly this).
- **`embedding_version` per embedded row from day one** (model id + dims + chunker
  version + a secret-free effective-profile fingerprint over provider, normalized
  endpoint, and catalog endpoint identity); retrieval filters
  `WHERE embedding_version = current`. API keys are deliberately excluded, so key
  rotation does not rebuild the corpus, while moving the same model label to another
  transport does. Mixed-model/transport vectors
  cause silent "index drift" — great results on fresh rows, garbage on stale ones, no
  error anywhere. No blue-green machinery is used: the index is disposable, so model
  migration = bump version → per-KB rebuild. Old-version chunks are filtered immediately
  and successfully rebuilt notes become searchable incrementally; the KB remains visibly
  `indexing`/`partial` until the clean watermark advances. A full
  10⁵-note rebuild costs $0.50–$3.25 in embeddings — rebuild-on-doubt is economically
  free. Batch embedding API for full rebuilds, synchronous calls for incrementals (wire
  the existing-but-unused `embed_batch`, `embedding_service.py:146` — today's upsert
  embeds one call per note).
- **Retrieval stack**: reuse `knowledge_hybrid_search`
  (`vector_schema_current.sql:147-222` — RRF over dense halfvec + tsvector + recency,
  weights 0.6/0.3/0.1) with `kb_id` threading; `rrf_k` 50→60 (the literature standard)
  is a tunable, not a rewrite. Plain tsvector for the sparse arm — SPLADE needs GPU
  inference for inconsistent gains; if lexical quality ever disappoints, ParadeDB
  `pg_search` (real BM25) is a drop-in index swap that changes no schema. pgvector HNSW
  at stock `m=16, ef_construction=64` — comfortable to ~5M vectors; don't tune before
  measuring. `search_knowledge` over-fetches ~50 fused candidates and returns 15–20,
  with a no-op reranker slot between fusion and return (a hosted/local reranker wires in
  later as a `rerank: true` precision mode — the single biggest post-hybrid quality
  lever, but agents can grep-and-read after search, so it's phase-2).
- **Schema delta** (migration `vector/0008_*`, following the `0006`/`0007` ALTER
  precedents): `knowledge_index` gains `kb_id`, `path` (UNIQUE `(kb_id, path)`),
  `blob_sha`, `superseded_by` + `invalidated_at` (**required, not optional** — the
  supersede edge lives only in Neo4j today, so retiring the Neo4j write path without
  this column loses the chain), `embedding_version`; plus the chunk table and
  `kb_index_watermark(kb_id PK, repo_name, branch, indexed_commit, pipeline_version,
  updated_at)`. Dead links are stored as rows (target = NULL) so `kb_lint`'s dead-link
  check is a query, not a re-scan. Preserve the default search filter `status='active'`
  exactly as-is across the cutover (it is *stricter* than "not superseded" — it also
  excludes resolved/archived).

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

Field validation (2026-07-03): this deviation is the **most strongly validated choice in
the design**. org-roam v2 made stable ids mandatory and *deleted net code* (−3k LOC —
the path-maintenance machinery disappeared); Dendron ran frontmatter `id` in production
for years; and the OKF spec itself has **no rename/alias mechanism at all** (its only
mitigation is "consumers MUST tolerate broken links"), so the id fills a real hole.
Obsidian's no-id stance works only because one app monopolizes writes and mass-rewrites
links on rename — false in any multi-writer vault. Three rules the prior art adds:

- **Resolution order**: exact path → id mapping → basename shortest-path. A link that
  resolves by none is *dead*; one that basename-matches more than one note is
  *ambiguous* (Foam's distinction) — two separate lint states. Humans editing in
  Obsidian will get links auto-rewritten on rename; agents' links survive via the id —
  the resolver must accept both worlds.
- **Rename is a tool verb** (`kb_update` with `move`): keep the id, update the mapping,
  refuse destination collisions (Dendron's refactor semantics). Human renames arrive as
  out-of-band edits; the gardener's `doctor --fix` pass backfills ids on human-created
  notes from the path slug.
- **Auto-maintained frontmatter writes only on material change** (Dendron's scar:
  auto-`updated` timestamps dirtied git on every open-and-save, causing merge conflicts
  in shared vaults until patched in v0.48). Applies directly to `modified`, TTL
  counters, and `last_verified_cycle` — a gardener that stamps rows it didn't materially
  change floods the tree-diff reindex with no-op work and generates conflicts against
  human editors.

## 7. Toolset

Signature-stable evolution of the existing tools where possible (the substrate doc's
"backend swap, not UX change" principle), plus the new maintenance surface:

| Tool | Op | Notes |
|---|---|---|
| `kb_write` | write | Creates a note: validates frontmatter (OKF `type` required; **`description` required** — one sentence; the whole progressive-disclosure economy of index files runs on it, and 203/417 of our own `docs/` files prove backfilling is what never happens), enforces id uniqueness, resolves links, writes `.md` + updates index + embedding in one step, commits. Emits **standard markdown links, not wikilinks** — OKF's emergent graph is body markdown links only; wikilinks are accepted on read (humans will write them) and normalized by lint. Stamps **provenance frontmatter** (`author` role, `job`, `branch`) — squash-merge erases git authorship, so provenance must live in the note itself. |
| `kb_update` | write | Edit + supersede semantics (`status: superseded`, `superseded_by`, **`invalidated_at`** — Graphiti-style "when it stopped being true", matching RecallStore's existing `valid_to`); supersession is also mirrored as a body link ("Superseded by [x](x.md)") so the chain is a graph edge for standard OKF consumers, not just our index. `move` = the rename verb (§6). |
| `search_knowledge` | query | Hybrid over the index — the existing RRF function with `kb_id` scoping (§5.1: over-fetch ~50 → return 15–20, empty reranker slot, response carries `indexed_commit`, optional 1-hop link expansion — "GraphRAG-lite" via linked-note titles/snippets). Signature unchanged. |
| `kb_query` | query | Structured: by type/tag/status, backlinks, unanswered questions, contradictions — the Appendix B SQL surface. |
| `kb_lint` | maintenance | The 13-rule starter set the ecosystems converged on (obsidian-linter/Foam/lychee — §12). **Error tier**: YAML validity + required keys (`id`, `type`, `description`); id uniqueness/format; dead links; ambiguous links; property-type violations against a KB-level type manifest (Obsidian types.json pattern — types declared per key KB-wide); supersede-chain integrity (target exists, not itself superseded). **Warning tier**: orphans (index/MOC notes excluded); duplicate keys/array values; heading structure; tag-taxonomy drift; near-duplicate content candidates (embedding-backed — our genuine addition); stale TTL/`last_verified_cycle`; dead external URLs (scheduled sweep, network-bound). Blocking gate for **agent** writes only — never for human merges (every ecosystem that hard-gated prose shed users). |
| `kb_index` | maintenance | Regenerate hierarchical `index.md` per directory in **OKF §6 shape** (no frontmatter — `index.md` is a reserved *filename*, which is also how generated indexes are excluded from embedding/orphan logic; heading-grouped `[Title](url) - description` bullets sourced from target frontmatter; bundle-root index carries `okf_version: "0.1"`), under a hard budget (~200 lines / 25 KB per file — Claude Code's memory-index budget). Human-authored heading sections survive regeneration (Zoottelkeeper's protected-sections pattern); never touches a file that isn't an `index.md`. |

Reads need no tools: the KB is a cloned directory in the workspace. (Optional gardener
verb, cheap: emit OKF `log.md` from `git log main` — spec-native, newest-first change
history that survives shallow clones.)

## 8. Tiers — one toolset, one knob

These are **KB substrate/deployment tiers**, not agent workspace tiers. A shell-less
agent on a Full deployment still has index-backed KB query tools.

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
   the one-time Neo4j → OKF migration per project: relationship edges become **standard
   markdown links** (not wikilinks — corrected 2026-07-03, see §7: OKF's emergent graph
   is body markdown links only), add `type:` + `id:` + `description` frontmatter, write
   into the project's KB repo, initial index build.
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
- **Conflicts are loud, in-band**: on merge conflict or lint-detected out-of-band
  damage, the gardener writes a visible artifact into the KB itself (a `type: conflict`
  note / gardener report), not just a log line — the ecosystem lesson (Obsidian Sync's
  top complaint) is that users forgive automated merging but not *silent* merging.
- **Laundering rule** (TMA-NM, arXiv:2606.24322): trusted writers must not restate
  untrusted claims as fact — write-time origin binding is provably necessary; content
  inspection isn't enough. Retros and gardener notes *cite* agent claims
  (job/branch/commit) rather than repeating them plainly; the `merge_status`-backed
  retro format already mostly does this — it's now a rule for every
  orchestrator-written note.

## 11. Slices (independently shippable — reordered 2026-07-03 after v2 shipped)

0. **Loop delivery pipeline** — DONE via [[loop_repo_compounding_v2]]: per-job branches,
   squash-merge to `main`, post-merge hook point, `retros/` as the first OKF note family.
1. **Dual-write** — ✅ **COMMITTED 2026-07-03** (`68fc0603`, on `develop`; strict
   TDD, +19 tests / 223 green across the knowledge suites, ruff clean). The
   strangler-fig first step: `kb_write`/`kb_update` additionally materialize each note
   as **flat `knowledge/<slug>.md`** on the workspace backend — flat resolved
   2026-07-03: link targets derivable from the slug alone (markdown links need no index
   lookup in a slice with no index), a type change never forces a file move, and the
   prior art says navigation belongs to index/MOC notes not folder taxonomies (`type`
   stays frontmatter + a `kb_index` grouping). As built
   (`src/tools/knowledge/knowledge_tools.py`):
   - **`_render_note_md(note) -> str`** — one pure OKF serializer, factored out of
     `kb_export` and shared by all three write paths; emits `type` + `description`
     frontmatter, **standard markdown links** (never wikilinks), in-note provenance
     (`author`/`job`/`branch`), optional fields only when present.
   - **`_dual_write_note(context, slug, note)`** — guarded on `context.has_git()`
     (stricter than `has_workspace()`: persistent sessions, repo-less projects and lite
     tiers keep their DB-only write → skip + log), **non-fatal** exactly like the
     pgvector write-through, **never local I/O**. Provenance from `_note_provenance`
     (`_job_metadata['config_name']` → `agent_id`; branch via
     `git_manager.current_branch()`).
   - `kb_write` gained an optional `description` arg; `kb_update` dual-writes **before**
     the pgvector upsert so the canonical file lands even if the disposable index write
     fails.
   - **`kb_export`'s local-`Path` fallback removed** (the "writes to the ephemeral agent
     host, invisible to the pod's clone" bug — same family as the citation stub-mode
     bug): it now requires a workspace and reuses `_render_note_md`.
   - **One deliberate deviation from §7**: `description` is *derived* (first sentence of
     the body) when omitted, **not rejected** — the hard required-key gate moves to
     slice-2 `kb_lint` (which gates agent writes), so tonight's loop can't break on a
     missing one-liner. Frontmatter always carries a `description` either way.
   - Delivery is free: phase/todo/completion commits `git add -A`
     (`git_manager.py:205`), and neither `.gitignore` floor lists `knowledge/`.
   The DB stays the retrieval substrate; the v2 merge delivers the files to `main`.
   Agent-side only, no schema, no orchestrator change. **E2E ✅ DELIVERED 2026-07-04**:
   the first overnight loop run on the deployed image wrote 151 OKF files to the loop
   repo's `main` — evidence and findings in §11.1. No index reads these files yet
   (slice 3), so zero retrieval blast radius.
2. **OKF lint/gardener toolset + curator-as-proper-auxiliary** — split into two PRs.
   - **PR1 — gardener tools ✅ COMMITTED 2026-07-03** (`d0125805`). Pure engine in
     `src/tools/knowledge/gardener.py` (no workspace/DB, reusable against any vault):
     `parse_note_md` (inverse of slice-1 `_render_note_md`), `lint_kb → LintReport`
     (9 deterministic rules: missing-frontmatter, invalid-yaml, missing-required-key,
     invalid-id, duplicate-id, dead-link, broken-supersede, orphan, missing-title —
     reserved `index.md`/`log.md` exempt), `render_index_md` (OKF §6 index: `## <type>`
     groups, gen-markers preserve human sections, loud 200-line/25 KB truncation). Two
     thin `@tool` wrappers `kb_lint`/`kb_index` (12 KB tools now). The two deferred
     rules (embedding-backed near-duplicate, network dead-external-URL sweep) landed
     2026-07-05 with the straggler batch — §11.1 addendum.
   - **PR2 — curator verdict gate ✅ COMMITTED 2026-07-03** (`16105a96`, on `develop`;
     strict TDD, 233 green across the knowledge suites, ruff clean). Ships **inert**
     (both knobs default off) — a deliberate config-flip-and-deploy step, deferred to a
     scoped test run (§3 note on the loop-vs-curator seam). The §3 refactor, as built:
     - **`KnowledgeStore.find_similar_many(project_id, embedding, k, min_similarity)`** —
       the neighbour fetch (active-only, `<=>` cosine, transient `.similarity`); the KB
       analog of `RecallStore.find_similar_many`. Empty result = the cost guard.
     - **`KnowledgeVerdict` / `KnowledgeVerdictTask`** (`auxiliary.py`) — chain-mode
       adjudication (one structured call, not an agent loop) returning
       ADD / UPDATE / SUPERSEDE / DISCARD + 1-based `target_indices`; prompt passed at
       **event time**, mirroring `IngestionVerdict`.
     - **`src/services/knowledge/ingestion.py`** (mirrors `src/services/memory/`):
       `KnowledgeVerdictService.adjudicate` (cost guard → straight ADD on no neighbour;
       **content-hash pre-filter** → DISCARD exact dups before the LLM; conservative-ADD
       fallback on aux error/wrong-shape), `gate_candidate` (embed → `find_similar_many`
       → adjudicate → resolve `target_indices` to notes), `build_knowledge_verdict_service`.
     - **Gate seam**: `create_kb_tools(context, verdict_service=?, verdict_prompt=?)` —
       only the curator passes them, so loop/worker agents write **ungated** as before
       ("loop jobs run bare"). `kb_write` routes: DISCARD skips, UPDATE redirects the edit
       onto the duplicate, SUPERSEDE creates then retires the stale note(s)
       (`status=superseded` + `SUPERSEDED_BY`), ADD/any gate error → normal create.
       `kb_update`'s body factored into a shared `_update_existing` helper.
     - **Config**: `auxiliary.tasks.curate_knowledge.{verdict,verdict_top_k,review_floor}`
       — **default OFF** (measured opt-in, like `memory.ingestion.enabled`). Prompt
       `config/prompts/knowledge_verdict_prompt.txt`; wired through `archive_phase` via
       `create_archive_phase_node(knowledge_verdict_prompt=…)`.
     - **E2E = an overnight loop run** with `curator.enabled` + `verdict` both on (the
       async gate is validated by real-execution unit tests, but tuning the verdict wants
       live slice-1 KB volume). **Enablement deferred** (2026-07-03 decision): flip on a
       **scoped test project**, not loop-wide — because of the seam below.
     - **Two seam caveats to weigh before enabling** (design §3 / §11 open questions):
       (a) the gate only reconciles the **curator's** writes; loop agents `kb_write`
       directly and stay ungated ("loop jobs run bare"), so the F33/F38 *agent-authored*
       noise is untouched until the curator itself is the writer — enabling the curator is
       a loop-wide behaviour + cost change. (b) DISCARD/UPDATE/SUPERSEDE only fire once
       `find_similar_many` has neighbours in the pgvector index, so a run from a sparse
       index is mostly ADD until volume accumulates.
     - **Deferred polish** (not blocking, noted for the resume): UPDATE carries only the
       candidate's `content` onto the target (drops title/type — the target keeps its
       identity); the new SUPERSEDE note gets the reverse `SUPERSEDED_BY` edge but not a
       forward `SUPERSEDES` link; the curator prompt has the gardener verbs in its tool
       surface but isn't *directed* to run a lint/index pass. (All three closed by the
       2026-07-05 straggler batch — the near-duplicate rule, the dead-external-URL
       sweep, and the curator prompts' Garden step; §11.1 addendum. The UPDATE
       content-only carry and the missing forward `SUPERSEDES` link remain open
       polish.)
3. **Postgres index + query tools + retrieval cutover** — the §5/§5.1 spec: tree-diff
   watermark reindex, chunk rows, `embedding_version` + `pipeline_version`, migration
   `vector/0008`; `search_knowledge` backend swap (the RRF functions gain `kb_id`);
   post-merge + job-start + leader-gated-sweeper triggers; `kb reindex --full`. DB write
   path retired (files become canonical, completing the strangler fig). **This slice is
   also what makes the existing `databases.neo4j.enabled` toggle real** (§9.4).
   **PR breakdown (2026-07-05, mirrors slice 2's split — each inert until PR4 flips):**
   - **PR1 — schema + store surface** (migration `vector/0008`, additive-only):
     `knowledge_index` gains `kb_id`, `path` (UNIQUE `(kb_id, path)`), `blob_sha`,
     `superseded_by`, `invalidated_at`, `embedding_version`; new chunk table
     (`note_row`, `chunk_ix`, `heading_path`, `embedding`, `embedding_version`) and
     `kb_index_watermark`; `KnowledgeStore` CRUD for the new shapes. Nothing reads
     the new columns yet — the live upsert path is untouched.
     **DONE 2026-07-05 (develop, TDD, uncommitted):** `0008_kb_index_chunking.sql`
     (halfvec HNSW on chunks mirrors 0005; chunk vector lives on `knowledge_chunks`,
     not the note row); `KbWatermark`/`KnowledgeChunk` dataclasses + six store
     primitives (`get_watermark`, `upsert_watermark`, `get_indexed_blob_shas`,
     `upsert_kb_note`, `replace_note_chunks`, `delete_kb_note`), `project_id = kb_id`
     backfill so legacy filters keep working. 19 tests in
     `tests/test_kb_index_chunking.py`; 331 green across the knowledge suites.
     *PR3 hygiene note:* `upsert_kb_note` targets the `(kb_id, path)` arbiter, so a
     legacy row sharing `(project_id, note_id)` would collide there — the reindexer's
     full-rebuild-first / migrated-rows path resolves this (same gate as the
     historically-retired-frontmatter audit below).
   - **PR2 — chunker + embed pipeline** (pure, no DB): heading-aware structural
     chunker (~400–512-token target, sibling merge-up, 10–15 % overlap only on
     forced mid-section splits), breadcrumb embed-text builder (title/type/tags/
     heading-path as natural text, never raw YAML), `embedding_version` stamping
     (model id + dims + chunker version; later extended with the effective-profile
     fingerprint), wire the unused `embed_batch` for bulk.
     **DONE 2026-07-05 (develop, TDD, uncommitted):** `src/tools/knowledge/chunker.py`
     — `chunk_note` (heading-stack breadcrumbs, greedy sibling merge-up to target,
     paragraph-packed forced splits with 12 % overlap seed; reuses
     `chunk_planner.count_text_tokens` for tiktoken sizing), `build_embed_text`,
     `embedding_version` (`model:dims:CHUNKER_VERSION`, `CHUNKER_VERSION="c1"`), and
     async `embed_note_chunks` → the exact dict shape `replace_note_chunks` (PR1)
     consumes, via one bulk `embed_batch` per note. 21 tests in
     `tests/test_kb_chunker.py` (injected word-counter for deterministic sizing);
     real-tiktoken smoke: short note → 1 chunk, 2628-tok note → 6 chunks ~438 tok
     each. Nothing calls it yet — PR3 wires parse → chunk → embed → persist.
   - **PR3 — tree-diff reindexer + triggers**: watermark → git tree diff → parse
     changed notes (gardener `parse_note_md`) → upsert note+chunk rows / remove
     deleted → advance watermark; full rebuild re-pointed at the git tree +
     `kb reindex --full` operator hatch; per-row `blob_sha` interruption self-heal.
     Triggers: post-merge hook, job-start, leader-gated sweeper.
     **DONE 2026-07-05 (develop, TDD, uncommitted):**
     `orchestrator/services/kb_reindex.py` — pure helpers (`knowledge_blob_map`
     filters `list_tree` to `knowledge/**/*.md` minus reserved; `plan_reindex`
     blob-sha set-diff; `note_fields` inverts `_render_note_md` frontmatter with
     CHECK-constraint-safe fallbacks) + `reindex_kb` orchestration (up-to-date
     short-circuit on head+pipeline_version; full rebuild on version change/no
     watermark/force; embed-BEFORE-write per note so a failed embed keeps the
     stale blob_sha for retry; unparseable notes SKIP — lint's problem, never
     wedges the watermark; watermark advances ONLY on zero-error runs) +
     `kb_sweep_tick`/`kb_reindex_sweeper_loop` (KB_REINDEX_SWEEP_SECONDS=900).
     Supporting: `EmbeddingService` gained explicit-config kwargs (orchestrator
     pod has no EMBEDDING_* env — the factory `_build_kb_embedding_service` in
     main.py resolves catalog-first via `resolve_default_for_capability` +
     `_inject_env_key_credentials`, env fallback, None→skip honestly);
     `KnowledgeStore.adopt_legacy_row` claims pathless legacy rows before the
     `(kb_id,path)` upsert (the `uq_knowledge_project_note` collision guard).
     Triggers wired: post-merge (fire-and-forget after `merge_status=="merged"`
     in the loop advance), leader-gated sweeper (`run_when_leader`), operator
     hatch (`POST /api/projects/{id}/knowledge/reindex?full=` + MCP
     `reindex_knowledge`). **Deliberately deferred: the job-start trigger** —
     nothing reads the index until PR4's cutover, post-merge covers the loop
     write path, and the sweep covers out-of-band edits; revisit at PR4 where
     stale-at-read matters and the response's `indexed_commit` exposes it.
     30 tests `tests/test_kb_reindex.py` (+2 store, +4 embedding-service);
     379 green across touched suites, ruff clean incl. main.py.
     Pushed 2026-07-05 (`cc94d336` PR1 / `7210604c` PR2 / `bfce75ec` PR3).
   - **PR3.1 — live-verification fixes (2026-07-05, develop, TDD, uncommitted).**
     Dev deploy shook out three production-only bugs no unit test could see
     (full forensics: gitea access logs + pgvector row timestamps + podman
     import bisection of the deployed images):
     1. **Import heisenbug / sweeper silent death.** The orchestrator image
        ships without `neo4j`, and `src/tools/__init__` eagerly builds the
        registry → `knowledge_tools` → `knowledge_graph` → `neo4j_db` — so the
        FIRST import of anything under `src.tools` (chunker → `services.
        kb_reindex`) raised ModuleNotFoundError, and the failure left
        `src.tools.knowledge` cached so every RETRY succeeded. Net effect: the
        sweeper (first import at leadership acquisition) died silently on every
        pod generation, while post-merge triggers worked once an earlier failed
        import had polluted `sys.modules` (the 13:28Z run indexed 323 notes).
        Fix: neo4j import guarded in `src/database/neo4j_db.py` (failure moves
        to `Neo4jDB(...)` construction); `run_when_leader` now logs factory
        raises + loop crashes at ERROR instead of dying/respawning invisibly.
        Regression-pinned by `tests/test_neo4j_import_guard.py` (subprocess,
        blocks neo4j, first-import + order-independence) and
        `tests/test_leader_election_wrapper.py`; import validated green inside
        the actual `sha-2e8b807` image via podman mount.
     2. **Zero chunks: missing pgvector codec.** `postgres.py` registers the
        asyncpg vector codec in `try/except ImportError: pass` — the
        orchestrator image lacks the `pgvector` package, so every chunk INSERT
        failed with `invalid input for query argument $6 … (expected str, got
        list)`; 323 notes upserted, 0 chunks, watermark honestly refused
        (`partial`, errors=325 — reproduced live via the operator endpoint).
        Agent-side slice-1 writes never hit this (agents ship pgvector). Fix:
        `pgvector>=0.2.0` added to `orchestrator/requirements.txt`. Plus the
        **stamp-after-chunks** redesign: `upsert_kb_note` now lands UNSTAMPED
        (blob_sha/embedding_version NULL) and `stamp_note_indexed` sets both
        only after `replace_note_chunks` succeeds — before this, a chunk-write
        failure left the note stamped and the incremental diff would have
        skipped it forever once a watermark existed.
     3. **Embed batch cap.** The self-hosted TEI endpoint 422s above 64 inputs
        per request; the 127 KB legacy dumps chunk into 95+. `embed_note_chunks`
        now splits into ≤`EMBEDDING_MAX_BATCH` (env, default 64) sub-batches,
        order-preserving.
     Also: per-KB asyncio lock in `reindex_kb` (two post-merge triggers 30s
     apart ran interleaved full rebuilds), and the post-merge trigger now fires
     on `merge_status in ("merged", "empty")` — knowledge-only jobs (iter-16
     scholar) land notes via kb_write with an empty branch diff, and the
     up-to-date short-circuit makes a false fire one HEAD read. Live state
     note: ~250 active pgvector rows are file-less ghosts (files deleted/renamed
     on main without kb_delete — dual-write gap on delete); they stay pathless,
     invisible to the diff and to PR4 chunk retrieval, and get archived by the
     migration-hygiene audit.
   - **PR4 — retrieval cutover + Neo4j demotion**: `search_knowledge` → existing RRF
     over chunk rows with `kb_id` (over-fetch ~50 → return 15–20, no-op reranker
     slot, response carries `indexed_commit`); `kb_related`/`kb_provenance`/
     `kb_contradictions` degrade to 1-hop link-table queries; `create_kb_tools`
     stops hard-requiring Neo4j (the registry no longer strips `kb_*` on Neo4j-less
     deployments); DB-first write path retired — files canonical. Preserve the
     `status='active'` search filter exactly. Port `find_near_duplicate_pairs` to
     the new schema — which forces the **near-duplicate definition under chunking**
     (note-level representative embedding vs. max chunk-pair similarity): decide
     here with the runbook's self-join SQL against live data.
     **IN PROGRESS 2026-07-05 (develop, TDD, uncommitted) — split into PR4a/b/c/d:**
     - **PR4a DONE — chunk-RRF search.** Migration `0009_knowledge_chunk_hybrid_search.sql`
       (`knowledge_chunk_hybrid_search(query, embedding, kb_ids[], version, …)` returning
       SETOF knowledge_index: dense+sparse arms over `knowledge_chunks` collapsed to the
       best chunk per note via `MIN(rank_ix) GROUP BY note_row`, recency arm over notes
       **with an EXISTS-chunks guard so file-less ghosts stay invisible**, `status='active'`
       preserved, `embedding_version` drift filter, rrf_k 60). `KnowledgeStore.search_chunks`
       (over-fetch → no-op `_rerank_chunks` slot → truncate to match_count). Artifact
       regenerated; migration verified applying against real pg15+pgvector.
     - **PR4b DONE — kb_search cutover.** `kb_search` now calls `search_chunks` (note-level
       `hybrid_search` is blind to reindexed notes — their embedding is NULL, vectors live
       on chunks), passes the live `embedding_version` (model + dimensions + chunker +
       effective-profile fingerprint, resolved from the query-time EmbeddingService so
       it matches the reindexer stamp),
       and surfaces the watermark `indexed_commit` in the header.
     - **PR4c-1 DONE — link table.** Migration `0010_knowledge_links.sql`
       (`knowledge_links`: source_note_row FK CASCADE, kb_id, source_id, target_id slug,
       rel_type default `references`; target resolved at READ time so mid-rebuild ordering
       and dead links are non-issues). `replace_note_links` + `get_related_notes` (1-hop
       bidirectional, active-only read-time join). Reindexer extracts body markdown links
       (`_internal_link_targets`, same parser as the dead-link lint) and rewrites edges
       per note BEFORE the durability stamp. Artifact regenerated.
     - **PR4c-2 DONE — Neo4j-optional read/degrade side.** `create_kb_tools` tolerates
       `kg is None` (only the store is required). `kb_related` degrades to
       `get_related_notes`; `kb_contradictions`/`kb_provenance`/`kb_unanswered`/`kb_export`
       degrade **honestly** to a "requires the Graph tier" message (CONTRADICTS/DERIVED_FROM/
       ANSWERS have no files-canonical representation — no fabrication from generic
       `references` edges). *Deviation from the spec's "all three degrade to 1-hop link
       queries": only `kb_related` maps cleanly; the other two are honest degrades.*
     - **PR4c-3 read half DONE.** `KnowledgeStore.get_note_by_slug` (status-agnostic full
       read, kg.read_note dict shape) + `list_notes` (kg.list_notes shape, tag via
       `ANY(tags)`); `kb_read`/`kb_list` gain `kg is None` branches over them.
     - **PR4c-3 write half + toggle flip DONE (2026-07-06, TDD, uncommitted).** kg-less
       `kb_write` (validates `note_type`/`confidence` up-front like `kg.create_note`; slug via
       `slugify`, deterministic content-hash fork on base-slug collision, content-hashed
       fallback for empty slugs; then `upsert_note` + `_dual_write_note`, Neo4j never touched)
       and `_update_existing_kgless` (reads the row via `get_note_by_slug`, applies
       content/append/status/confidence/add_tags in Python, writes store + OKF file; `add_links`
       round-trip as generic body links via the reindexer — graph-only rel_type not preserved,
       matching the honest degrade). `ToolContext.has_knowledge()` relaxed to **store-only**
       (Neo4j optional); registry gate message updated. Neo4j path byte-identical (additive
       `kg is None` branches). 15 write-path tests + 4 `has_knowledge` tests; 459 green across
       the touched suites; ruff clean.
       - **Risk reframe:** because the Neo4j path is byte-identical, the whole kg-None surface
         is dormant on every Neo4j-*enabled* deployment (dev/prod included) — the flip changes
         behavior only when a deployment sets `databases.neo4j.enabled=false`, a controlled
         event. Real milestone test = a Neo4j-less E2E on local k3d.
       - **Known follow-up (out of scope):** the inline curator
         (`curate_and_store_knowledge` / `assemble_and_converge_knowledge` in `auxiliary.py`)
         still guards internally on `if not kg: return None`, so it silently no-ops on a
         Neo4j-less deployment even though the outer `has_knowledge()` gate now passes. No
         crash, no regression (kg-less curation never worked); wire it to the store's
         `list_notes` when doing the Neo4j-less E2E.
     - **PR4d DONE (2026-07-06, TDD, deployed `sha-ae0cddc`) — near-dup restored via note centroid.**
       Decision (settled): the near-dup-under-chunking semantic stays *whole-note*
       ("should a gardener merge these two notes?"), so instead of a chunk×chunk self-join
       (O(chunks²) ≈ 20M pairs, noisy on shared boilerplate sections) the reindexer stores a
       **centroid** — `note_centroid()` = the per-dimension mean of a note's chunk embeddings
       (pure helper in `chunker.py`) — back onto `knowledge_index.embedding` *atomically with
       the stamp* (`stamp_note_indexed(..., centroid=)`, one UPDATE, no stamped-without-centroid
       window; omitted → embedding left untouched). **`find_near_duplicate_pairs` is byte-for-
       byte unchanged** (it already filters `embedding IS NOT NULL`); reindexed notes, whose
       note-row embedding had gone NULL at the chunk cutover, are visible to the self-join
       again. No schema/migration change. Cosine (`<=>`) normalises magnitude so a plain mean
       is a sound centroid; legacy agent-written notes still carry a whole-note embedding —
       same space, cosine-comparable, and they converge to centroids as they reindex.
       - **Floor still 0.9 default; live re-tuning deferred** (as with the note-level 0.97):
         chunked + breadcrumb-prefixed embeddings shift the similarity distribution, so the
         floor is re-judged against the live centroid index in the migration-hygiene pass.
   **Migration-hygiene prerequisite (before PR3/PR4 land, zero-job, offline):** the
   reindex reads truth from *files* — audit the vault for historically-retired notes
   whose file frontmatter still says `active` (retired before `_update_existing`'s
   dual-write existed) and repair, or the cutover resurrects them. This is
   `tests/okf_kb_slice2_straggler_validation.md` §1+2 promoted to a slice-3 gate.
   Sequencing: PR1 ∥ PR2, PR3 needs both, PR4 last.
   **AUDITED 2026-07-06** (read-only, dev cluster). Findings: resurrection is tiny
   (2 files); ghosts have grown to 396 active pathless rows; the 5 invalid-YAML
   files trace to a *live* renderer bug (`_render_note_md` emits unquoted
   `tags`/`keywords` flow sequences); the 07-05 0.97 near-dup floor was never
   applied in code. Concrete per-item remediation tracker (code fixes + live
   mutations + deferred tuning): **`docs/features/okf_kb_hygiene_worklist.md`**.
   **REMEDIATED 2026-07-06:** the four code guards shipped + deployed (`sha-ae0cddc`) —
   C-1 renderer quoting (`33396baa`), the 0.97 `kb_lint` floor (D-1), the near-dup
   `embedding_version` guard (D-2), and `path IS NOT NULL` on `list_notes` /
   `get_note_by_slug` (B-1). The **root cause** was reframed: ghosts are an
   **adoption/reconciliation gap**, not a delete gap — the agent write-through
   (`upsert_note`) is born pathless, the reindexer adopts it slug-keyed once the file
   lands, and un-adopted rows are invisible to the path-keyed delete. Closed by **R-1**:
   `KnowledgeStore.reconcile_orphans` (project_id-keyed, 1h adoption grace, soft-archive)
   + a non-fatal per-reindex pass that surfaces a `reconciled` count (implemented,
   uncommitted; spec `docs/superpowers/specs/2026-07-06-kb-ghost-reconciliation-design.md`).
   It archives orphans on the next clean reindex, so the bulk B-2 cleanup is retired as
   a no-op. Live loop-repo repairs (A-1 resurrection files, C-2 invalid-YAML backfill)
   await dev-cluster access (tunnel down 2026-07-06).
4. **KB datasource type (implemented 2026-07-11; live verification pending)** — first ship external,
   centrally indexed, read-only KB datasources; migrate the native project KB into the
   same model afterward. Detailed implementation:
   [Slice 4 implementation plan](../superpowers/plans/2026-07-11-okf-kb-datasource-slice-4.md).
   As-built mechanics:
   - **Repository credential plumbing exists**: `repository` datasources already support
     token/SSH auth and external git URLs. Slice 4 reuses their validation/normalization
     for an orchestrator-side git content source, but deliberately does **not** reuse the
     agent-workspace clone path in v1. The KB boundary is stricter: tokens/passwords are
     HTTPS-only; SSH/SCP requires an explicit deploy key and ignores ambient SSH agents,
     identities, config, password auth, and proxies. A later external-write slice can
     revisit cloning.
   - **Org-wide vaults are nearly free**: `datasources.is_global` already means
     "visible to all users" and `list_eligible_datasources` already returns
     owned + global + project-linked — an org wiki is a KB datasource with
     `is_global: true`. No new sharing machinery.
   - **Lite/backend tiers**: repository datasources currently *reject* shell-less
     workspace backends; a KB datasource is query-only in v1
     (index-backed `search_knowledge`/`kb_query`, no clone, no filesystem reads) on
     every tier. It must therefore stay out of every `type=="repository"` tier gate.
   - **Schema/persistence**: `datasources.config` is non-null JSONB with `{}` as its
     default and `root_path` as the only v1 KB key. Do NOT overload
     `credentials`; `type="kb"` already carries the OKF meaning. Keep `kb` out of
     managed-connector credential withholding: its credentials are consumed by the
     orchestrator indexer and never included in the agent dispatch payload.
   - **Auto-attach is deferred, not abandoned**: the native project KB remains the
     implicit `project_id` binding during this slice. After external KBs are proven, a
     migration creates one `type="kb"` datasource per project pointing at the jobs repo
     with `root_path="knowledge"`, rekeys/rebuilds the index, and makes that binding
     required. This avoids combining external ingestion with a live native-KB migration.
   - **Cockpit**: the OKF form reuses repository URL/branch/token/SSH inputs, adds the
     root path, exposes pending/indexing/ready/partial/failed state and reindex actions,
     fixes external KB project access to read-only, and leaves `kb` selectable on lite
     backends while clone-based `repository` remains disabled there.
   - **Embedding/runtime identity**: the orchestrator indexer and KB query store share a
     dedicated system `KB_EMBEDDING_*` profile. Personal memory keeps the user's separate
     embedding preference; the system KB key is scoped to jobs/sessions that actually
     bind knowledge and is authoritatively cleared on reused unscoped agents. The
     row-level `embedding_version` includes a secret-free fingerprint of the effective
     provider, normalized base URL, and catalog endpoint identity, so a same-name model
     rerouted to a different transport cannot query stale vectors. API keys never enter
     that fingerprint.
   - **Deletion ordering**: request-spawned rebuilds are canceled/drained, then deletion
     holds the same per-KB local/advisory claim across vector cleanup and app-row removal.
     A stale remote sweeper checks the app row only after taking the claim, preventing
     post-delete watermark/note resurrection.

### 11.1 First production night (2026-07-04) — run-8 evidence

The first loop run on the deployed image (project `68137e29` "Better Resavio", 4 jobs
scholar→critic→developer→scholar, 21:38Z–05:37Z, image `sha-0b71ff3`, all completed
clean, all four `retros/` written with `merge_status: merged`). What it proved, and what
it exposed — this section is the worklist for the **optimization phase**.

**Proved:**

- **Dual-write works at scale.** 151 OKF files under `knowledge/` on `main` from one
  night. Spot-checked format: frontmatter correct (`type`, `description`, tags,
  confidence, provenance `author`/`job`/`branch`), standard markdown links throughout.
- **The files ARE the graph — the design's central bet, confirmed empirically.**
  Opening `knowledge/` in Obsidian renders a densely connected note-to-note topology:
  hubs are the load-bearing notes (proposals, the critic verdict, evidence anchors),
  clusters follow iterations/roles — structure the agents *authored* via markdown
  links. Contrast: the app-wide Neo4j graph (all projects, all jobs, all time) contains
  **292 relationships total, every one of them `TAGGED` (190) or `HAS_KEYWORD` (102) —
  zero note→note edges**. The "graph database" was an inverted index rendered as
  disconnected tag-starbursts; four jobs of OKF files produced more relational
  structure than the entire Neo4j corpus. This hardens §9.4 and the TL;DR stance:
  Neo4j is at best a *derived view* over the vault (slice 3), and retiring it for KBs
  outright is now a live option. It also makes the vault itself a demo-able
  deliverable: "every project produces a browsable, interlinked Obsidian vault."
- The historical failure mode has inverted: knowledge used to be *disconnected*
  (existence problem); it is now *overconnected* (tuning problem). The remaining work
  is pruning, not plumbing.

**Exposed (= the gate/gardener's tuning corpus, in priority order):**

1. **Slug-collision twins** — at least 5 pairs of same-title notes where `kb_write`
   minted a hash-suffixed sibling (`…-aa73e2.md`, `…-dafaa2.md`, `…-e556f3.md`,
   `…-8cf359.md`, `…-9639cc.md`) instead of updating. Exactly the UPDATE/DISCARD
   verdict path of PR2; the collision-suffix branch in `kb_write` should arguably
   *route to the gate even when the gate is otherwise off* — a same-slug write is a
   free, embedding-less duplicate signal. First target for the scoped gate test.
2. **Curator note bloat** — auto-curated "learning"/"retrospective" dumps up to
   **131 KB per note**; pgvector rows grew 374→648 in one night, still dominated by
   learning (224) + retrospective (149). Candidates: a note-size lint rule
   (loud-flag above ~15 KB, mirroring `render_index_md`'s loud-truncation posture) and
   curator-prompt direction toward distillation (the deferred "gardening direction"
   polish from PR2).
3. **Double-H1 serializer bug** — `_render_note_md` emits `# <slug>` and the body
   begins with the same H1 → every rendered note carries its title twice.
   Small fix in the renderer (skip the heading when the body already starts with one)
   + a `kb_lint` rule so hand-authored notes are covered.
4. **Numbering chaos persists** (loop F34) — one night produced self-labels iter-3,
   iteration-4, iteration-27, loop-10 and loop-16. Not an OKF problem to *solve*, but
   the vault inherits it in slugs; `retros/` NNN is the authoritative counter and
   curator/lint guidance should push notes to reference it.

**Sequencing consequence:** the scoped gate test (flip `curator.enabled` +
`curate_knowledge.verdict` on a test project) moves ahead of everything else — the
corpus to tune against now exists. Success criterion from the graph, not just the row
counts: **hubs survive, the twin/fuzz thins out** — the graph should get *sharper*,
not merely smaller. The `kb_lint` near-duplicate rule (still owed from slice 2) gets
tuned against the same corpus. Slice 3 follows unchanged; its Neo4j question now has
an evidence-backed answer.

**Addendum 2026-07-04/05 — the worklist above is now worked:**

- Item 1 (slug twins): hardening batch `e1183757` (deterministic content-hash
  collision suffix + idempotent re-check in `create_note`; exact-content no-op
  short-circuit in `kb_write` reaching every writer). **Live evidence the stack
  works**: iter-9's curator wrote the same "Phase 0 strategic archive finding"
  twice 42 s apart — the gate saw the first as a neighbour (pgvector upsert is
  synchronous; `find_similar_many` has no job filter, so same-job notes ARE
  visible) and SUPERSEDE'd it at write time. The only both-active twins left in
  the corpus predate the gate (06-27, 07-02 legacy pairs, genuinely different
  content). Residual hole: a **bare** (ungated) writer's same-slug-different-content
  collision still mints a silent suffixed twin — covered by detection, not
  prevention: the new `slug-forked` lint rule (base + 6-hex-suffix sibling, both
  active) flags them for the gardener.
- Item 2 (curator bloat): `oversized-note` lint rule (loud WARNING above ~15 KB,
  mirroring `render_index_md`'s posture) + "Distill, Don't Dump" section and a
  **Garden** workflow step (run `kb_lint`, merge/supersede twins, `kb_index`) in
  all five curation prompt forks (base/gpt_5/deepseek/gemma/gpt_oss).
- Item 3 (double-H1): fixed in `e1183757` (renderer skips the prepended title
  when the body opens with an H1; `duplicate-h1` lint covers legacy files).
- Slice-2 deferred rules: `near-duplicate` (embedding-backed — one active-only
  pgvector self-join via `KnowledgeStore.find_near_duplicate_pairs`, 0.9 floor vs
  the verdict's 0.6 fetch floor; pure formatter in the gardener keeps the engine
  DB-free; non-fatal when the index is unreachable) and `dead-external-url`
  (opt-in `check_urls` arg — stdlib HEAD probe, flags only clear negatives
  404/410/unreachable, 25-URL cap reported loudly as `url-sweep-truncated`).
- Item 4 (numbering chaos) remains open — an orchestrator/prompt concern, not an
  OKF one.

Live verification of this batch is OWED — runbook with known-answer fixtures and
success criteria: `tests/okf_kb_slice2_straggler_validation.md`. **Sequencing
corrected 2026-07-05: slice 3 does NOT wait on this runbook.** The 0.9
near-duplicate floor is a parameter, not architecture — and slice 3 re-embeds the
corpus (chunked, breadcrumb-prefixed, new `embedding_version`), shifting similarity
distributions, so the floor is re-judged *after* the re-embed with the same
self-join SQL. The runbook's only slice-3 gate is the offline frontmatter status
audit (§1+2 — promoted into the slice-3 entry above as migration hygiene, zero
jobs needed); the curator Garden E2E validates prompts, not slice-3 design, and
accumulates passively while the loop runs.

## Open questions

- **Datasource shape — RESOLVED 2026-07-11:** a first-class `kb` type, sharing
  repository credential validation but owning its indexing/tool semantics.
- **Org-vault write policy — RESOLVED FOR V1 2026-07-11:** every external KB datasource
  is read-only, including owner/global use. Credentials remain orchestrator-side and
  `kb_write`/`kb_update` target only the native project KB. External write-back gets a
  separate branch/PR design after read-only ingestion is proven.
- **Multi-KB note addressing — RESOLVED 2026-07-11:** source-qualified
  `kb-alias:note-slug` handles; unqualified reads must resolve uniquely. Search/list
  return the source for every result and accept an optional KB selector.
- **Where the default project KB lives** — RESOLVED 2026-07-03 by v2 shipping: the jobs
  repo is already the coordination repo and `retros/` already lives on its `main`, so the
  default project KB is `knowledge/` in the jobs repo. The general shape: **a KB root =
  a (repo, path-prefix) pair** — the toolset takes a root, so standalone KB-datasource
  repos (root = repo top level) are the same thing to every tool. No entangling: the KB
  is *content* on the coordination repo, not a coupling to it.
- **Merge gate** — RESOLVED: merge-then-flag, matching v2's implemented merge-failure
  posture (flag + continue; loss visible, never silent). The gardener sweeps.
- **Embedding locality** — RESOLVED 2026-07-03: shared pgvector tables with `kb_id`
  scoping (the code audit confirms it's consistent with current usage; the RRF
  functions take the scope as a parameter).
- **Note volume**: dual-write makes today's KB noise (F33/F38 — 374 notes in one run-6
  night) *visible* as files on `main`. That's a feature (you can finally see and prune
  it), but expect a fat `knowledge/` tree until the gardener (slice 2) ships. Loop jobs
  run bare (no curator), so loop-night volume is agent-authored only.
  **CONFIRMED 2026-07-04 (§11.1)**: 151 files in one night, slug-collision twins and
  131 KB dumps included — visible, prunable, and now the gate's tuning corpus.

## 12. Research basis (2026-07-03)

Six-agent sweep (three codebase audits, three web); findings are folded into the
sections above. The load-bearing sources:

- **OKF is Google Cloud's spec** — v0.1 Draft, published 2026-06-12, Apache-2.0
  (github.com/GoogleCloudPlatform/knowledge-catalog, `okf/SPEC.md`; lineage from
  Karpathy's LLM-wiki gist). One required frontmatter field (`type`, deliberately
  free-form), and an explicit extension clause ("consumers SHOULD NOT reject documents
  with unrecognized fields") — every extra field we add is spec-legal. The spec has
  **no rename/alias mechanism**, so our frontmatter `id` fills a real hole with private
  semantics. Binding consequences adopted here: body links are **standard markdown
  links** (the emergent graph — wikilinks appear nowhere in the spec), `index.md` has a
  mandated shape (no frontmatter; heading-grouped `[Title](url) - description` bullets),
  and the *reserved* things are the filenames `index.md`/`log.md` (the optional fields
  are "recommended"). Ecosystem: reference agent + visualizer in-repo, third-party
  validator (okf.site/validator) and conformance suite.
- **The architecture is org-roam v2** (mandatory stable ids + a disposable SQLite cache
  over plain files, stable since 2021; the id move *deleted* ~3k net LOC —
  blog.jethro.dev/posts/org_roam_v2) **and Khoj** (heading-aware chunks + pgvector +
  content-hash gating — a production near-isomorph of §5.1). **Logseq's retreat from
  files-canonical** was driven by block-granular real-time collaboration — orthogonal to
  note-files + tool-mediated writes + git-as-sync; their still-unfinished bidirectional
  DB⇄MD sync promise is evidence *for* §4's ban, not against files-canonical.
  SilverBullet thrives on the same invariant we adopted ("the truth remains in the
  markdown… flushed at any time and rebuilt").
- **Watermark indexing is GitLab's shipped design** (per-project last-commit SHA +
  incremental diff + the escape-hatch full reindex their troubleshooting docs show gets
  used); blob-SHA dedup is GitHub Blackbird's; force-push→rebuild *and*
  rebuild-on-config-change are Zoekt's. Dataview→Datacore is the counterexample we
  avoid (non-persistent index → startup rescans + stale renders).
- **Neo4j-dormant matches the 2026 GraphRAG consensus**: graph structure pays only on
  multi-hop/relational query classes (suggested adoption bar: >15 % of traffic), and our
  link graph is *explicit* — we skip GraphRAG's dominant cost (LLM entity extraction)
  entirely. "Agent follows backlinks" is the literature's recommended low-commitment
  alternative, which the 1-hop link table + a file-reading agent already implements.
- **Agent-memory field (2026)**: Letta's MemFS (git-backed markdown memory projection)
  is convergent evolution; **mem0's April-2026 retreat** from verdict-gating to add-only
  (docs.mem0.ai/migration/oss-v2-to-v3) is the economics caveat that doesn't apply at
  curated-note volume; Zep/Graphiti's bi-temporal edge invalidation
  (arXiv:2501.13956) is where `invalidated_at` comes from; TMA-NM (arXiv:2606.24322)
  supplies §10's laundering rule. **Nobody in the field combines verdict-gated ingestion
  with a markdown-git-canonical store — that combination is ours.**
- **Portability verdict**: markdown+frontmatter is the consensus for knowledge
  interchange (vendor memory APIs are the lock-in anti-pattern — mem0 export is a lossy
  JSON summary; Zep's migration guide is "re-ingest raw messages"). The checklist our
  format already satisfies: CommonMark links by path, OKF-recommended fields, stable
  ids, in-note provenance, relative-path attachments, §6-shaped indexes, **no embeddings
  in the repo** (they never travel; importers re-embed — the disposable-index principle
  guarantees this).

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
