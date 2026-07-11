# OKF Knowledge Base Datasource — Slice 4 Implementation Plan

> **Status:** implemented 2026-07-11; feature-scoped automated verification green,
> live deployment verification pending. Steps use
> checkbox (`- [x]`) syntax for tracking.

**Goal:** Let a user register an OKF Markdown repository (including an external
GitHub/GitLab/Gitea repository) as a first-class `kb` datasource, index it once under
the datasource UUID, attach it explicitly to jobs or sessions, and use the existing
knowledge search/read/list/injection tools without exposing repository credentials to
the agent.

**Architecture:** Slice 4 adds a control-plane binding around the files-canonical KB
substrate that already shipped. The app database stores a `kb` datasource whose stable
UUID is the external KB's `kb_id` and whose canonical location is `(connection_url,
default_branch, config.root_path)`. A provider-neutral orchestrator-side git reader
feeds the existing blob-SHA/watermark reindexer; Postgres/pgvector remains a disposable
index. At dispatch, only authorized KB ids and display metadata reach the agent — never
external-repository credentials. Runtime tools and passive retrieval search the native
project KB plus selected external ids and return source-qualified note handles.

**Tech stack:** FastAPI/Pydantic, asyncpg app + vector databases, git CLI in the
orchestrator image, existing `KnowledgeStore`/chunker/reindexer, Angular 21 signals,
Transloco, pytest/pytest-asyncio, Vitest.

**Parent design:**
[`docs/features/okf_knowledge_base.md`](../../features/okf_knowledge_base.md), especially
"Slice 4 decision lock (2026-07-11)" and §11 Slice 4.

## Implementation result (2026-07-11)

Tasks 0–6 are implemented in the working tree. The delivered path includes the two
migrations, provider-neutral disposable git snapshots, generalized root-aware indexing,
watermark status and HA claims, initial/manual/periodic lifecycle triggers, deletion
cleanup, credential-free dispatch bindings, qualified multi-KB tools, protected passive
retrieval, external-only/lite runtime support, and the Cockpit authoring/status flow.
The closeout audit additionally locked strict Git transport/auth isolation, coordinated
delete-vs-reindex ordering, same-id rename/duplicate-id safety, durable failure summaries,
and a dedicated system KB embedding profile distinct from personal-memory embeddings.

Automated evidence at closeout:

- 905 KB/knowledge/datasource Python tests passed.
- 636 injection/persistent/runtime/auth-equivalence tests passed.
- 893 Cockpit tests passed; i18n check and production build passed.
- Repository-wide Ruff lint, Helm lint, Compose rendering, Python compilation, and
  diff checks passed. The feature-touched Python files also pass Ruff formatting;
  the repository-wide format check still reports eight unrelated pre-existing files.
- The provider-neutral source's focused suite passed, including a local bare-repository
  integration fixture, secret-redaction assertions, timeout/cancellation cleanup, and a
  production orchestrator image build with Git 2.47.3.

Task 7's manual k3d/private-provider verification remains intentionally open; none of the
automated results below should be read as a deployed/live claim.

Rollout note: the effective-profile fingerprint intentionally changes the row-level
embedding stamp once for existing KBs. Trigger a full reindex during deployment (or wait
for the default 15-minute sweep); old-stamp vectors are excluded from retrieval until
their notes are rebuilt. If model weights change behind an otherwise unchanged
provider/model/endpoint identity, operators must request a full rebuild explicitly.

## Locked contract

These are implementation constraints, not questions to reopen inside the slice:

1. The formal/product name is **OKF Knowledge Base**. The datasource discriminator is
   `kb`; it is not a `repository` datasource with a format flag.
2. For an external KB, `datasources.id == kb_id`.
3. `datasources.config.root_path` is the only KB-specific v1 config key. It is a
   normalized relative POSIX path, default `""`, with no absolute path or `..` segment.
4. External KBs use explicit-only datasource selection. Project linkage controls
   eligibility; it does not auto-attach them.
5. The native project KB remains an implicit, writable `project_id` binding. Migrating
   it to an auto-created datasource is a later slice.
6. External KBs are read-only in v1. Their credentials stay inside the orchestrator;
   they are not cloned into agent workspaces. `kb_write`/`kb_update` continue to target
   only the native project KB.
7. External ingestion is orchestrator-owned and provider-neutral. Ship initial/manual/
   periodic polling; no provider webhooks in v1.
8. Every multi-KB result carries its source. Canonical handles are
   `kb-alias:note-slug`; unqualified slugs must resolve uniquely.
9. Lite/no-shell workspaces have the same external-KB query surface as full workspaces.
10. Neo4j is optional and never canonical; this slice adds no graph tier.
11. Tokens/passwords use HTTPS only. SSH/SCP requires an explicit deploy key and cannot
    inherit an orchestrator SSH agent, identity, config, proxy, or password prompt. Every
    network remote must match an exact trusted host/port; arbitrary internal hosts,
    loopback/IP literals, wildcards, local paths, and executable helpers are rejected.
12. KB indexing/querying uses a dedicated system-owned embedding profile. It is sent only
    for a native or explicitly selected external KB; user memory keeps its own profile.
13. Datasource deletion and reindex share one local/cross-replica mutation claim, and
    stale work must re-check app-row liveness under that claim.
14. `indexed_commit` is the last fully reconciled commit, not a claim that every physical
    row is frozen there during a partial run. Read/search/injection output must show
    partial/indexing/failed status and attempted source HEAD alongside that watermark.
15. Attachment metadata is a selector, never a durable grant. Thread create/attach/resume
    and child-job creation re-authorize current project/datasource access; inherited
    selections are rechecked too. Internal job POSTs derive scope from a thread, parent,
    or authenticated MCP user and reject originless shared-key calls. A `kb` datasource
    cannot set legacy `job_id`; jobs attach it only through explicit selection.

## Non-goals

- External repository writes, pushes, branches, pull requests, or conflict resolution.
- Auto-provisioning/migrating the native project KB into `datasources`.
- GitHub/GitLab/Gitea webhooks.
- Indexing arbitrary PDFs, `.odt`, office documents, images, or attachments. V1 indexes
  Markdown notes under the configured OKF root.
- A new vector database, embedding model, chunker, reranker, or GraphRAG layer.
- Changing datasource visibility or the explicit-only job/session picker semantics.
- Editing `orchestrator/database/schema.sql` or `vector_schema.sql`; migrations only.

## As-built baseline and gaps

Already shipped:

- `src/tools/knowledge/knowledge_tools.py`: OKF serializer, native `knowledge/`
  dual-write, read tools, gardener tools, chunk search.
- `src/tools/knowledge/chunker.py`: structural chunking and embedding-version stamp.
- `src/services/knowledge_store.py`: `kb_id`-scoped note/chunk/link rows, watermarks,
  multi-id chunk search.
- `orchestrator/services/kb_reindex.py`: git-tree/blob-SHA incremental reindexing for a
  project's managed Gitea jobs repo.
- Vector migrations `0008`–`0010`: per-KB paths/watermarks, chunks, chunk RRF, links.
- Datasource ownership/eligibility/explicit selection and repository credential
  encryption.

Gaps this plan closes:

- No `kb` datasource type or `datasources.config` column.
- Datasource SELECT/dispatch paths omit config; internal dispatch also omits the stable
  datasource id and currently sends repository credentials to agents.
- Reindexing accepts only `GiteaClient`, hardcodes `knowledge/`, and sweeps only
  `project_repositories(role='jobs')`.
- Worker and persistent initialization still construct `KnowledgeStore` only after a
  successful Neo4j connection.
- `ToolContext` carries project ids, not KB bindings; `KnowledgeRecord` drops `kb_id`.
- `kb_read` returns the first slug match across scopes and search output has no source.
- Worker/persistent passive injection paths are not consistently on chunk search and
  have no local-vs-external budget partition.
- Cockpit/API knowledge surfaces remain project-centric.

## File map

Expected new files:

- `orchestrator/database/migrations/app/0054_datasource_config.sql`
- `orchestrator/database/migrations/vector/0011_kb_watermark_status.sql`
- `orchestrator/services/kb_git_source.py`
- `orchestrator/services/kb_datasources.py`
- `src/services/knowledge/bindings.py`
- `tests/test_kb_git_source.py`
- `tests/test_kb_datasource_api.py`
- `tests/test_kb_bindings.py`
- `cockpit/src/app/views/datasources/datasource-list.component.spec.ts` (focused KB
  form/status cases; create only if the component remains practical to instantiate)

Expected modified files:

- `orchestrator/database/postgres.py`
- `orchestrator/database/schema_current.sql` (generated from migrations)
- `orchestrator/database/vector_schema_current.sql` (generated from migrations)
- `orchestrator/main.py`
- `orchestrator/services/kb_reindex.py`
- `orchestrator/mcp/server.py` and `orchestrator/mcp/client.py` if reindex/status are
  exposed through MCP
- `docker/Dockerfile.orchestrator`, `docker/Dockerfile.orchestrator.dev`
- `src/agent.py`, `src/api/persistent_session.py`, `src/api/persistent_app.py`
- `src/tools/context.py`
- `src/tools/knowledge/knowledge_tools.py`
- `src/services/knowledge_store.py`
- `src/graph.py`, `src/persistent_graph.py`
- `src/core/datasource_setup.py` (discovery copy only; no external KB clone)
- `cockpit/src/app/core/models/api.model.ts`
- `cockpit/src/app/core/services/api.service.ts`
- `cockpit/src/app/views/datasources/datasource-list.component.ts`
- `cockpit/src/app/views/agent-settings/datasources-group.component.ts`
- `cockpit/src/app/views/project-detail/project-detail.component.ts`
- `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`
- Existing datasource/knowledge/reindex tests listed per task below

---

## Task 0 — Finish the store-without-Neo4j runtime seam

Slice 3 made the tools tolerate `knowledge_graph=None`, but both worker and persistent
startup still create `KnowledgeStore` only inside `if kg.connect()`. External KBs must
work in deployments with Neo4j disabled and in sessions that have no native project id.

**Files:**

- Modify: `src/agent.py` (`_setup_job_tools` knowledge initialization)
- Modify: `src/api/persistent_session.py` (`_setup_knowledge`)
- Test: `tests/test_neo4j_import_guard.py`
- Test: `tests/test_persistent_session.py`
- Test: `tests/test_knowledge_tools.py`

- [x] Add failing worker and persistent tests proving a usable vector connection +
  embedding service creates `KnowledgeStore` when Neo4j connection fails/returns false.
- [x] Construct `EmbeddingService` + `KnowledgeStore` first. Connect
  `KnowledgeGraphDB` independently and install it only on success.
- [x] Change the setup guard from “has project ids” to “has at least one effective KB
  binding” once Task 3 supplies bindings; retain project-id behavior until then.
- [x] Keep degradation honest: missing vector/embedding marks `_kb_degraded`; missing
  Neo4j alone logs graph degradation and does not mark search/read unavailable.
- [x] Verify `ToolContext.has_knowledge()` remains store-only and graph tools continue
  their current honest degradation.

**Verification:**

```bash
pytest tests/test_neo4j_import_guard.py tests/test_persistent_session.py \
  tests/test_knowledge_tools.py -x -q --tb=short
ruff check src/agent.py src/api/persistent_session.py tests/
```

---

## Task 1 — Datasource schema, validation, persistence, and internal payload

Add the control-plane representation without indexing anything yet. This task is
additive and must leave every existing datasource type byte-for-byte compatible.

**Files:**

- Create: `orchestrator/database/migrations/app/0054_datasource_config.sql`
- Modify: `orchestrator/database/postgres.py`
- Modify: `orchestrator/main.py` (`DatasourceCreate`, `DatasourceUpdate`, CRUD,
  `_build_datasources_payload`, type validation)
- Modify: `orchestrator/security/access.py` only if config redaction needs an explicit
  allowlist (config must never contain credentials)
- Test: `tests/test_datasource_redesign.py`
- Test: `tests/test_datasource_access.py`
- Test: `tests/test_datasource_credentials_encryption.py`
- Create: `tests/test_kb_datasource_api.py`

### Contract

`0054_datasource_config.sql`:

```sql
ALTER TABLE datasources
    ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb;
```

Do not edit frozen schema snapshots. All datasource SELECT/RETURNING statements must
carry `config`; `_datasource_row_to_dict` leaves it as an ordinary dict.

KB validation helper:

```python
normalize_kb_config({"root_path": "docs/knowledge"})
# -> {"root_path": "docs/knowledge"}
```

Rules:

- Missing/blank root becomes `""`.
- Convert `\` to `/`, collapse duplicate separators and `.` segments.
- Reject absolute paths, URI-like roots, NUL, and every `..` segment.
- `type="kb"` requires `connection_url`; default branch remains optional and resolves
  to the remote default when absent.
- Reject repository URLs containing embedded userinfo/password/token material. KB
  secrets belong only in encrypted `credentials`, never in `connection_url`.
- Reject unknown v1 KB config keys rather than silently persisting misspellings.
- Config is non-secret. Tokens/keys remain in encrypted `credentials` only.

Internal dispatch rules:

- For `type="kb"`, include `datasource_id`, `name`, `description`, `type`,
  `default_branch`, normalized `config`, `project_read_only=True`.
- Set `credentials={}` and omit/neutralize `connection_url`; neither is needed by the
  agent because v1 does not clone external KBs.
- Existing datasource payloads retain their current shape.

- [x] Write migration-lint and CRUD tests first.
- [x] Add `config` to create/update/list/get/eligible/project/job/thread datasource SQL.
- [x] Add `kb` to valid API/frontend type contracts.
- [x] Implement strict `normalize_kb_config` and use it on create/update.
- [x] Preserve existing config when an update omits it; allow an explicit new root.
- [x] Add the credential-isolated KB dispatch payload and pin it with a test containing
  sentinel token/SSH values that must not appear anywhere in the payload.
- [x] Ensure REST redaction returns safe config and never credentials.

**Verification:**

```bash
pytest tests/test_kb_datasource_api.py tests/test_datasource_redesign.py \
  tests/test_datasource_access.py tests/test_datasource_credentials_encryption.py \
  -x -q --tb=short
scripts/schema-snapshot.sh app
ruff check orchestrator/main.py orchestrator/database/postgres.py tests/
```

---

## Task 2 — Provider-neutral, credential-safe git content source

Generalize the reindexer around a small source protocol rather than adding a GitHub API
client. A generic git transport supports public/private GitHub, GitLab, Gitea, and SSH
remotes with one path.

**Files:**

- Create: `orchestrator/services/kb_git_source.py`
- Modify: `docker/Dockerfile.orchestrator`
- Modify: `docker/Dockerfile.orchestrator.dev`
- Create: `tests/test_kb_git_source.py`
- Extend: `tests/test_datasource_repo_clone.py` only for shared credential-normalization
  helpers, if reused

### Source protocol

```python
class KnowledgeGitSnapshot(Protocol):
    async def list_tree(self) -> list[dict[str, str]]: ...
    async def get_file(self, path: str) -> str | None: ...


class KnowledgeGitSource(Protocol):
    async def get_head(self) -> str | None: ...
    def snapshot(self, ref: str) -> AsyncContextManager[KnowledgeGitSnapshot]: ...
    @property
    def label(self) -> str: ...
```

The reindexer calls `get_head()` first, then opens one snapshot context for the tree and
all changed-file reads. The remote adapter therefore creates at most one temporary clone
per changed run, and the context manager owns unconditional cleanup.

Implement two adapters:

- `GiteaKnowledgeGitSource`: a thin adapter over the existing `GiteaClient` for native
  project KBs. No behavior change.
- `RemoteKnowledgeGitSource`: git CLI against a disposable temporary bare/partial clone
  for external KB datasources.

Security requirements for `RemoteKnowledgeGitSource`:

- Use `asyncio.create_subprocess_exec`, never a shell command string.
- Never embed a token/password in argv, a remote URL, an exception, or a log line.
- HTTPS auth uses a temporary `GIT_ASKPASS` helper plus secret environment variables;
  SSH auth uses a mode-0600 temporary key and `GIT_SSH_COMMAND`/`IdentitiesOnly=yes`.
- Redact subprocess stderr before surfacing it; test with sentinel secrets.
- Delete temporary auth material and repository data in `finally` on success,
  cancellation, and failure.
- Set timeouts and output limits for `ls-remote`, clone/fetch, tree, and blob reads.
- Reject symlink/path escapes at the configured root; only Markdown blobs are passed to
  the parser.

Performance posture:

- Check remote HEAD with `git ls-remote` first. An unchanged watermark performs no
  clone/fetch.
- On change, prefer shallow partial clone/fetch (`--filter=blob:none`, no checkout);
  fall back cleanly when a server lacks partial-clone support.
- `git ls-tree -r` supplies `{path, blob_sha}`; `git show <ref>:<path>` fetches only
  changed Markdown bodies.
- The source is disposable. No persistent orchestrator filesystem is canonical.

- [x] Add `git` to both orchestrator runtime images.
- [x] Write fake-subprocess tests for public HTTPS, token, and SSH command/env shapes.
- [x] Add a local bare-repository integration test (no network) covering HEAD, nested
  tree paths, Unicode Markdown, deletion, and cleanup.
- [x] Implement both adapters and secret-redaction helpers.
- [x] Verify cancellation and timeout cleanup.

**Verification:**

```bash
pytest tests/test_kb_git_source.py -x -q --tb=short
ruff check orchestrator/services/kb_git_source.py tests/test_kb_git_source.py
docker build -f docker/Dockerfile.orchestrator -t srw-orchestrator:kb-source .
```

---

## Task 3 — Generalized reindex lifecycle, status, sweeper, and cleanup

Wire external sources into the existing chunk/blob/watermark pipeline. Index once per
datasource, regardless of how many projects/jobs attach it.

**Files:**

- Create: `orchestrator/database/migrations/vector/0011_kb_watermark_status.sql`
- Create: `orchestrator/services/kb_datasources.py`
- Modify: `orchestrator/services/kb_reindex.py`
- Modify: `src/services/knowledge_store.py`
- Modify: `orchestrator/main.py`
- Modify: `orchestrator/mcp/client.py`, `orchestrator/mcp/server.py` if the existing
  `reindex_knowledge` MCP surface is generalized
- Test: `tests/test_kb_reindex.py`
- Test: `tests/test_kb_index_chunking.py`
- Test: `tests/test_knowledge_store.py`
- Test: `tests/test_kb_datasource_api.py`

### Watermark status extension

Add operational fields to `kb_index_watermark`:

- `source_head VARCHAR(64)` — last observed remote HEAD, including partial failures.
- `status TEXT` — `pending | indexing | ready | partial | failed`.
- `last_attempt_at`, `last_success_at` timestamps.
- `last_error TEXT` — bounded, redacted diagnostic; never credentials or raw auth URLs.

`indexed_commit` remains the truth about what the index actually covers. Status fields
are operational index state, not knowledge truth.

### Reindex changes

- Replace `gitea_client/repo_name/branch` coupling with an injected
  `KnowledgeGitSource` plus `root_path` and a non-secret source label.
- Generalize `knowledge_blob_map(tree, root_path)`; keep reserved `index.md`/`log.md`
  exclusion and Markdown-only behavior.
- Keep stored `path` repo-relative for compatibility and diagnostics.
- Stamp chunks with `embedding_version = model:dims:chunker:profile-fingerprint`, where
  the secret-free fingerprint covers the effective provider, normalized base URL, and
  catalog endpoint identity (never an API key). Both central indexing and runtime query
  filtering derive the exact same stamp. Build a separate watermark `pipeline_version`
  that also hashes normalized `root_path` and parser version. A root change forces a full
  rebuild without making query embeddings incompatible.
- Preserve embed-before-write, links-before-stamp, stamp-after-durability, delete, and
  orphan-reconciliation ordering.
- Acquire a cross-replica Postgres advisory lock keyed by `kb_id`, in addition to the
  in-process lock. A competing run reports `already-indexing` rather than interleaving.
- On a clean full rebuild, remove stale rows for that `kb_id` before/through the normal
  diff without touching any other KB.

### Lifecycle/API

- Creating a `kb` datasource records `pending` and schedules a best-effort initial
  index. Creation succeeds even if indexing later fails; status is visible.
- Updating URL/branch/root/credentials schedules a full rebuild. Metadata-only name or
  description changes do not re-embed.
- `POST /api/datasources/{id}/reindex?full=false` is owner/admin-only and returns the
  standard reindex summary.
- `GET /api/datasources/{id}/index-status` follows normal datasource visibility and
  returns watermark/status without credentials.
- `POST /api/datasources/{id}/test` for `kb` resolves HEAD and reports the indexable
  Markdown note count (or none found at the configured root) without embedding or
  mutating the index.
- The leader-gated KB sweeper scans both native project KBs and `datasources.type='kb'`.
  One failed external remote never starves the rest.
- Datasource deletion cancels/drains local builds, then holds the shared KB mutation lock
  across `knowledge_index WHERE kb_id=<datasource id>` cleanup (chunk/link cascade),
  watermark deletion, and app-row deletion. Stale sweep/manual work rechecks liveness
  after taking the lock and cannot recreate rows.

- [x] Write status/store migration tests first.
- [x] Refactor native Gitea reindex through `GiteaKnowledgeGitSource` with existing tests
  unchanged.
- [x] Add external source/root tests: initial, unchanged, modify, add, delete, rename,
  force-push/pipeline invalidation, malformed note skip, partial failure retry.
- [x] Implement cross-replica index claim and status transitions.
- [x] Add manual/status/test endpoints and access tests.
- [x] Extend the leader sweeper and deletion cleanup.
- [x] Regenerate the vector migration artifact through the repository's migration
  workflow; never hand-edit reference snapshots.

**Verification:**

```bash
pytest tests/test_kb_reindex.py tests/test_kb_index_chunking.py \
  tests/test_knowledge_store.py tests/test_kb_datasource_api.py \
  -x -q --tb=short
scripts/schema-snapshot.sh vector
ruff check orchestrator/services/kb_reindex.py \
  orchestrator/services/kb_datasources.py src/services/knowledge_store.py tests/
```

---

## Task 4 — Runtime KB bindings and source-aware tools

Replace the accidental `project_ids == kb_ids` equivalence with an explicit runtime
binding model while preserving native project writes.

**Files:**

- Create: `src/services/knowledge/bindings.py`
- Modify: `src/tools/context.py`
- Modify: `src/services/knowledge_store.py` (`KnowledgeRecord.kb_id`)
- Modify: `src/tools/knowledge/knowledge_tools.py`
- Modify: `src/agent.py`
- Modify: `src/api/persistent_session.py`, `src/api/persistent_app.py`
- Modify: `orchestrator/main.py` dispatch payload
- Test: `tests/test_kb_bindings.py`
- Test: `tests/test_knowledge_tools.py`
- Test: `tests/test_memory_persistent_equivalence.py`
- Test: `tests/test_memory_worker_equivalence.py`

### Binding model

```python
@dataclass(frozen=True)
class KnowledgeBinding:
    kb_id: uuid.UUID
    alias: str
    name: str
    kind: Literal["native", "datasource"]
    writable: bool
    indexed_commit: str | None = None
```

Rules:

- A project job/session gets a native binding first: `kb_id=project_id`, alias
  `project`, writable true.
- Every selected `kb` datasource adds a read-only binding from the internal dispatch
  payload. A session with no project may still have external bindings and KB tools.
- Alias = slugified datasource name. Resolve duplicate aliases deterministically by
  appending the datasource id's first eight hex characters.
- `ToolContext.knowledge_bindings` is authoritative. Keep `project_id/project_ids` for
  project/memory semantics; do not overload them with datasource ids.
- `ToolContext.kb_ids` derives from bindings. Writes resolve the one writable native
  binding; external-only contexts expose read tools but write tools return a clear
  read-only/no-native-target error.

### Tool behavior

- Add an optional `kb: str | None` selector to `kb_search`, `kb_read`, `kb_list`, and
  `kb_related` without adding new tools.
- `kb_search` searches all binding ids or the selected one. Each result renders
  `[alias] alias:slug — title (...)` and includes the source watermark/as-of marker.
- `KnowledgeRecord.from_row` preserves `kb_id` so formatting can map it back to a
  binding.
- `kb_read("alias:slug")` resolves exactly. `kb_read("slug")` queries every authorized
  binding and succeeds only for one match; two or more returns an ambiguity error that
  lists qualified handles. It must never silently return the first project/KB match.
- When Neo4j is present, only native bindings may use it. External bindings always read
  from `KnowledgeStore`; read/list/related calls combine native graph results with
  external Postgres results rather than taking the current global `if kg ... else ...`
  branch.
- `kb_list` and `kb_related` source-label every row. A qualified source is required for
  related-note traversal when the slug is ambiguous.
- `kb_write`/`kb_update` ignore external bindings as targets and keep the existing
  native file/DB behavior. Do not add an external target parameter in v1.
- `kb_lint`/`kb_index` remain native/workspace gardener tools; they do not mutate an
  external index snapshot.

- [x] Add binding/alias/ambiguity unit tests first.
- [x] Thread safe datasource ids/config through job and persistent-session dispatch.
- [x] Build bindings in both runtimes and initialize the store for external-only scope.
- [x] Add `kb_id` to `KnowledgeRecord` and source-aware formatting.
- [x] Implement optional selectors and qualified handles.
- [x] Pin writes to the native binding and add external-only error tests.
- [x] Ensure delegation/light-reader contexts inherit the same read-only bindings.

**Verification:**

```bash
pytest tests/test_kb_bindings.py tests/test_knowledge_tools.py \
  tests/test_memory_persistent_equivalence.py tests/test_memory_worker_equivalence.py \
  -x -q --tb=short
ruff check src/services/knowledge/bindings.py src/tools/context.py \
  src/tools/knowledge/knowledge_tools.py src/agent.py src/api/persistent_session.py tests/
```

---

## Task 5 — Multi-KB passive injection with a protected native share

Tools alone are insufficient: attached external KBs must participate in transient
knowledge injection without allowing a large org vault to erase local project context.

**Files:**

- Modify: `src/graph.py`
- Modify: `src/persistent_graph.py`
- Modify: `src/core/knowledge_injection.py`
- Modify: `src/services/knowledge_store.py` (`assemble_knowledge_block` source labels)
- Test: `tests/test_knowledge_injection.py`
- Test: `tests/test_persistent_graph.py`
- Test: `tests/test_memory_worker_equivalence.py`
- Test: `tests/test_memory_persistent_equivalence.py`

Default five-note policy:

- With a native binding: retrieve up to three native notes and up to two notes across
  selected external bindings. Unused slots may spill to the other side.
- Without a native binding: retrieve up to five external notes.
- Keep ordering stable within each RRF result set; deduplicate by `(kb_id, note_id)`.
- Every injected item displays `[alias]` and the block footer lists the external
  watermarks/as-of commits used.
- Both worker and persistent paths use chunk search with the current embedding version;
  remove/fence any remaining note-level `hybrid_search` fallback that is blind to
  reindexed chunk-only rows.
- Retrieval failure for one external KB is non-fatal and must not discard successful
  native/other-KB results.

- [x] Write native-floor, external-only, spill, labeling, and partial-failure tests.
- [x] Factor one shared retrieval helper used by worker and persistent graphs.
- [x] Preserve transient-message identification and prompt-cache tail anchoring.
- [x] Add audit metadata with counts by binding, but never repository URLs/credentials.

**Verification:**

```bash
pytest tests/test_knowledge_injection.py tests/test_persistent_graph.py \
  tests/test_memory_worker_equivalence.py tests/test_memory_persistent_equivalence.py \
  -x -q --tb=short
ruff check src/graph.py src/persistent_graph.py src/core/knowledge_injection.py tests/
```

---

## Task 6 — Cockpit datasource authoring, selection, status, and i18n

Expose the feature as an OKF Knowledge Base, not as an Obsidian- or generic-repository
toggle.

**Files:**

- Modify: `cockpit/src/app/core/models/api.model.ts`
- Modify: `cockpit/src/app/core/services/api.service.ts`
- Modify: `cockpit/src/app/views/datasources/datasource-list.component.ts`
- Modify: `cockpit/src/app/views/agent-settings/datasources-group.component.ts`
- Modify: `cockpit/src/app/views/agent-settings/datasources-group.component.spec.ts`
- Modify: `cockpit/src/app/views/project-detail/project-detail.component.ts`
- Modify: `cockpit/src/assets/i18n/en.json`
- Modify: `cockpit/src/assets/i18n/de-DE.json`
- Create/modify focused datasource-list tests where practical

UI contract:

- New option **OKF Knowledge Base** in a knowledge/repository-oriented optgroup.
- Fields: name, description, repository URL, default branch, OKF root path, HTTPS token
  or SSH key. Reuse repository auth widgets and “leave blank to retain credentials.”
- Explain: “Markdown/OKF repository; centrally indexed; read-only to agents in this
  release.” Do not market it as an Obsidian datasource.
- Datasource cards show `Pending`, `Indexing`, `Ready @ <sha>`, `Partial`, or `Failed`,
  plus last success and a credential-redacted error.
- Owner/admin gets Test and Reindex actions. Full rebuild remains an advanced confirm
  action because it can incur embedding cost.
- Job/session picker permits `kb` on every workspace tier. It remains explicit and
  preselected according to the existing eligible-datasource behavior.
- Project detail fixes the KB access badge/toggle at read-only for v1; it cannot imply
  that changing `project_datasources.read_only` enables external writes.
- Update English and German together and run the i18n checker.

- [x] Add TS model/API methods for config and index status.
- [x] Build the KB form using repository auth fields plus root path.
- [x] Add list/status/reindex UI and tier-selection coverage.
- [x] Add/adjust Vitest tests and both translation files.

**Verification:**

```bash
cd cockpit
npm test -- --run
npm run i18n:check
npm run build
```

---

## Task 7 — End-to-end acceptance and documentation closeout

Validate the whole path without relying on a public internet service in CI.

**Automated acceptance fixture:** create a temporary bare git remote containing:

- `vault/index.md` (reserved, excluded),
- two linked OKF notes with colliding-friendly common slugs,
- a nested note,
- one malformed note (lint/skip behavior),
- a later commit that modifies one note, deletes one, and adds one.

Acceptance cases:

- [ ] Create `type=kb`, root `vault`; credentials are encrypted at rest and absent from
  REST, dispatch, logs, status, and errors.
- [ ] Initial index reaches `ready`, stores `kb_id=datasource.id`, excludes reserved and
  out-of-root files, and stamps the remote commit.
- [ ] A project job with the datasource selected searches native + external and returns
  source-qualified results.
- [ ] An external-only lite persistent session loads KB tools and can search/read.
- [ ] Two KBs containing the same slug produce an ambiguity error for unqualified read
  and succeed with `alias:slug`.
- [ ] `kb_write` still writes only the native project `knowledge/` tree; external-only
  context refuses the write.
- [ ] Remote update + sweep re-embeds only changed blobs, removes deleted rows/chunks/
  links, and advances the watermark.
- [ ] Datasource deletion removes only that KB's index and leaves native/other KBs.
- [ ] A bad remote or embedding failure leaves the previous indexed commit readable,
  reports `partial/failed`, and retries on the next sweep.

**Full local verification:**

```bash
pytest tests/test_kb_*.py tests/test_knowledge_*.py \
  tests/test_datasource_*.py -x -q --tb=short
ruff check src/ orchestrator/ tests/
ruff format --check src/ orchestrator/ tests/
cd cockpit && npm test -- --run && npm run i18n:check && npm run build
```

**Manual k3d verification:**

1. Create a private Gitea/GitHub-compatible test repository with an OKF root.
2. Add it as an OKF Knowledge Base using a read-only token or deploy key.
3. Confirm initial status and hybrid search from Cockpit.
4. Start a `virtual` job/session with the KB selected; verify no workspace upgrade or
   clone occurs and `kb_search`/`kb_read` work.
5. Push an out-of-band note change, run Reindex, and verify the new commit/content.
6. Inspect agent metadata/logs to confirm the remote URL credentials/key never crossed
   the orchestrator boundary.

After verification:

- [ ] Update `docs/features/okf_knowledge_base.md` Slice 4 status with commits and live
  evidence.
- [x] Record any operational tuning (poll interval, timeouts, max repo/note size) in the
  design rather than silently changing defaults.
- [ ] Create a separate external-write-back design if writable KB datasources are next;
  do not smuggle pushes into this slice.

## Definition of done

Slice 4 is complete when an authorized user can add an external OKF git repository as a
`kb` datasource, centrally index and refresh it, explicitly attach it to a job/session
on any workspace tier, and have agents retrieve source-qualified knowledge through both
active tools and passive injection — while external credentials never reach the agent
and no external write path exists.
