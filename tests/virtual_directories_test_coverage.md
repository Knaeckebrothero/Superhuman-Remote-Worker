# Virtual Directories — test coverage map (what's covered vs what isn't)

Companion to `docs/features/virtual_directories.md`. Records exactly what is
verified, by which mechanism, and — the point of this file — **what could not be
covered yet**, why, and how to close each gap.

Last updated 2026-08-01, after the Slice 1 dev live gate (agent image
`sha-f41970a`, job `97c7a8aa`).

Feature scope built so far: **Slice 1** — read-only providers for `tools/`,
`contacts/`, `instructions.md`, `task_brief.md`, served through
`VirtualOverlayBackend`. **Slice 2** (writable `plan.md` / `todos.yaml` backed by
a `job_documents` table) is **not built**; the `writable` / `write()` half of the
provider contract exists and is unit-tested against fakes, but no writable
provider ships.

---

## 1. Covered

### 1.1 Unit tests (run in CI; real filesystem via `tests/_fs_backend.py`, no mocks for the overlay)

| Area | File / selector | What it asserts |
|---|---|---|
| Overlay routing + read path | `tests/test_virtual_overlay.py` (39) | prefix match/normalisation; **full subtree ownership** (unknown name under a prefix → not-found, never a fall-through to a stale real file); nested `main/tools/` stays real; binary reads; `exists`/`is_file`/`is_dir`; `stat` returns **0** for a missing path per the `WorkspaceBackend` contract; provider failure surfaces a readable error; `__getattr__` delegation |
| Listings + search | `tests/test_virtual_overlay.py` | root listing merge + dedupe against a real leftover; listing inside a prefix comes from the provider; flat trees (nothing below a virtual entry); search scoped to a named entry does not leak sibling hits; root search merges real + virtual; `SEARCH_RESULT_HARD_CAP` applied to the merged set; `read_all()` fast path **and** the fallback for providers without it |
| Mutation rules | `tests/test_virtual_overlay.py` | all 7 mutations rejected on a read-only prefix; `move` rejected from either endpoint; **copy-out virtual→real allowed**; writable providers accept `write_file`/`append_file` and still reject delete; provider write failure becomes a readable error |
| Providers | `tests/test_virtual_providers.py` (26) | `ToolsProvider` renders byte-identically to `generate_tool_index` / `generate_tool_description`; **live tool list read per call** (no snapshot); `SingleFileProvider` renders lazily; instruction precedence inline → upload → template; `ContactsProvider` slug collisions, README index, one fetch per TTL window, stale-serve on failure, cold-cache error, empty-project render |
| Wiring, kill switch, sweep | `tests/test_virtual_dirs_wiring.py` (23) | manager wraps/does-not-wrap per `VIRTUAL_DIRS_ENABLED`; registration serves through the manager; marker-checked legacy `tools/` sweep (delete generated / preserve user-owned / non-fatal); legacy write helpers deleted; **deferred tools keep FULL docstrings after `apply_description_overrides` rebinds** (the placebo-test regression — fixture is a genuine `defer_to_workspace` tool with a `model_copy`-capable double, plus guards so it cannot go vacuous); `swap_backend` keeps the overlay in front of the new backend; tripwires pinning the production probe and swap call sites; `workspace.py` constructs when loaded **outside its package** |
| Seeded-content sentinel | `tests/test_virtual_dirs_wiring.py`, `tests/test_workspace_phase0_seed.py` | `.srw_seeded` round-trips through the manager; a **virtual** file cannot mask an unseeded workspace; a legacy workspace with only `task_brief.md` still reads seeded (safe degradation); an empty workspace reads unseeded |
| Kill-switch rollback | `tests/test_workspace_phase0_seed.py:474+` | with the switch off, `instructions.md` and `task_brief.md` are materialized as real files, so the job's first `HumanMessage` is never empty |
| Cloud sync isolation | `tests/cloud_sync/test_base.py` (20) | a registered provider's files **never enter the sync walk** — enforced structurally by unwrapping in `WorkspaceSyncBase.__init__`, not by ignore patterns |
| Contacts endpoint + resolver | `tests/test_contacts_internal_endpoint.py` (2), `tests/test_resolve_project_for_agent.py` (7) | internal-key gate; exactly-one-of `job_id`/`thread_id`; project resolved **server-side** (job branch, thread-via-`thread_mounts`, thread-via-column fallback, malformed UUID → `None`) |

Suite totals at the time of writing: 117 tests across the six files above, plus
the instruction/seed cases in `tests/test_workspace_phase0_seed.py`. Full suite
12146 passed / 3 failed — all three failures pre-existing and branch-independent
(two need a local Postgres; one is the stale `docs/security/endpoint_inventory.txt`
manifest, red since a pre-branch commit).

### 1.2 Live gate — dev, 2026-08-01

Recorded in full in `docs/features/virtual_directories.md` §Live gate. Verified
on a real sandbox-tier worker job plus direct `kubectl exec` inspection of the
workspace pod: virtual reads, listing merge, write rejection with the copy-out
message, copy-out producing a real file, search merge, full-subtree ownership,
`file_exists` agreement, contacts live fetch, `.srw_seeded` present on the real
filesystem, and `instructions.md` / `task_brief.md` / `contacts/` **absent** from
it.

---

## 2. Not covered — and how to close each gap

### 2.1 Cloud sync end-to-end (the PII-leak scenario) — **highest priority**

**Why it matters.** Before the fix, `WorkspaceSyncBase` walked the overlay, so
contact names, emails and phone numbers were uploaded to the user's
OpenCloud/Nextcloud folder, and the return pull's `VirtualPathError` set
`workspace_sync = None` for the session's entire life — silently. The unit test
(`tests/cloud_sync/test_base.py::test_virtual_provider_files_never_enter_the_sync_walk`)
pins the walk, but nothing exercises a real cloud round-trip.

**Why it isn't covered.** Cloud sync is session-only, and the orchestrator MCP
surface has **no session chat-send** — only jobs can be driven programmatically.
The gate therefore cannot be scripted from an agent.

**How to close.** A human drives a Cockpit session on a project that has at
least one contact linked, with cloud sync enabled:
1. Confirm no `contacts/`, `instructions.md` or `task_brief.md` object appears in
   the project's cloud folder.
2. Run ≥3 turns; confirm no `workspace_sync.error` frame is emitted.
3. **Restart the session** and confirm the initial pull succeeds and
   `workspace_sync` is not `None` / `degraded: true`. Step 3 is the one that
   catches the silent-disable half.

### 2.2 Workspace-tier upgrade with the overlay live

**Why it matters.** `workspace_manager._backend = new_backend` orphaned the
overlay; after a virtual→sandbox upgrade every virtual path 404'd, including the
deferred-tool docs. `swap_backend` fixes it and is unit-tested, but no test drives
a real upgrade.

**Why it isn't covered.** The in-process upgrade needs a live provisioner, a real
second backend, and an SSH-capable pod — see
`tests/workspace_tier_upgrade_test_coverage.md`, which documents the same
limitation for the tier feature itself.

**How to close.** Run a worker job on the **virtual** tier, trigger
`request_workspace_upgrade` to sandbox mid-job, then from the agent read
`tools/README.md`, `tools/<a deferred tool>.md` and `instructions.md`. All three
must still return content. Verify `overlay.inner` is the new backend in the pod
log, and that the old backend was disconnected (not the new one).

### 2.3 Resume paths that the sentinel guards

**Why it matters.** These are the destructive branches: a false "unseeded"
reading makes `recover_to_phase` overwrite `checkpoint.db`, `plan.md`,
`todos.yaml` and `archive/`, and makes a git-less content-bearing workspace reach
`initialize()`'s `rm -rf`.

**Why it isn't covered.** Both need a genuine resume of a pod that already holds
seeded content plus at least one phase snapshot — not reproducible in-process.

**How to close.** Two runs:
1. Same-pod resume (cooldown pause/resume, freeze-continue, or outage-sweeper
   redispatch) of a sandbox/VM job **after** a phase snapshot exists. Confirm the
   log does **not** say "VM workspace is fresh — seeding from last snapshot", and
   that `plan.md` / `todos.yaml` are not reverted.
2. Resume a job with `git_versioning: false` on a sandbox backend and confirm the
   workspace is not wiped.
Also worth covering once: a **legacy** workspace (seeded before `.srw_seeded`
existed, carrying only a real `task_brief.md`) must still read as seeded.

### 2.4 Kill-switch smoke on a real job

**Why it matters.** With `VIRTUAL_DIRS_ENABLED=false` the write path is deleted,
so the fallback materialization is the only thing standing between the switch and
an agent that is never told its task.

**Why it isn't covered.** Unit tests assert the two files are written; nothing
asserts what the agent's **first message actually contains** on a real run.

**How to close.** One worker job with the switch off; read the first
`HumanMessage` from the audit trail and confirm it carries the task brief and
instructions, not just the strategic-mode boilerplate.

### 2.5 Subagent reader isolation

A reader's `tools/` should reflect the **reader's own** tool subset, not the
parent's. The binding is a one-line closure in
`src/tools/delegation/reader_env.py` over a local that is never rebound, verified
by reading the code and by the final review — but no automated test drives
`spawn_subagent` and inspects the reader's overlay.

**How to close.** Spawn a light subagent whose tool set differs from the parent's
and assert its `tools/README.md` lists only its own tools.

### 2.6 Slice 2 surface (not built)

`writable` / `write()` and `EntryMeta.mtime` exist and are unit-tested against
fake writable providers, but no writable provider ships, and none of the Slice 2
machinery (the `job_documents` table, write-through to Postgres, mtime shadow
reconciliation for shell writes) exists. Nothing to test yet; listed so the
contract's tested-but-unused half is not mistaken for dead code.

---

## 3. Known gate finding (open, minor)

`config/worker_base.yaml:50` still lists `tools/` in the workspace `structure`,
so `WorkspaceManager.initialize()` creates an **empty real `tools/` directory**
that nothing writes to. Harmless — the overlay owns the whole prefix, and the
boot sweep correctly leaves it alone because it carries no generated marker — but
it is dead scaffolding and should be dropped from `structure`.
