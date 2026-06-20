---
tags:
  - feature
  - architecture
  - citation-engine
  - data-management
  - tool-development
  - cloud-infrastructure
  - orchestrator
aliases:
  - Citation Engine Integration
  - Native Citation Engine
  - citation engine deep integration
related:
  - citation_engine_roadmap.md
  - citation_engine_rework.md
  - citation_issues.md
  - agent_memory_overhaul.md
  - database_architecture.md
  - no_workspace_agent_mode.md
  - main_cloud_abstraction.md
status: phase-3-implemented
date: 2026-06-19
updated: 2026-06-20
---

# Citation Engine — Native SRW Integration

## Summary

The citation engine has been **folded into the repo** (vendored at `./citation_engine/`,
floating git pin removed, host owns the schema, tests relocated, k3d round-trip
verified). It *was* still built like a standalone library — its own synchronous
DB connection with SQLite/Postgres dual modes, its own LLM client for
verification, its own embedding stack, and a schema duality. This document
covers turning it into a **first-class SRW subsystem** that uses SRW's native
infrastructure for every one of those concerns. **Phases 1–3 are now
implemented** — see *Implementation status* below.

This is **orthogonal to** the existing citation docs:
[[citation_engine_roadmap]] and [[citation_engine_rework]] describe the engine's
*feature set* (shared source library, annotations/tags, vector + hybrid search);
[[citation_issues]] is the verification *research*. This doc is about the
*plumbing*: how the engine plugs into SRW's DB, auxiliary-LLM, and cloud layers.
The feature set and the verification approach are unchanged.

## Implementation status (2026-06-20)

| Phase | Scope | Status |
|---|---|---|
| **1** | DB collapse + async-native on the `srw_vector` pool; SQLite / `mode` / `psycopg2` / separate DSN + embedding stacks deleted; chunk embeddings on SRW's async `get_embedding_service()` | ✅ shipped + k3d-verified |
| **2a** | Verifier → auxiliary-LLM (`VerifyCitationTask`); `cite_*` returns `pending` and self-schedules async write-back; D6 model slot (`CITATION_LLM_*` moved engine→agent); `AuxHealth` + `verify_citations` config gate | ✅ shipped + k3d-verified |
| **2b** | D4 feedback loop (worker): execute node re-injects still-`failed` citations each turn; `archive_phase` boundary reconcile; DB-driven + self-resolving | ✅ shipped + k3d-verified |
| **3a** | Cloud anchor capture (D7, agent-side): `webdav_read` captures the snapshot-anchor (etag, raw-bytes `file_sha256`, `webdav_url`, backend); `cite_document` threads it onto the source's `metadata.cloud` (JSONB — no migration) | ✅ shipped + k3d-verified |
| **3b** | Original-bytes snapshot: `POST /api/citations/snapshot` (internal-key) persists the raw file to `srw-snapshots` content-addressed (`citations/<sha[:2]>/<sha>`, HEAD-dedup) → `snapshot_blob_key` on `metadata.cloud`; agent uploads via `OrchestratorClient` at cite-time (it has no S3 creds) | ✅ shipped + k3d-verified |
| **3c** | On-view drift check + view-original: `GET /api/citations/{id}/drift` (viewing-user auth) re-fetches the live source via MainCloud **only when it's provably under the viewer's own cloud home**, hash-compares → `unchanged`/`changed`/`unreachable`; `GET /api/citations/{id}/snapshot` streams the backup via `get_blob`; cockpit UI deferred | ✅ shipped + k3d-verified |

Verified by the gated async Postgres round-trip
(`tests/citation_engine/test_integration_postgres.py`, 5/5 vs the dev
`srw_vector`) + unit suites (`tests/test_citation_feedback_injection.py`,
`TestEditCitationTool`, the graph tests). A full live agent-job run is the
remaining end-to-end check for the 2b injection.

**Deferred follow-ons:** persistent-session feedback injection (sessions already
*verify* citations; only the feedback inject routes differently — via the
MemoryManager seam); a cockpit citations view; semantic chunking restored behind
an async chunker; the `oc:fileid` durable cloud link. Per-phase detail +
acceptance criteria are in *Phasing & acceptance criteria* below; the original
design rationale (Motivation / Design) is kept as the record.

## Motivation

A standalone-shaped library inside an async, service-oriented agent platform is a
set of latent mismatches:

- **Sync DB in an async loop.** The engine opens a per-instance synchronous
  `psycopg2` connection (`citation_engine/engine.py:282`); SRW agents run on an
  async event loop with a managed `asyncpg` vector pool. Every citation tool call
  blocks the loop.
- **A second, parallel DB config.** The engine resolves its own DSN from
  `CITATION_POSTGRES_*` / `CITATION_DB_URL` (`src/utils/db_url.py`,
  `src/utils/citation_utils.py:40`). On the dev cluster this is actively
  mis-wired: `CITATION_POSTGRES_DB=citation_engine` points at a database that
  does not exist, no `CITATION_POSTGRES_*` creds are in the Secret, and the
  citation tables actually live in `srw_vector`. Citations cannot connect on dev
  today (pre-existing, surfaced during the fold-in verification).
- **A second LLM stack.** The engine carries its own model selection, provider
  detection, API key and reasoning config purely for citation verification
  (`citation_engine/engine.py:167–211`) — duplicating capability SRW already has
  in its auxiliary-LLM service.
- **Dual SQLite/Postgres modes.** ~72 lines and ~12 methods branch on
  `self._db_type == "sqlite"`; `schema.py` exists almost entirely to serve the
  duality. None of this earns its keep now that the engine only ever runs inside
  SRW against the vector store.
- **Citations can't reference what users actually care about.** Sources embed a
  copy of the extracted text; they can't point at a living document in a user's
  cloud (OpenCloud/Nextcloud) with a durable, clickable reference.

## Goals

1. **One database.** The engine always uses SRW's vector store (`srw_vector`),
   reached through SRW's managed async pool — no private connection, no separate
   citation DSN.
2. **Async-native.** The engine's data access is async on SRW's pool, not sync
   `psycopg2` off the event loop.
3. **Verifier on the auxiliary model.** Citation verification runs as an SRW
   auxiliary-LLM task, not a separate synchronous LLM call.
4. **Cloud-document citations.** A citation can reference a document in a user's
   cloud with a durable, re-fetchable, user-clickable reference.
5. **Remove the standalone path.** SQLite / `mode="basic"` and the dual schema
   are deleted; the engine is SRW-only (consistent with the earlier "no reuse"
   decision when we chose to fold the repo in).

## Non-goals

- Changing the citation **feature set** — annotations, tags, shared source
  library, semantic chunking, and hybrid (RRF) search all stay as built per
  [[citation_engine_roadmap]].
- Re-deriving the verification **methodology** — the judge / post-hoc grounding
  research in [[citation_issues]] stands; only its execution path changes.
- A reusable, externally-publishable citation library — explicitly abandoned.

## Decisions (locked 2026-06-19)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Async-native** on SRW's vector pool (not "keep sync `psycopg2`, run off-thread") | Do the integration properly; a sync data layer in the agent loop is a permanent footgun. The bigger lift, but the honest one. |
| D2 | **Async verification** — `cite_*` returns immediately with `verification_status=pending`; the auxiliary task writes the verdict back later (eventually-consistent) | Decouples the agent turn from verification latency; aligns with the "verification loops are costly" finding in [[citation_issues]]. |
| D3 | **Remove SQLite / standalone mode entirely** | The engine only runs inside SRW; the duality is dead weight. See the test-strategy consequence below. |
| D4 | **Verdict feedback = inject + reconcile.** A failed check is injected into the agent's workflow at its current position (async, mid-stream) **and** a batch step at a natural boundary (phase boundary / before completion) sweeps any still-`pending`/`failed` citations. | Combination per the design discussion: immediate correction when the agent can still act, with a boundary backstop for verdicts that land after it moved on. Resolves former open Q1 + Q4 (agent is the primary consumer). |
| D5 | **Citation sources are a distinct store, cross-referenceable** with the KB / RecallStore — not unified. | Provenance/verification is a different job than "what the agent learned"; merging muddies both. They can link to each other. |
| D6 | **The citation-verification model is a configurable slot** — its own default (Admin → Models, alongside chat/auxiliary) + per-job override via `config_override`; falls back to the auxiliary model when unset. | Lets the verification model be tuned / cost-controlled per job without hardcoding the aux model. |
| D7 | **Cloud-document citations use snapshot-as-anchor.** At cite-time, save a copy (extracted text in `content` for the verifier + the original file bytes in the `srw-snapshots` blob store for "view original") plus a best-effort live pointer (`path`/`webdav_url`) and an `etag` + `content_hash`. Verification runs against the copy; an on-view drift check warns + offers the backup if the live source changed. | The copy guarantees the citation, so a stable cloud file ID is *not* required — `oc:fileid` becomes a later link-durability enhancement, not a prerequisite. Dissolves the identity + credential-scoping problems; mirrors Zotero / Perma.cc. |

## Current state (per seam, grounded)

### DB layer
- `CitationEngine(mode=...)` chooses `basic` (SQLite) or `multi-agent` (Postgres)
  at construction (`engine.py:143–164`); `_connect_sqlite` / `_connect_postgresql`
  (`engine.py:264`, `:282`) open a **per-instance sync connection**.
- DSN comes from `get_citation_engine_config()` →
  `build_postgres_url("CITATION_POSTGRES", fallback_env="CITATION_DB_URL")`
  (`src/utils/citation_utils.py:40`, `src/utils/db_url.py:18`).
- `schema.py` ships `SQLITE_SCHEMA` + `POSTGRESQL_SCHEMA` + dual migrations;
  `_initialize_schema` already **no-ops in Postgres mode** (host owns the schema —
  done during the fold-in), so the Postgres DDL here is already dead.
- Authoritative schema: `orchestrator/database/migrations/vector/0001_initial.sql`
  (sources, citations, source_embeddings, job_sources, source_annotations,
  source_tags + the `source_type`/`confidence_level`/… enums).

### Verifier
- Own LLM stack at `engine.py:167–211`: `CITATION_LLM_URL`, `CITATION_LLM_MODEL`
  (default `gpt-4o-mini`), `CITATION_LLM_PROVIDER` (openai/groq/custom auto-detect),
  `CITATION_LLM_API_KEY`, `CITATION_REASONING_LEVEL`/`_REQUIRED`.
- `_verify_citation` runs **synchronously** inside `cite_*`. It appears to combine
  a deterministic quote-match (`similarity_score`, `matched_location`) with an LLM
  judgment — **confirm the exact split during implementation**; only the LLM part
  moves to the auxiliary model.

### SRW's auxiliary-LLM service (the target for the verifier)
- `src/services/auxiliary.py`: `AuxiliaryLLM.chain()` (single structured call) and
  `.agent()` (tool loop); typed `AuxTask` / `AuxAgentTask` base classes with an
  `output_schema`; `AuxHealth` escalates after 3 consecutive failures (directly
  addresses the recurring "aux tasks silently died" incidents).
- Config under `auxiliary:` in `config/defaults.yaml` / `persistent_defaults.yaml`
  (`AuxiliaryConfig` in `src/core/loader.py:1461`): model, base_url, temperature,
  timeout, and per-task `enabled` gates.
- **`IngestionVerdictTask`** (judge a candidate against neighbours → structured
  ADD/UPDATE/MERGE verdict) is a near-exact analog for citation verification.

### Sources & cloud
- A source **embeds full extracted text** + a SHA-256 `content_hash` for dedup
  (`engine.py` `add_doc_source` / `add_custom_source`; `models.py` `Source`).
- User cloud files are reachable only by **WebDAV path** today
  (`src/tools/webdav/tools.py`) — **no stable file ID or etag is exposed to
  agents**. Opaque `ProjectFolderHandle`/`SessionFolderHandle`
  (`orchestrator/services/cloud/handles.py`) survive moves and back the job
  cloud-mirror flow ([[cloud_collaboration_model]], [[job_cloud_export]]).

## Design

### Phase 1 — DB: always the vector store, async-native

- Drop the `mode` parameter, the `sqlite3` import, `_connect_sqlite`, and every
  `if self._db_type == "sqlite"` branch. The engine is always Postgres.
- Replace the per-instance `psycopg2` connection with SRW's **async vector pool**
  (the same pool the embeddings/memory layer already uses). Rewrite the engine's
  data-access methods `async`.
- Delete `schema.py`'s DDL and migration machinery (the host's
  `migrations/vector/` is authoritative; the Postgres init is already a no-op).
  Keep only constants still referenced by callers, if any.
- Retire the separate citation DSN: no more `CITATION_POSTGRES_*` /
  `CITATION_DB_URL` / `CITATION_POSTGRES_DB=citation_engine`. **This dissolves the
  dev mis-wiring** — there is one vector DB and citations live in it. Remove the
  now-dead config from the Helm `configmap`/values.
- No data migration: the tables already exist in `srw_vector`.

**Effort:** the largest piece — an async rewrite of the engine's data layer.

### Phase 2 — Verifier as an auxiliary-LLM task

- Delete the engine's LLM stack (`CITATION_LLM_*`, provider detection, key,
  reasoning config) and the synchronous LLM call inside `_verify_citation`.
- Add `VerifyCitationTask(AuxTask)` + a `CitationVerdict` output schema in
  `src/services/auxiliary.py`, modelled on `IngestionVerdictTask` (chain mode).
- **Async (D2):** `cite_*` writes the citation with `verification_status=pending`
  and schedules verification via the established aux pattern
  (`asyncio.create_task` / the memory-writer seam). When the verdict lands, the
  task updates `citations.verification_status` (+ notes) and records it to the
  audit trail. Verification inherits `AuxHealth`, so silent aux outages surface.
- **Feedback (D4):** on a failed verdict, enqueue a feedback message injected into
  the agent's workflow at its current position — worker: a pending-feedback check
  at the execute node (the worker is `ainvoke`, so injection is "picked up next
  loop", not truly mid-call); persistent: injected into the turn stream. Reuse the
  existing message-injection seam (cf. memory injection / `blocking_message`).
  Independently, a **batch reconcile** at a natural boundary (phase boundary /
  before completion) sweeps citations still `pending` or `failed` and hands them
  back to the agent to correct — the backstop for verdicts that land after the
  agent moved on. This is the [[citation_issues]] "audit tool" pattern.
- **Keep the deterministic part in the engine** — quote matching / similarity
  (`matched_location`, `similarity_score`) is not an LLM concern and stays inline;
  only the LLM judgment moves to aux. (Confirm the boundary in `_verify_citation`.)
- **Model slot (D6):** the verification model is resolved per-job from
  `config_override` → an Admin-set default (a new entry alongside chat/auxiliary
  in Admin → Models) → the auxiliary model as fallback. Add a `verify_citations`
  task under `auxiliary.tasks` (per-task `enabled`). Prompt lives in
  `config/prompts/` with model-family variants, per the prompt-matrix convention.
- Verification methodology follows [[citation_issues]] (the judge, post-hoc
  grounding) — unchanged, just relocated.

**Effort:** clean / moderate. Mostly additive in `auxiliary.py` + deletion in the
engine + a status-writeback path.

### Phase 3 — Cloud-document citations (snapshot-as-anchor)

A citation can reference a document in a user's cloud, made durable by **saving a
copy at cite-time**. The copy is the anchor; the live pointer is best-effort. This
dissolves the identity + credential-scoping problems (D7), so Phase 3 is specified
here — it does **not** need the separate design pass the earlier draft assumed.

**At cite-time, a cloud-document source stores:**
- **The copy (text + blob).** Extracted text in the existing `content` field (for
  the verifier + hybrid search) **and** the original file bytes in the
  `srw-snapshots` blob store (MinIO/S3) for the user-facing "view the original",
  referenced by a `snapshot_blob_key`.
- **A best-effort live pointer:** `{backend, path, webdav_url}` so the citation
  links back to the live file. Native `oc:fileid` is **deferred** — a later
  enhancement that lets the live link survive renames/moves; not required for
  citation integrity, since the copy carries it.
- **A drift snapshot:** the source's `etag` / last-modified **and** `content_hash`
  at cite-time.

**Verification (Phase 2 verifier):** runs against the saved copy in SRW storage —
**no cloud credentials at verify-time**, and immune to the job/creds having
expired. This is why D7 *simplifies* Phase 2 rather than complicating it.

**Drift detection (on view):** lazy, in the cockpit when someone opens the
citation. Cheap-then-certain — one PROPFIND to compare `etag`/last-modified first,
re-hash only if those differ. If the live document changed, show a **warning + the
backed-up original** (the version actually cited). If the viewer has **no access**
to that user's cloud file (e.g. a shared citation), fall back to the snapshot +
"live source not reachable from your account."

**Data model:** a cloud-document source is today's embedded-content source **+**
the live pointer, drift snapshot, and `snapshot_blob_key` — carried as `document`
+ structured `metadata` (no new enum value unless we want to filter on it).
`content_hash` dedup still applies: identical content reuses a source; an edited-
then-re-cited doc yields a new snapshot, correctly pinning that version.

**Re-fetch path:** the on-view drift check fetches via the *viewing user's*
session (cockpit → orchestrator → cloud), never the agent — so it never needs the
agent's expired creds. Reuses [[main_cloud_abstraction]] /
[[cloud_collaboration_model]] / [[rclone_cloud_mount]].

**Data-residency note:** this means SRW **stores copies of user cloud documents**
(text + original bytes). Acceptable — arguably expected — for a deep-cloud-
integration product; `content_hash` dedup bounds growth; snapshot-blob retention
should follow the same policy as other `srw-snapshots` artifacts.

Depends on a clean source model from Phase 1.

## Test-strategy consequence (from D3)

Removing SQLite is not free for the tests. `tests/citation_engine/test_engine.py`
and `test_search.py` run against **SQLite `mode="basic"`** for fast, hermetic unit
testing, and `test_search.py` has explicit `test_hybrid_falls_back_to_keyword_on_sqlite`
/ `test_semantic_falls_back_to_keyword_on_sqlite` cases that **only exist because
SQLite lacks pgvector**. Once SQLite is gone:

- Those fallback code paths and their tests are retired.
- The remaining unit tests must move to Postgres (the relocated
  `test_integration_postgres.py` pattern, gated + run against the k3d/dev vector
  DB per `tests/citation_engine/README.md`) or be rewritten with a mocked data
  layer. Net effect: the engine loses its zero-infra unit-test path; CI's
  fast in-process citation tests shrink.

This is a known, accepted consequence of D3, called out so it's planned, not
discovered.

## Phasing & acceptance criteria

**Phase 1 — DB collapse + async** *(foundational; also retires the dev config bug)*
— ✅ **Implemented + k3d-verified 2026-06-20.**
- No `sqlite`/`mode`/`_db_type` branching remains in the engine; `schema.py`,
  `embeddings.py`, and the standalone `tool.py` were deleted. ✅
- Engine performs all I/O async on SRW's vector pool (`agent.vector_conn`,
  threaded through `ToolContext.vector_db`); no `psycopg2` connection. The
  citation tool wrappers (`src/tools/citation/sources.py`), the background doc
  registration (`agent.py`), and the paper-download registration
  (`tools/research/papers.py`) are all `async`. ✅
- `CITATION_POSTGRES_*` / `citation_engine`-DB config removed from code + Helm +
  compose; the dead `src/utils/citation_utils.py` was removed. ✅
- Existing citation tools work; the async k3d round-trip
  (`tests/citation_engine/test_integration_postgres.py`, per the README) passed
  3/3 against the real `srw_vector` schema; `TestEditCitationTool` (6) +
  `tests/test_graph.py` (66) green. ✅

  **Two in-scope deviations, both deliberate:**
  - **Embedding stack also collapsed.** The engine's own `CITATION_EMBEDDING_*`
    stack was entangled with the async data layer, so it now shares SRW's async
    `get_embedding_service()` (4096-dim `qwen3-embedding-8b`, matching the host
    `source_embeddings.embedding vector(4096)` column). One model, one config.
  - **Semantic chunking → fixed-size (temporary).** `SemanticChunker` needs a
    sync embedder; rather than block on an async chunker, chunking is fixed-size
    for now (search still works on fixed chunks). Restoring semantic chunking
    behind an async embed is a tracked follow-up.

  **Dev-cluster gotcha found + fixed:** on k3d the `srw` pgvector role's
  scram password had drifted from the declared `dev_pgvector_password` secret
  (masked everywhere by pg_hba's `127.0.0.1 → trust` rule; `port-forward svc/…`
  arrives via pod-IP → scram). `ALTER ROLE srw WITH PASSWORD …` realigned it —
  this also unblocks the orchestrator's own vector-DB (memory/KB) connections,
  which use the same scram path.

**Phase 2 — Verifier → aux** — split into **2a (✅ shipped + k3d-verified
2026-06-20)** and **2b (the D4 feedback loop, not yet started)**.

Phase 2a (done):
- The engine's own LLM client/stack is gone (`_setup_llm_client`,
  `_verify_citation`, the prompt builders, `_resolve_llm_provider`, the GROQ
  constants, the `__init__` LLM config). The engine owns no model. ✅
- Verification runs via `AuxiliaryLLM` — a new `VerifyCitationTask` +
  `CitationVerdict` schema + `verify_and_store_citation` helper in
  `src/services/auxiliary.py`, modelled on `IngestionVerdictTask`. ✅
- `cite_*` returns `pending` immediately; the engine **self-schedules**
  background verification (`asyncio.create_task`, tasks tracked in
  `_verify_tasks`), which writes `verification_status` (+ notes/score) back
  async (D2). The verification LLM call is recorded to the audit trail via the
  aux archiver (`call_type=citation_verification`). ✅
- **Model slot (D6):** the agent builds the verifier from `CITATION_LLM_MODEL`
  (orchestrator-dispatched from per-job `config_override` → Admin default),
  falling back to the auxiliary model when unset. Note: `CITATION_LLM_*` is
  **not** removed — it moved from the engine to the agent's verifier builder
  (`_initialize_citation_verifier`); it *is* the model slot. ✅
- `AuxHealth` surfaces sustained verification failures; gated by
  `auxiliary.tasks.verify_citations` (default on). An aux *outage* leaves the
  row `pending` (only a real negative verdict sets `failed`) — the Phase-2b
  reconcile is the backstop. ✅

Phase 2b — feedback loop (D4) — **✅ worker shipped + k3d-verified 2026-06-20;
persistent deferred**:
- The worker execute node re-reads `verification_status` each turn and injects
  any still-`failed` citations as a synthetic `check_citation_verification`
  tool-result (`src/core/citation_feedback_injection.py`), telling the agent to
  edit/remove them. **DB-driven, so it self-resolves**: editing a citation resets
  it to `pending` → re-verifies → it drops out of the injected block next turn.
  Excluded from summarization via the unified `is_workspace_injection_message`. ✅
- Boundary reconcile: `archive_phase` awaits in-flight verdicts
  (`engine.await_pending_verifications`) so failures surface in the next phase. ✅
- This realises the **inject + reconcile** combination of D4: the per-turn
  re-injection *is* the sweep (failed citations resurface until fixed), and the
  boundary await flushes verdicts still computing at the phase edge. No queue /
  engine sink needed — the DB row's `verification_status` is the source of truth.
- Verified at the component level on k3d (negative verdict → `failed` →
  `list_citations(failed)` → `format_failed_citations`) + unit tests for the
  injection helpers + the 66-test graph suite; a full live agent-job run is the
  remaining end-to-end check.
- **Deferred — persistent-session feedback injection.** Persistent sessions
  already *verify* citations (2a wired `verify_aux` on their ToolContext), but
  their turn messages are assembled via the MemoryManager seam, not the worker's
  `_inject_transient_messages`, so the feedback injection there is a separate,
  clean follow-on. (A cockpit citations view remains the other deferred reader.)

**Phase 3 — Cloud citations** — split into **3a (✅ shipped + k3d-verified
2026-06-20)**, **3b (✅ shipped + k3d-verified 2026-06-20)**, and **3c (✅
shipped + k3d-verified 2026-06-20)**, mirroring the 2a/2b split.

Grounding found during implementation: agents reach cloud files two ways —
(1) a **WebDAV datasource** (`src/tools/webdav/tools.py`; the worker cite path,
where `client.info()` exposes the `etag` that `webdav_read` was discarding), and
(2) **rclone `cloud_mount`** at `/workspace/cloud` (persistent sessions only;
main-cloud files appear as plain FS paths with no etag). The `srw-snapshots`
blob store is **orchestrator-only** (the agent has no S3 creds), so the
original-bytes snapshot must go through an orchestrator endpoint. Phase 3 targets
the WebDAV-datasource worker path first (consistent with 2b's worker-first
scoping).

Phase 3a — cloud anchor capture (agent-side, done):
- `webdav_read` best-effort captures a snapshot-anchor — `{backend, path,
  webdav_url, etag, modified, content_type, size, file_sha256, captured_at}` —
  via `client.info()` + the client hostname + a raw-bytes SHA-256, and stashes it
  on `ToolContext._cloud_anchors` keyed by the resolved local path. Capture never
  raises (a metadata failure can't break a download). ✅
- `cite_document` looks the anchor up by resolved path and threads it through
  `get_or_register_doc_source(cloud_metadata=…)` →
  `add_doc_source(metadata={"cloud": …})`. The source now records the live
  pointer + drift fingerprint of what it actually cited. ✅
- **No engine change, no migration** — `metadata` is JSONB and round-trips via
  `_register_source` / `_row_to_source` / `_loads`. The text copy (`content`) +
  `content_hash` dedup are unchanged, so the Phase 2 verifier already verifies
  against the saved copy with no cloud creds. ✅
- Tests: `tests/test_cloud_citation_anchor.py` (11, CI — anchor build, stash
  normalization, metadata threading) + integration
  `test_cloud_anchor_metadata_persists` (6/6 vs k3d `srw_vector`) + the 66-test
  graph suite. ✅

Phase 3b — original-bytes snapshot (done):
- `SnapshotService.save_blob` / `get_blob` store raw bytes content-addressed
  under `citations/<sha[:2]>/<sha>` in `srw-snapshots` (HEAD-dedup, so identical
  bytes never re-upload). ✅
- `POST /api/citations/snapshot` (internal-key auth via `require_internal`) reads
  the raw request body + `?content_type`, calls `save_blob`, and returns
  `{snapshot_blob_key, size_bytes}`. The agent has no S3 credentials, so this is
  how the bytes get persisted. ✅
- `OrchestratorClient.save_citation_snapshot` (agent) POSTs the bytes;
  `ToolContext.snapshot_cloud_source_bytes` reads the cited file, uploads, and
  writes `snapshot_blob_key` back onto the anchor **before** registration, so the
  source is registered with the key already on `metadata.cloud`. Best-effort —
  a failed/unavailable store leaves the citation intact (text copy is the
  verification anchor); the tool reports "live pointer recorded" vs "original
  snapshotted" accordingly. ✅
- Tests: `tests/test_citation_snapshot_blob.py` (13, CI — `save_blob` put/dedup,
  `get_blob`, `save_citation_snapshot` 200/error/network/empty,
  `snapshot_cloud_source_bytes` upload/short-circuit/no-client/missing-file).
  Live k3d: `require_internal` 401 gate → authed 200 + content-addressed key →
  identical re-POST returns the same key → boto3 read-back confirms exact bytes +
  `application/pdf` ContentType in real MinIO. ✅

Phase 3c — on-view drift check + view-original (done):
- `GET /api/citations/{id}/snapshot` (viewing-user auth: `require_approved_user`
  + `user_can_access_any_job`, 404-on-no-access so existence isn't leaked) streams
  the backed-up original via `SnapshotService.get_blob(snapshot_blob_key)` with
  the stored `content_type` + `inline` Content-Disposition. ✅
- `GET /api/citations/{id}/drift` returns the cite-time fingerprint +
  `snapshot_available`, and runs a **best-effort** live re-fetch **only when the
  cited file is provably inside the viewing user's own cloud home** —
  `_home_relative_path` guards on the anchor `webdav_url` being a prefix of the
  user's `UserHome.webdav_url` (so the creds are the user's, never the agent's,
  and we never compare a same-named different file), then
  `get_project_folder_file_bytes` + SHA-256 compare → `unchanged` / `changed`.
  External datasource / different cloud / no access → `unreachable` (the spec's
  "fall back to the snapshot" branch). ✅
- Tests: `tests/test_citation_drift_helpers.py` (8, CI — `_home_relative_path`
  under/not-under/host-mismatch/trailing-slash, `_source_cloud_meta` dict/string/
  no-block). Live k3d (real orchestrator + MinIO, authed as the test user via
  `X-Internal-Key`+`X-MCP-User-Id`): `/snapshot` → 200 + exact bytes +
  `application/pdf` + `inline` filename; `/drift` → `unreachable` +
  `snapshot_available:true` + echoed fingerprint; no user header → 401. ✅
- **Deferred (same bar as prior slices):** the `unchanged`/`changed` live path
  needs a citation whose source is a real file under the viewing user's
  OpenCloud home — exercise on a full real-cloud agent run. **Cockpit citations
  view stays deferred** (the data layer + endpoints are now ready for it).

## Open questions & deferrals

Resolved this round (now **D4–D6**): verdict feedback = inject + batch reconcile;
sources stay distinct but cross-referenceable; the verification model is a
configurable slot. Consumers of the async verdict (former open Q4) follow from
those: the **agent** via injection + reconcile, the **DB** `verification_status`
+ the **audit trail** always, and the **cockpit** as the deferred reader below.

**Deferred (out of scope for this integration):**
- **Cockpit surfacing of citations + verification status.** With async status
  (`pending → verified/failed`) a live UI would need a notification channel
  (SSE/WS). The integration only needs the DB + audit to hold the status; a
  cockpit citations view is a worthwhile follow-on once the data layer is clean.

**Resolved (now D7):** the cloud-document reference model — snapshot-as-anchor
(save text + blob, best-effort pointer, on-view drift check). The earlier
"durable identifier" problem is sidestepped: the saved copy is the anchor, so a
stable cloud file ID (`oc:fileid`) is only a later link-durability enhancement,
not a prerequisite. **No open design questions remain across Phases 1–3.**

## Risks & migration notes

- **Async rewrite blast radius (Phase 1):** touches every engine data method.
  Mitigation: the relocated test suite + the proven k3d round-trip as the gate.
- **Lost zero-infra unit tests (D3):** see test-strategy consequence; plan the
  Postgres/mocked replacement as part of Phase 1.
- **Helm/config cleanup:** removing `CITATION_POSTGRES_*` and the
  `citation_engine` DB references must land with Phase 1, not lag it, to avoid
  leaving dangling config.
- **Verification semantics change (D2):** anything that assumed a synchronous
  verdict from `cite_*` must tolerate `pending`.

## Related

- [[citation_engine_roadmap]] / [[citation_engine_rework]] — the engine's feature
  history (built into the standalone package).
- [[citation_issues]] — verification research (the judge, post-hoc grounding).
- [[agent_memory_overhaul]] — auxiliary-LLM + RecallStore patterns the verifier
  reuses.
- [[database_architecture]] — the 4-store layout the vector DB lives in.
- [[main_cloud_abstraction]] / [[cloud_collaboration_model]] / [[job_cloud_export]]
  — cloud primitives Phase 3 builds on.
