---
tags:
  - feature
  - agent
  - workspace
  - tools
  - contacts
aliases:
  - virtual directories
  - virtual filesystem
  - overlay backend
related:
  - "[[contacts_registry]]"
  - "[[agent_skills]]"
  - "[[workspace_storage_state_topology]]"
  - "[[loop_repo_compounding]]"
---

# Virtual Directories

> Agent-facing framework files — `tools/`, `contacts/`, `instructions.md`, and later `plan.md` / `todos.yaml` — served from live state through the workspace file tools instead of written to the workspace filesystem. Nothing goes stale after a snapshot restore, nothing leaks into a repo or backup, and agent-authored state stops dying with the pod.

**Status:** Design approved 2026-07-30 (brainstorm + prior-art review); scope extended the same day to instruction/organization files. Not yet implemented.
**Filed:** 2026-07-30

## Motivation

Framework content is currently **materialized as real files** on an `emptyDir` workspace that is not the durable store ([[workspace_storage_state_topology]]: Postgres is the truth):

| File(s) | Author | Written by | Consumed by |
|---|---|---|---|
| `tools/README.md`, `tools/<name>.md` | system | `generate_workspace_tool_docs` in `src/agent.py`, `src/api/persistent_session.py` | agent reads (deferred-tool docs) |
| `contacts/<slug>.md` | system | *(nothing — materialization reverted in `b8e48c10`)* | agent reads |
| `instructions.md`, `task_brief.md` | system (template / job upload / inline) | `_deploy_instruction_files` (`src/agent.py:3101`) — **worker jobs only**; the session path (`src/api/persistent_session.py:995`) writes neither file and never has (verified 2026-07-30) | agent reads; `src/graph.py:472,478` |
| `plan.md`, `todos.yaml` | **agent** | `PlanManager.write` (`src/managers/plan.py:93`), todo tools | agent; curator (`src/graph.py:2783`); orchestrator display via `orchestrator/services/workspace.py:519`, `get_all_todos`; `/api/jobs/{id}/todos*`; MCP; Cockpit |

Materialization has a recurring failure class:

- **Staleness.** Workspaces persist through snapshots/restores; nothing prunes docs for removed tools, and mid-lifecycle tool changes (virtual→sandbox upgrade re-derive, session tool-group overrides) leave docs describing the wrong tool set.
- **Leakage.** Generated files sit next to project files. `skills/` leaked onto a loop's `main` once (caught by k3d E2E); `_LOOP_MAIN_GITIGNORE` now carries eleven defensive entries (`workspace.md`, `plan.md`, `todos.yaml`, `tools/`, `instructions.md`, `task_brief.md`, …) purely to keep framework scaffolding out of project artifacts.
- **Fragile seeding.** `instructions.md` deployment is guarded by `exists()` probes precisely because a remote-backend probe once "clobbered user-provided instructions with the template" (`agent.py:3120` comment); a parallel repair path rewrites it "only if it vanished" (`agent.py:2086`). Both exist only because the content lives in a file that can go missing.
- **Agent state dies with the pod.** `plan.md` / `todos.yaml` are the agent's own working state, stored on an ephemeral pod. The orchestrator can only display them by reaching *into* the workspace over SSH — which fails exactly when the pod is gone and you most want to see the plan.

Virtual directories remove the class: content is *served* at read time, and agent-authored content is *stored* where it survives.

## Prior art (2026-07-30 review)

- **Anthropic, "Code execution with MCP"** — tools presented as a filesystem tree the agent lists/reads on demand (150k→2k token reduction claim). Their tree lives in an ephemeral sandbox; our workspaces persist, which is why we serve instead of write. <https://simonwillison.net/2025/Nov/4/code-execution-with-mcp/>
- **LangChain deepagents** — filesystem tools over a pluggable virtual backend; `CompositeBackend` routes path *prefixes* to different backends. Same shape as this design, and their FS is read-**write** state-backed — precedent for Slice 2. <https://docs.langchain.com/oss/javascript/deepagents/context-engineering>
- **Dust.tt** — production synthetic filesystem over live enterprise data, served live with caching; lesson adopted: virtual reads flow through normal read caps/pagination. <https://www.zenml.io/llmops-database/building-synthetic-filesystems-for-ai-agent-navigation-across-enterprise-data-sources>
- **arXiv 2607.17598** — one level of index→leaf routing scales; "a second, deeper routing level never helps and sometimes breaks accuracy outright." Virtual prefixes stay flat. <https://arxiv.org/abs/2607.17598>
- **ToolFS / Mirage** — FUSE-based VFS visible to the shell; the road not taken (mount/daemon infrastructure on every pod and VM image).

## Scope decisions (user, 2026-07-30)

- **General mechanism, staged consumers.** Slice 1: `tools/`, `contacts/`, `instructions.md` + `task_brief.md` (all read-only). Slice 2: `plan.md` + `todos.yaml` (read-write, Postgres truth). Skills stay materialized — skill scripts execute via the shell and need real files.
- **Tool-layer only.** Virtual paths are visible through the file tools, **not** the shell (`run_command` runs on the workspace pod/VM over SSH against the real filesystem). No FUSE.
- **Live read-through** with a short TTL cache, not boot snapshots.
- **Same paths as today.** Prompts and instruction files keep working unchanged.
- **Overlay backend** at the `WorkspaceManager` seam, over per-tool interception and over FUSE.
- **Postgres is the truth for agent-authored files** (Slice 2), so the plan survives pod death and Cockpit reads it without SSH-ing into a workspace.

## Provider classes

The mechanism is one overlay; providers come in three flavours, and the distinction drives everything else:

| Class | Examples | Truth | Agent writes? |
|---|---|---|---|
| **A. System projection** | `tools/`, `instructions.md`, `task_brief.md` | in-process state / job record | no — rejected |
| **B. Live DB projection** | `contacts/` | Postgres (contacts registry) | no — rejected |
| **C. Agent state** | `plan.md`, `todos.yaml` | Postgres (`job_documents`) | **yes — write-through** |

A and B ship in Slice 1; C in Slice 2. The contract below is designed for all three now, so Slice 2 adds a provider rather than a redesign.

## Architecture

**Seam.** New `VirtualOverlayBackend` (`src/core/backends/overlay.py`) wraps the real backend where `WorkspaceManager` stores it (`self._backend`). Local, remote-SSH, and virtual-tier backends are wrapped identically; file tools keep calling `context.workspace_manager` and never learn the overlay exists. Subagent readers build their **own** `WorkspaceManager` over a `SubdirBackend` (`src/tools/delegation/reader_env.py:156`), so they get their own overlay and must have providers registered explicitly — a reader's `tools/` is bound to the reader's own tool list, not the parent's.

**Idiom.** Same as `SubdirBackend` (`src/core/backends/subdir.py`): a plain delegating wrapper, *not* a `WorkspaceBackend` subclass — `__getattr__` forwards everything not overridden, so future backend-interface growth delegates by default. Overridden are the path-touching methods: `read_file`, `write_file`, `append_file`, `exists`, `is_file`, `is_dir`, `list_dir`, `search_files`, `stat`, `resolve_path`, `mkdir`, `delete_file`, `delete_directory`, `move`, `copy`.

**The overlay exposes `.inner`** — the unwrapped backend — for the few call sites that must bypass virtualization (see *Sentinel probes*).

**Provider contract:**

```python
class VirtualDirProvider(Protocol):
    prefix: str                                    # "tools", "contacts", "instructions.md"
    writable: bool = False                         # Class C opts in
    def entries(self) -> dict[str, EntryMeta]      # filename -> {size, mtime}
    def read(self, subpath: str) -> str | None     # None = not found
    def write(self, subpath: str, content: str) -> None   # writable providers only
```

`list_dir`, `exists`, `is_file`, `is_dir`, `stat`, glob patterns, and `search_files` (grep over `entries()` + `read()`) are derived generically in the overlay, so a provider cannot contradict itself. A `prefix` may be a directory (`tools`) or a single file (`instructions.md`) — a file-prefix provider serves exactly one entry and reports `is_dir=False`. Virtual directories are **flat**; no subdirectories in v1. Rendered content flows through the normal `read_file` size caps/pagination.

**Registration.** At the boot paths that write these files today: worker (`src/agent.py`) and session (`src/api/persistent_session.py`). Tools always. **Instructions on the worker path only** — sessions write neither `instructions.md` nor `task_brief.md` today, and Slice 1 is a passive migration, so it adds no file a surface never had (session instructions would duplicate `get_phase_system_prompt`'s output; if wanted, that is its own decision). Contacts only when the job/session has a project. Kill switch `VIRTUAL_DIRS_ENABLED` (default `true`).

## Slice 1 providers (read-only)

### ToolsProvider

Holds a **callable returning the currently loaded tools** plus the existing `DescriptionManager`. `entries()` = `README.md` + `<name>.md`; `read()` renders on demand via the untouched `generate_tool_index` / `generate_tool_description` — content byte-identical to today. Because it reads the current tool list per call, mid-lifecycle tool changes are reflected with no regeneration step. Registered before `apply_description_overrides` so full docstrings are captured.

Migration: delete both `generate_workspace_tool_docs` call sites and the file-writing wrappers (`generate_workspace_docs`, `generate_workspace_tool_docs`); keep the renderers.

### ContactsProvider

The contacts registry is **implemented on `develop`** (migration `0076_contacts_normalize.sql`, `orchestrator/routers/contacts.py`, Cockpit page) except its agent surface, and the renderer `src/core/contact_files.py` (`contact_slug`, `render_contact_md`, `contacts_to_workspace_files`) is retained dormant for exactly this provider.

New: one orchestrator internal endpoint, `X-Internal-Key` authed, keyed by **job/thread identity — not agent-supplied `project_id`** (the orchestrator derives the project binding server-side, same trust posture as `send_message`). ~60s TTL cache, ~3s client timeout. Serves `<slug>.md` per the contacts file format plus a `README.md` index. This deliberately replaces the reverted `b8e48c10` approach, which pushed contacts through the resolved-config blob at boot — the staleness this design removes.

### InstructionsProvider

Serves `instructions.md` and `task_brief.md` with **precedence resolved in one place**: job upload/inline content (from the job record) → rendered template (`load_instructions` + `render_instruction_content`, which needs `loaded_tool_names` for `{% if has_tool(...) %}`). Registered after tools load, like today's deployment.

This deletes, rather than reimplements, the fragile parts:

- the `exists()`-probe guard that once clobbered user-provided instructions with the template (`agent.py:3120`),
- the "rewrite only if it vanished" repair path (`agent.py:2086`),
- `instructions.md` / `task_brief.md` membership in `_agent_seed_files` re-assertion (`agent.py:216`) — virtual files cannot be lost, so re-assertion narrows to genuinely seeded real files (bound skills).

Config-driven `instruction_files` (literal files and bound skills → `skills/<name>/SKILL.md`) stay **real files** — skills are out of scope, and those paths are shell-executed.

**Sentinel probes — the one hazard.** `task_brief.md`'s *existence* is currently used as a proxy for "this workspace still has its seeded content" (`agent.py:2011`, `agent.py:2159` `if resume and _backend_has("task_brief.md")`), which feeds resume and re-seed decisions in the blast radius of the known unseeded-workspace bug. A virtual `task_brief.md` always exists, so a naive migration makes a freshly-wiped pod look seeded. **Every such probe must be retargeted at `overlay.inner`**, and its semantics narrow to "are the real seeded files (bound skills, uploads) present?". Enumerating and retargeting these probes is a required, tested step of Slice 1 — not a follow-up.

## Slice 2 — writable providers (`plan.md`, `todos.yaml`)

**Storage.** One generic table, so future writable docs need no schema work:

```sql
job_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,      -- worker jobs
    thread_id UUID REFERENCES threads(id) ON DELETE CASCADE,  -- sessions (table: `threads`, 0001_initial.sql:746)
    path TEXT NOT NULL,                                     -- 'plan.md', 'todos.yaml'
    content TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((job_id IS NULL) <> (thread_id IS NULL))
);
CREATE UNIQUE INDEX uq_job_documents_job ON job_documents(job_id, path) WHERE job_id IS NOT NULL;
CREATE UNIQUE INDEX uq_job_documents_thread ON job_documents(thread_id, path) WHERE thread_id IS NOT NULL;
```

Migration number = next free at implementation time (`0076` is contacts, renumbered from `0075` when `0075_project_loop_officer_scheduling.sql` claimed that slot — check again before writing).

**Write path.** Agent `write_file("plan.md", …)` → overlay → `provider.write()` → orchestrator internal endpoint (upsert) → cache updated in place. Write-through, no local copy, last-write-wins. A failed write raises a tool error the agent can see and retry — never a silent drop.

**What needs no change:** `PlanManager` (`src/managers/plan.py`) calls `workspace.read_file` / `write_file`, which the overlay intercepts — the class is untouched. Phase snapshots (`src/core/phase_snapshot.py:259`) read `plan.md` through the workspace (virtual) and write into `archive/phase_<n>/` (real) — they keep working and gain a copy that no longer depends on the pod's disk.

**What gets simpler:** `orchestrator/services/workspace.py:519` (`get_workspace_file(job_id, "plan.md")`), `get_all_todos`, `get_current_todos`, and everything above them (`/api/jobs/{id}/todos*`, MCP `get_todos`/`get_current_todos`, Cockpit) read the DB directly instead of reaching into a pod — and keep working after the pod is gone. This is the "live job plan display" win.

**What must be verified:** cloud-sync (`src/services/cloud_sync/base.py:39` lists `plan.md`) must read through the workspace manager to see virtual content; if it touches the backend directly it needs retargeting. Todos additionally "restore from the LangGraph checkpoint (not disk)" per `job_provisioning.py:36` — Slice 2 must reconcile checkpoint restore with DB truth (DB is authoritative; checkpoint restore writes through the provider).

**Shell-shadow reconciliation.** The shell writes to the real filesystem, so `echo >> plan.md` or `sed -i` would otherwise be silently lost behind the virtual file. Mitigation is recovery, not just detection: at boot and at each phase transition, for every writable virtual path, if `overlay.inner` has a real file, compare its mtime against the row's `updated_at` — **newer → ingest as the new content; older → discard** — then delete the real file and log loudly either way. A shell write is absorbed on the next sweep. The rendered instruction line additionally tells the agent to edit these files with `write_file`, not the shell.

## Semantics

- **Full subtree ownership.** A registered prefix answers everything under it, including names the provider lacks: `read_file("tools/removed_tool.md")` is a clean not-found, never a fall-through to a stale real file. Matching is on the first path segment, so `main/tools/` inside a cloned repo is untouched.
- **Listings.** Root listing delegates to the real backend, then merges virtual prefixes in (deduped against real leftovers). Inside a prefix, listings come purely from `entries()`. No "(virtual)" markers in output; the rendered instruction files state which paths are virtual, tool-readable only, and invisible to the shell.
- **Search.** Scoped inside a prefix → provider grep only. Root-scoped → real results merged with provider matches in the same `path:line:text` format, `SEARCH_RESULT_HARD_CAP` applied to the merged set.
- **Mutations.** On a read-only virtual path, `write_file`, `append_file`, `mkdir`, `delete_file`, `delete_directory`, and `move` (either endpoint) raise `VirtualPathError` with a teaching message: *"tools/ is a virtual, read-only directory generated from the live tool registry; its files cannot be modified. Copy a file out if you want an editable version."* `copy` **from** virtual **to** real is allowed — the escape hatch that message names. On a writable path, `write_file`/`append_file` write through; `delete`/`move` still raise.
- **Boot sweep of leftovers.** `tools/`: if a real `tools/README.md` exists and its first line is the generated marker (`# Available Tools`), delete the directory — old snapshots converge and the shell stops showing stale docs; no marker → leave it (user's own dir) and log. `instructions.md` / `task_brief.md`: delete real copies unconditionally (system-authored; the provider is authoritative). Writable paths use mtime reconciliation instead (above). Sweeps are logged and non-fatal.
- **`_LOOP_MAIN_GITIGNORE`** keeps its entries as legacy protection for old snapshots; nothing is added.

## Failure modes

| Case | Behaviour |
|---|---|
| Contacts/plan fetch fails, cache warm | Serve stale + log warning (TTL keeps retrying) |
| Fetch fails, no cache | Read returns a "temporarily unavailable" tool-error string; boot never blocks |
| Fetch slow | ~3s client timeout, then the stale/error path |
| **Writable-path write fails** | Tool error surfaced to the agent (retryable); content preserved in cache; never silently dropped |
| Provider raises unexpectedly | Overlay catches, logs, surfaces a readable tool error; the agent loop never crashes |
| Mutation on read-only path | `VirtualPathError` teaching message |
| Shell wrote a shadow file | Reconciled by mtime at the next sweep (ingest or discard), file deleted, logged |
| Sentinel probe on a virtual path | Probes retargeted to `overlay.inner`; a fresh pod still reads as unseeded |
| Real leftover collides with a prefix | Shadowed by design; converged by the boot sweep |
| `VIRTUAL_DIRS_ENABLED=false` | Overlay not installed; **no** fallback materialization (that path is deleted). Deferred tools degrade to short descriptions; Slice 2 paths fall back to real files with DB rows ignored. Emergency switch, not a supported mode |

## Testing

- **Overlay unit matrix:** every overridden method × virtual/real path (+ `move`/`copy` cross-combinations); full-ownership (unknown name under prefix → not-found, never inner); root-listing merge + dedupe; search merge respecting `SEARCH_RESULT_HARD_CAP`; copy-out allowed; read-only mutations raise; writable paths write through; `__getattr__` delegation (guards against backend-interface drift); file-prefix providers report `is_dir=False`.
- **ToolsProvider:** README byte-identical to `generate_tool_index`; entries track a changed tool list without re-registration; registration order captures full docstrings pre-override.
- **InstructionsProvider:** upload/inline beats template; template renders with `has_tool()` conditionals; **sentinel probes see `inner`** — a wiped workspace with a virtual `task_brief.md` still classifies as unseeded (the regression that would reopen the unseeded-workspace bug).
- **ContactsProvider:** rendering matches the contacts format + README index; deterministic slug collisions; one fetch per TTL window; stale-serve and no-cache-error paths; empty-project case.
- **Slice 2:** write-through round-trip (agent write → DB row → orchestrator read); `PlanManager` unchanged and still passing; phase snapshot captures virtual `plan.md` into `archive/`; orchestrator display path reads DB with **no workspace pod running**; mtime reconciliation both directions (newer ingested, older discarded, file deleted); write-failure surfaces a retryable tool error; checkpoint restore writes through the provider.
- **Boot sweeps:** marker → deleted; no marker → preserved + logged; errors non-fatal.
- **Live gate on local k3d** before dev deploy: session — root listing shows `tools/`, read `tools/README.md`, rejected write, contact linked mid-session appears within TTL; worker job — `plan.md` written by the agent is visible in Cockpit **after the pod is deleted**; shell-written shadow `plan.md` is reconciled, not lost. This gate can also discharge the contacts registry's own never-run live gate. CI (Py3.12) is the merge gate.

## Out of scope

Skills migration (scripts are shell-executed and need real files) · shell visibility / FUSE · subdirectories inside virtual prefixes · "(virtual)" markers in listings · `workspace.md` (legacy, already unused per `src/graph.py:419`) · `notes/`, `output/`, `archive/` (genuine agent artifacts, belong on disk) · additional providers (datasources, experts, memory) · a materialization fallback mode · version history / diffs for `job_documents`.

## Companion change to the contacts spec

[[contacts_registry]] §Agent surface is already amended (2026-07-30): materialized files replaced by the ContactsProvider projection; the `_LOOP_MAIN_GITIGNORE` requirement, snapshot-PII concern, edit-overwrite behavior, staleness limitation, and `CONTACTS_MATERIALIZE_ENABLED` gate are struck. File format, raw-address decision, no-`list_contacts` decision, DB schema, API, resolver, and Cockpit sections are untouched.

## Decision log

- **2026-07-30:** General virtual-directory mechanism; tool-layer visibility only (FUSE rejected); live TTL reads over boot snapshots; today's root paths retained; overlay-backend seam using the `SubdirBackend` idiom; flat one-level trees per arXiv 2607.17598. (User.)
- **2026-07-30:** Scope extended to instruction/organization files. `instructions.md` + `task_brief.md` join Slice 1 read-only; `plan.md` + `todos.yaml` become **Postgres-backed read-write** virtual files (Slice 2) so agent state survives pod death and Cockpit can display a live plan without SSH-ing into a workspace. Provider contract designed with `write()` now to avoid a Slice 2 redesign. (User.)
- **2026-07-30:** Sentinel probes on `task_brief.md` must be retargeted to `overlay.inner` as part of Slice 1 — virtualizing it otherwise makes a wiped workspace read as seeded. (Spec, from code audit.)
- **2026-07-30:** Shell-shadow writes on writable paths are reconciled by mtime (ingest-or-discard, then delete), not merely detected — a shell write is absorbed rather than silently lost. (Spec.)
