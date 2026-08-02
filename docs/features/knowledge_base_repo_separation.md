# Knowledge base as a default-attached datasource, in its own repo (2026-08-01)

Status: **BUILT 2026-08-01, uncommitted.** All six steps of §7 implemented; full suite
`8 failed / 12460 passed`, the 8 being the recorded environment baseline (1 live-Postgres,
7 live-MCP) — zero regressions. Steps 1–3 independently verified live on k3d (migration
applied and behaviourally checked; endpoint committing to real Gitea). §9's fresh-project
smoke is the remaining gate. Notes added in place where the build diverged from this
design: §10a (a confirmed index-wipe found during step 2, fixed), §10c (`index.md` is no
longer written by anyone), and the "As built" notes in §6.

This is step 4 of `workspace_and_change_records.md` (§6.1, §7.4), promoted to its own doc
because it grew a schema migration, a new repo role, and a write-path move. The parent doc's
§7.4 should be read as "see this doc".

All `file:line` references verified at `5eefe025`.

## 1. What we are actually fixing

Today a project's knowledge base is not *configured* to live in the jobs repo — it is
**welded** there by the write path. `_dual_write_note`
(`src/tools/knowledge/knowledge_tools.py:520`) materialises every note as
`knowledge/<slug>.md` by calling `context.workspace_manager.write_file(...)`, and the agent's
workspace root *is* a checkout of the jobs repo. The file is then canonical: the reindexer
ingests it (`orchestrator/services/kb_reindex.py:62`, `KNOWLEDGE_PREFIX = "knowledge/"`), and
agent reads are gated on it — `get_note_by_slug` and `list_notes` both filter
`WHERE … path IS NOT NULL` (`src/services/knowledge_store.py:1239`, `:1326`), and `path` is
set **only** by the reindexer.

So the jobs repo carries three unrelated things at once: job records, the agent's scratch
workspace, and the knowledge vault. Curated merge (already shipped) separated the first two.
This separates the third.

The user-visible goal, in their words: *"the knowledge base changes should show which job
changed what note when"* and *"we separate the knowledge base"* — with the project repo kept
as the versioned default workspace and the record index.

## 2. The finding that reshaped this design

The obvious design — give the KB its own repo and clone it into the workspace next to the
code datasources — **does not survive contact with the code.**

`WorkspaceManager._clone_source_repos` (`src/core/workspace.py:732`) clones every non-`jobs`
project repo into `repos/<name>/`, registers it in `source_repos`, and adds `repos/` to the
workspace `.gitignore`. But **nothing in `src/` ever commits or pushes a cloned source
repo.** They are read-only clones; when an agent wants to push one it drives `git`/`gh` from
the shell itself. A note written into `repos/<kb>/knowledge/<slug>.md` would therefore sit in
an unpushed clone, never reach the KB repo, never be indexed — and the note would vanish when
the workspace is reaped.

Making that path work would mean adding automatic commit+push for one specific cloned repo,
plus push credentials for it inside the workspace. Both are new machinery, and the second
runs against the standing guardrail that internal credentials do not go in the workspace.

**So the write moves server-side instead** — which also happens to be what was wanted
originally (*"if the KB isn't in the workspace we don't even have to clone it"*). Reads
already come from Postgres; only the file write ever needed the workspace. The orchestrator
already owns the exact primitive: `GiteaClient.change_files`
(`orchestrator/services/gitea.py:533`) writes N files in a single commit and, since the
curated-merge work, takes a per-file `create`/`update` operation — which is precisely an
upsert.

**This is not the substrate flip.** Files stay canonical; the reindexer still ingests from
git; reads keep their `path IS NOT NULL` gate; `knowledge_chunks` keeps the reindexer as its
sole writer. All that changes is *who writes the file and into which repo*. The
files→store flip stays owned by `knowledge_base_substrate_decision`.

## 3. The design

**Every project gets a second managed repo, `project-<id8>-knowledge`, created at project
creation, registered with the new repo role `knowledge`, and auto-attached as a `kb`
datasource.** The vault root inside it stays `knowledge/`, so the reindexer needs no new
path vocabulary.

Note writes take this path:

1. Agent calls `kb_write` → row upserted into `knowledge_index` (direct asyncpg, unchanged).
2. The agent POSTs the rendered markdown to a new orchestrator endpoint, which commits it to
   the project's resolved KB repo via `change_files`, one commit per note.
3. The reindexer picks it up on its next sweep exactly as it does today, sets `path`, and
   generates chunks.

Step 2 is non-fatal, as the file write is today: a failed materialisation logs and the tool
still succeeds. Nothing in the read or search path moves.

Note that this keeps today's **Postgres-first** ordering — the row is the write, the commit
follows it. The intended end state inverts that, with the commit as the write and Postgres
rebuilt from git changes. See §12; this design is deliberately halfway, and step 4 is the
prerequisite for the rest of the distance.

**The dual-write is deleted, not branched.** `_dual_write_note` and its three call sites
(`knowledge_tools.py:860`, `:1024`, `:1312`) are replaced by a single server-side
materialisation call; the name goes too, since nothing is "dual" any more. There is no
workspace-write fallback for any project, old or new — one write path for everyone. Existing
projects differ only in which repo §5 resolves for them.

Consequences worth stating, because they are behaviour changes and not all of them are
losses:

- **The agent no longer needs git, or a workspace, to record knowledge.** Today
  `_dual_write_note` is guarded on `has_git()` (`:531`), so persistent sessions, repo-less
  projects and lite tiers write to Postgres and never materialise — their notes are invisible
  to `kb_read`/`kb_search`, which are gated on `path IS NOT NULL`. After this they
  materialise like everyone else. That is a fix, and it is worth a test of its own.
- **The KB repo is never cloned into the workspace.** `_clone_source_repos` must skip role
  `knowledge` exactly as it already skips `jobs` (`workspace.py:745`).
- **Note writes stop appearing in the workspace diff.** Anything that inferred "this job
  touched knowledge" from workspace files must read the change record instead — which is
  what §5 of the parent doc built the `changes:` block for.

## 4. Schema

`project_repositories` currently constrains the role vocabulary
(`orchestrator/database/migrations/app/0001_initial.sql:453`):

```sql
CONSTRAINT valid_repo_role CHECK (role IN ('jobs', 'source', 'reference'))
```

New migration `orchestrator/database/migrations/app/0078_project_knowledge_repo.sql`:

- Drop and recreate the CHECK to include `'knowledge'`.
- Add a partial unique index mirroring the existing jobs-repo one
  (`0001_initial.sql:460`): one knowledge repo per project.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_knowledge_repo
    ON project_repositories(project_id) WHERE role = 'knowledge';
```

**After applying: regenerate the snapshot with `scripts/schema-snapshot.sh app`.** Mandatory
after any migration.

## 5. Resolution and backwards compatibility

This is the part that must not break the 3,242 live notes in Better Resavio and every other
existing project.

`resolve_kb_repo` (`orchestrator/services/kb_reindex.py:909`) today returns the first
`role='jobs'` repo. It becomes:

1. `role='knowledge'` if the project has one → use it.
2. Otherwise fall back to `role='jobs'` → today's behaviour, unchanged.

Every existing project has no knowledge repo, so it takes branch 2 and nothing moves. New
projects take branch 1. **No backfill, no migration of note files, no dual-read.** A project
can be migrated later by creating its knowledge repo and moving `knowledge/` across in one
commit; out of scope here.

`kb_sweep_tick` (`:925`) drives its work list from `WHERE role = 'jobs'`; it must union in
`knowledge` repos, or better, select per project through the same resolution helper so the
two cannot drift.

The materialisation endpoint uses the same helper, so an existing project's notes keep
landing in `knowledge/` of its jobs repo — same repo, same path, same reindexer behaviour as
today. Only the writer changes, from the agent's workspace checkout to a Gitea commit.

A project with neither repo (no Gitea, repo-less) resolves to `None` and materialisation is
skipped, exactly as `has_git()` skips it today.

## 5a. The other filesystem consumers

"No local filesystem writes" is broader than the dual-write, and two of the three consumers
the parent doc named need attention:

- **`close_backlog_ticket` (`orchestrator/services/project_backlog.py:245`) — already
  correct.** Despite the parent doc's wording ("reads *and rewrites* the note file"), it is
  orchestrator-side and goes through Gitea: `gitea.get_file_content(repo_name, file_path)`
  against `knowledge/{note_id}.md`. It needs only to take its `repo_name` from the §5
  resolution instead of assuming the jobs repo.

- **`kb_lint` (`knowledge_tools.py:2011`) and `kb_index` (`:2097`) — must be refactored.**
  Both hard-require a workspace (`:2030`, "kb_lint requires a workspace backend to read
  notes") and then glob the vault off it with `ws.list_files(root, "*.md")` +
  `ws.read_file(rel)`. Once the vault leaves the workspace they do not fail loudly — they
  report *"No markdown notes found under `knowledge/`"* and look like an empty KB. **That
  silent-empty mode is the single most dangerous outcome in this change.**

  Refactor both to read notes from `knowledge_index` rather than files. They gain the same
  benefit as the write path: they start working on lite tiers and persistent sessions, where
  today they refuse outright. The `path` argument becomes vestigial for the default vault;
  keep accepting it for an explicit directory lint, or drop it — implementer's call, but it
  must not silently mean "look in a workspace that no longer has the vault".

## 6. Datasource attachment

`create_project` (`orchestrator/main.py:32545`) already creates the jobs repo and calls
`add_project_repository` (`orchestrator/database/postgres.py:11657`). It gains the parallel
block for the knowledge repo, then creates a `kb` datasource pointing at it and links it to
the project — the same shape as the WebDAV datasource the cloud provisioning already
auto-creates.

Two things to get right:

- **Do not double-index.** External `kb` datasources are indexed under their own datasource
  UUID (`kb_datasources.py:55`, `reindex_kb_datasource`), while the native project KB is
  indexed under `project_id`. If the auto-attached datasource were treated as an ordinary
  external KB, every note would be indexed twice under two different `kb_id`s and appear
  twice in search. The project's own KB datasource must be marked as native-backed
  (e.g. a `config.project_id` field) and skipped by the external sweep at
  `kb_reindex.py:968`.

  **As built (step 6):** the marker is `config.native_project_id`
  (`NATIVE_PROJECT_CONFIG_KEY` / `native_kb_project_id()` in
  `orchestrator/services/kb_datasources.py`, mirrored in
  `src/services/knowledge/bindings.py` because the agent image has no orchestrator
  deps) — named for what it asserts rather than the ambiguous bare `project_id`, which
  reads like "the project this connector is linked to". Four places honour it: the
  external sweep filters it out of `list_datasources(ds_type="kb")`;
  `reindex_kb_datasource` raises on it as the funnel-point backstop; `POST
  /api/datasources/{id}/reindex` 400s; and `PUT /api/datasources/{id}` re-attaches it
  after normalising a user-supplied config, so editing the root path cannot strip the
  marker and quietly promote the vault back into the sweep. `_normalize_kb_config`
  gained `stored=` for exactly that asymmetry: user input can never carry the marker
  in, stored config never loses it on the way out.
- **Writability.** `build_knowledge_bindings` (`src/services/knowledge/bindings.py:50`)
  gives datasource-kind bindings `writable=False` unconditionally (`:113`), and native
  project bindings `writable=index == 0` (`:78`). The project's own KB must stay writable.
  Simplest correct move: keep emitting it as the `kind="native"` binding it is today and let
  the datasource row be the *management surface* (visible, listable, unlinkable) rather than
  the binding source. Genuinely external KBs stay read-only, unchanged.

  **As built (step 6):** that, plus a dedupe. `build_knowledge_bindings` keys a
  native-marked datasource by its *project* id — the id its notes are actually indexed
  under — and skips it if that KB is already bound, so selecting the connector in the
  job picker collapses into the writable `kind="native"` binding instead of shadowing
  it with a read-only alias. Selected for a job in a *different* project it still binds,
  read-only, against the right index rather than an empty datasource UUID.
  `inject_datasource_index` also drops it from datasources.md's "OKF Knowledge Bases"
  list, which would otherwise tell the agent its own KB is read-only.

## 7. Sequencing

1. Migration + snapshot regen. Inert on its own.
2. `resolve_kb_repo` prefers `knowledge`, falls back to `jobs`; `kb_sweep_tick` and
   `close_backlog_ticket` take their repo from the same helper. Still inert — no project has
   a knowledge repo yet, so every caller resolves exactly what it resolves today.
3. Orchestrator endpoint that materialises one note into the resolved KB repo via
   `change_files`, with per-file `create`/`update`.
4. Replace `_dual_write_note` with a call to it, at all three call sites. Delete the
   workspace write. **This is the first behaviour change, and it hits every project at
   once** — existing ones keep the same target repo and path, but the writer changes.
5. Refactor `kb_lint` / `kb_index` to read from `knowledge_index` (§5a). Must land no later
   than step 6, or new projects get the silent-empty vault.
6. `create_project` provisions the knowledge repo + `kb` datasource + link;
   `_clone_source_repos` skips role `knowledge`.
7. Fresh test project, seeded by hand, exercised with manual jobs (§9).

Steps 1–2 are safe to ship alone. Step 4 is the one to watch: it is a single write path for
every project, so a regression there is fleet-wide rather than scoped to new projects. It is
also the step that fixes note materialisation for lite tiers and persistent sessions.

## 8. Acceptance criteria

1. Existing projects still work: Better Resavio's reindex still resolves to its jobs repo,
   and a note written by a job still lands at `knowledge/<slug>.md` there — now as a Gitea
   commit from the orchestrator rather than a workspace file. Same repo, same path, same
   reindex outcome.
2. A newly created project has exactly two managed repos (`-jobs`, `-knowledge`) and one
   auto-attached `kb` datasource.
3. A note written by a job in the new project appears as a commit in the **knowledge** repo,
   and nowhere in the jobs repo.
4. That note is readable via `kb_read` and findable via `kb_search` after a reindex — proving
   `path` and chunks were populated through the new path.
5. The note appears exactly **once** in search results (no double-index).
6. The new project's workspace contains no checkout of the knowledge repo.
7. `knowledge_note_revisions` records an entry when a second job overwrites that note
   (migration `0016`, already shipped).
8. A job that only writes notes still produces a change record on `main` of the jobs repo
   listing the note as a `knowledge` change.
9. **No workspace dependency:** a note written from a persistent session or a lite-tier job
   — neither of which has git — materialises and becomes readable via `kb_read`. This is new
   behaviour; today those notes stay pathless and invisible.
10. `kb_lint` and `kb_index` report the new project's notes rather than
    "No markdown notes found", and both run without a workspace.
11. `grep -rn "knowledge/" src/tools/knowledge/` shows no remaining workspace write or
    workspace glob of the vault — the refactor is complete, not merely bypassed.

### 8a. k3d verification status (2026-08-01)

Run against the local k3d cluster on project `d4f216f9` ("KB Repo Separation Gate"), created
through the authenticated `POST /api/projects` so provisioning ran for real.

| # | Criterion | Result |
|---|-----------|--------|
| 1 | Existing projects untouched | **PASS** — a note posted for a project with no knowledge repo resolved to its **jobs** repo and committed there (`project-9acaf531-jobs`, `knowledge/k3d-probe-note.md`). The fallback works on live data. |
| 2 | Two managed repos + one auto-attached `kb` connector | **PASS** — `project-d4f216f9-{jobs,knowledge}` both `is_managed`, plus one `kb` datasource carrying `config.native_project_id` and a deliberately null `connection_url` (see below). |
| 3 | Note lands in the knowledge repo, nowhere in the jobs repo | **PASS** — `knowledge/gate-note.md` present in the knowledge repo; the jobs repo has **0** `knowledge/` entries. Commit message `kb: gate-note (job aaaaaaaa)` with the full job uuid in the body. |
| 4 | Readable via `kb_read` / findable via `kb_search` after reindex | **PASS** — after seeding the tiktoken cache (see below) the sweep adopted the file: `path = knowledge/gate-note.md`, `status = active`, and 1 chunk with a non-null embedding. It therefore satisfies the `path IS NOT NULL` gate the agent read path uses, and has the chunk row `kb_search` runs on. |
| 5 | Exactly one search hit (no double-index) | **PASS** — exactly **1** row for the project, keyed `kb_id = project_id = d4f216f9…`, and **0** rows under the connector's own uuid. The sweep logs `kb_sweep: skipping 1 native project KB datasource(s)` on every tick. |
| 6 | No checkout of the knowledge repo in the workspace | not yet exercised — needs a job to run. |
| 7 | `knowledge_note_revisions` on overwrite | not yet exercised — needs two jobs. |
| 8 | Notes-only job still produces a change record | not yet exercised — needs a job. |

**Criteria 6–8 need a real job to run** (workspace provisioning, a second job overwriting a
note, and a change record on `/complete`). They are not blocked by anything — just not
exercised by a note-level test.

**One environmental detour worth recording.** The first two sweeps resolved the knowledge
repo and read `gate-note.md` correctly, then failed to chunk it:
`Failed to resolve 'openaipublic.blob.core.windows.net'` while fetching the tiktoken
`cl100k_base` encoding — k3d has no egress for it (the known
`k3d_tiktoken_offline_blocks_kb_reindex` issue). Fixed by seeding the cache in the
orchestrator pod: the file is keyed by the sha1 of its URL, i.e.
`/tmp/data-gym-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4`. The following sweep indexed
it. Unrelated to this design, but it will bite anyone verifying KB work on k3d.

**`connection_url` is deliberately null on the auto-created connector — do not "fix" it.**
`gitea.create_repo` returns an *authenticated* clone URL (`http://user:pass@host/...`; its own
docstring says so), and unlike `redact_repository` — which strips embedded credentials and
externalizes the host, and whose docstring calls the raw value "a credential leak to every
project member" — `redact_datasource` (`orchestrator/security/access.py`) only pops
`credentials` and never touches `connection_url`. Storing the URL would therefore return
credentials to every project member via `GET /api/datasources`, reopening an already-fixed
leak through a different table. `project_repositories` stays the single answer to "where does
the vault live"; if the UI ever needs to display it, derive it at read time through
`redact_repository` rather than storing a second copy.

Follow-up worth its own ticket, latent today: `redact_datasource` should sanitise
`connection_url` the way `redact_repository` does. Nothing currently exploits it — zero
datasources on either k3d or dev carry credentials in that field, because in practice they go
in the `credentials` blob, which *is* stripped — but a user pasting
`postgres://user:pass@host/db` as a connection URL would be exposed to every project member.

## 9. Test plan — the fresh project

Better Resavio is not the test bed: it predates all of this and its vault would take the
fallback path. A new project is created for the test, seeded by hand with a small number of
notes and one attached code datasource (the KurortEngine GitHub repo is already wired and can
be reused, or a throwaway repo created).

Manual jobs, run one at a time so each acceptance criterion is attributable:

- A notes-only job → criteria 3, 4, 5, 8.
- A second notes-only job overwriting the first's note → criterion 7.
- A code job against the attached repo → confirms the jobs repo stays records-only and the
  code change lands as a PR on the external repo.

## 10. Risks

- **The endpoint is a new failure mode on every note write, with no local fallback.** This is
  the cost of deleting the dual-write rather than branching it, and it is accepted
  deliberately. Mitigated by keeping the call non-fatal exactly as the current file write is,
  and by the reindexer being idempotent — a missed materialisation is recovered by rewriting
  the note, not by repair tooling. But note the asymmetry: today a materialisation failure
  means one stale file in a workspace that is about to be discarded; afterwards it means the
  note exists in Postgres and is invisible to every reader. Worth a counter/log that is
  actually looked at, not just a `logger.warning`.

- **Step 4 is fleet-wide.** Unlike the rest of this design, replacing the write path affects
  every existing project immediately, not just projects created after the change. It is the
  one step that wants a k3d run before it goes near dev.
- **Commit volume.** One commit per note write, where today a phase's notes ride along in the
  workspace commit. If this proves noisy, batch per phase boundary; `change_files` already
  takes N files in a single commit, so batching is a caller change only.
- **Double-index** is the one failure that corrupts search rather than just failing. It is
  criterion 5 for that reason, and should be tested before anything else is built on top.
- **`resolve_kb_repo` has more callers than it looks, and they must move together**, or the
  sweep and the write path target different repos — the silent-divergence failure that
  produced `kb_reindex_watermark_never_advances`. Implementation found the sites below;
  `main.py` is cited by symbol because its line numbers move.

### 10a. The post-merge trigger will wipe the chunk index — found and confirmed during step 2

**This is the highest-severity item in this design and it must land with or before step 6.**

`_reindex_project_kb` (`orchestrator/main.py`) resolves the vault repo **only when
`repo_name` is falsy** (`if not repo_name:`). The post-merge KB-freshness trigger — the
`_kb_reindex_after_merge` closure fired for `merge_status in ("merged", "curated", "empty")`
— passes `repo_name=job["repo_name"]` explicitly, and that value is always the **jobs** repo
(`orchestrator/services/job_provisioning.py:192`). So resolution is bypassed.

Once a project has a knowledge repo, that trigger reindexes `kb_id=project_id` against the
jobs repo, whose tree no longer contains `knowledge/`. `plan_reindex`
(`orchestrator/services/kb_reindex.py:159`) computes
`deletes = sorted(path for path in indexed if path not in current)`, so an empty `current`
turns **every indexed path into a delete**. The whole chunk index is dropped; the next
leader-gated sweep rebuilds it from the knowledge repo.

The result is a chunk index that flaps between full and empty on every loop job, with no
error surfaced anywhere: the trigger is fire-and-forget and its failure path is a non-fatal
`logger.warning`. Search would intermittently return nothing and nothing would look broken.

Fix: drop the `repo_name=` override at the call site so the trigger resolves like everything
else. One line. It was not made during step 2 only because `main.py` was owned by another
agent in the same wave.

### 10c. `knowledge/index.md` is no longer written by anyone — decided, 2026-08-01

`kb_index` used to regenerate `knowledge/index.md` into the workspace. Implementing step 4
surfaced that it cannot simply move to the materialisation endpoint: `kb_materialize.py`
refuses the OKF reserved stems (`_RESERVED_SLUGS = {"index", "log"}`), so every `kb_index`
call would return `failed`/`invalid-slug` and flood the `kb-materialize:` ERROR channel with
deterministic noise — burying exactly the real note failures §10 wants that channel to carry.

**Decision: `kb_index`'s vault branch writes nothing.** It still reads the index, skips
reserved ids and reports the grouping; `kb_list` gives the same view live. Verified safe:
nothing in the codebase reads `knowledge/index.md`. The only non-comment reference is
`orchestrator/services/kb_reindex.py:78`, `_RESERVED_BASENAMES = {"index.md", "log.md"}`,
which *excludes* it from indexing. The file was write-only.

Two reasons not to simply carve `index` out of the reserved set:

- The agent cannot read the repo's current `index.md`, so regenerating it blind would drop
  the human-authored sections `render_index_md` exists to preserve.
- It is derived navigation over the whole vault, which is the reindexer's view, not a single
  agent's.

**If the vault ever wants a generated index again, the reindexer should own it** — it already
walks the tree and already knows the reserved basenames. `kb_index`'s explicit-`path` mode
(indexing `docs/` or a datasource checkout) is unaffected and still writes to the workspace;
that is not the vault.

### 10b. Adjacent, pre-existing, not caused by this change

- `_clone_auxiliary_repos` (`src/core/workspace.py:731`, skip at `:745`) skips only
  `role == "jobs"`, so a knowledge repo would be cloned into `repos/`. Scheduled for step 6.
  (Note: this function was renamed from `_clone_source_repos`; earlier drafts of this doc
  used the old name.)
- The project knowledge `PATCH`/`DELETE` endpoints in `orchestrator/main.py` mutate
  `knowledge_index` and Neo4j without touching the file. Since files stay canonical, the
  next reindex reverts them — the same class of bug the backlog mirror was written to avoid.
  Independent of this work; wants its own ticket.

## 11. Not in scope

- The files→store substrate flip (`knowledge_base_substrate_decision`). Files stay canonical.
- Migrating existing projects' vaults into knowledge repos.
- Making genuinely external KB datasources writable (that is §6.3, change-capable
  datasources).
- The cockpit/agent read-surface mismatch noted in the parent doc's §6.1 — real, but
  independent.

## 12. Direction — git as the source of truth (not built, and this design only goes halfway)

This design moves the *writer* server-side but keeps today's write **order**, which is
Postgres-first:

```
kb_write → row upserted into knowledge_index      (the write)
         → note committed to the KB repo           (materialisation)
         → reindexer sets path, generates chunks   (makes it readable)
```

The intended end state inverts that. **The git commit should be the write, and Postgres
should be a pure derived index that rebuilds from git changes:**

```
kb_write → note committed to the KB repo           (the write — the only write)
         → reindex on commit rebuilds knowledge_index + chunks   (derived)
```

Everything else in the system already points this way, and several current awkwardnesses are
symptoms of only going halfway:

- The `path IS NOT NULL` gate on `get_note_by_slug` / `list_notes` exists precisely because a
  row can exist without a file. If the commit *is* the write, that state is unrepresentable
  and the gate becomes unnecessary rather than load-bearing.
- `reconcile_orphans` (`src/services/knowledge_store.py:1082`, `grace=timedelta(hours=1)`)
  is a garbage collector for exactly the states Postgres-first ordering makes possible. Its
  own docstring enumerates them: *"A row written by the agent write-through carries
  `(project_id, note_id)` but no `kb_id`/`path`; the reindexer adopts it once a file with
  `note_id`'s slug appears in the tree. A pathless active row whose `note_id` matches no
  current tree slug, and which has sat unadopted past the adoption grace, is an orphan
  (failed/squashed commit, slug mismatch, create-then-delete)."* Under commit-first ordering
  there is no unadopted row to reconcile, because the row is only ever created by adoption.
- `knowledge_note_revisions` (migration `0016`, shipped) reconstructs in Postgres a history
  git already keeps natively. It stays useful as a fast query surface, but under
  git-as-truth it is a cache, not the record.
- The *"which job changed what note when"* requirement, and any future human **review** of
  knowledge changes, are diffs and pull requests in git terms. Modelling them in table rows
  is the harder path.

**This is a live tension with `knowledge_base_substrate_decision`, and it should be resolved
there before anyone builds further.** That decision currently frames the open question as the
files→**store** flip — making Postgres canonical and dropping the files. This note argues the
opposite direction: files→**more** canonical, Postgres derived. Both cannot be right.

For the record, git-as-truth was weighed once already, during the design conversation that
produced the parent doc, and set aside: hybrid search (`vector(4096)` + tsvector + HNSW + RRF)
needs Postgres regardless, so git-as-truth does not remove Postgres — it demotes it to a
derived index and adds a sync pipeline, and that pipeline is what has already failed twice
here (`kb_reindex_watermark_never_advances`, `kb_export_vault_corruption`). Concurrency is the
other cost: loop roles write simultaneously, and git resolves that with conflicts rather than
last-write-wins. The condition recorded for revisiting was *"if the requirement becomes human
review of KB changes rather than recovery — git wins that one."*

Nothing in this doc blocks that direction; step 4 moving the write server-side is the
prerequisite for it either way, since a commit-first write is only possible once the
orchestrator owns the commit. What would still be needed:

- Make reindex commit-triggered rather than sweep-triggered, so a note is readable promptly
  after its commit instead of after the next sweep.
- Decide the read-after-write contract: either the materialisation endpoint indexes
  synchronously before returning, or `kb_write` becomes explicitly eventually-consistent and
  callers stop assuming a note is immediately findable.
- Give concurrent writers a conflict story that is not last-write-wins.
