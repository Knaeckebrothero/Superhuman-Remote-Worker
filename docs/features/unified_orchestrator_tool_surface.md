---
tags:
  - feature
  - architecture
  - tooling
  - orchestrator
  - agent
  - mcp
  - officers
  - sessions
aliases:
  - unified tool surface
  - shared job toolset
  - job management toolset
related:
  - "[[orchestrator_tool_surface_fragmentation]]"
  - "[[source_tree_unification]]"
  - "[[centurion]]"
  - "[[shared_application_action_layer]]"
  - "[[application_tool_surface_baseline]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
---

# One Job-Management Toolset

**Date:** 2026-08-06 (re-scoped same day from a full-catalogue unification draft, per Legate)
**Status:** IMPLEMENTED THROUGH S4 + S6 — all four §6 decisions ratified by the
Legate 2026-08-06; S1 completed 2026-08-06 and S2/S3/S4/S6 completed
2026-08-14. S5 remains pending.
**2026-08-14 officer-boundary amendment:** descriptor plane metadata is live,
but this change deliberately does not activate the proposed background-officer
object-plane ceiling or expand the officer's effective defaults. Existing
effective grants were migrated to canonical names. Evidence tools and the
knowledge-boundary policy remain pending with S5 in
[[officer_supervision_surface]]; sessions and MCP lose nothing.
**Findings authority:** `docs/issues/orchestrator_tool_surface_fragmentation.md`
(F1–F9, verified 2026-08-03).
**Supersedes:** `shared_application_action_layer.md` (2026-07-08) for the job
domain; `application_tool_surface_baseline.md` stays as the inventory snapshot.

---

## 1. Goal

**One job-management toolset that sessions, MCP and the officers share.**
(Legate, 2026-08-06.)

Not a unification of everything: MCP keeps its operator tools (project CRUD,
datasources, DB introspection, sudo, session admin) as its own surface, and the
officer does not inherit them — he doesn't need to create projects. What gets
straightened out is the job domain specifically, because that is where all
three callers do *the same work* — dispatch, supervise, investigate, decide —
with three hand-written vocabularies today: 12 agent-side tools for the
officer, a 14-name session kit, ~30 MCP tools, nine of them the same
operations under different names, and every observability read missing outside
MCP (the night-1 blind-reads incident).

When this is done:

- The job toolset is defined **once** — one name, one description, one output
  format per operation — and every surface registers from that definition.
- Which subset a caller gets is **config** (officer YAML, session tool groups,
  MCP as today), not per-surface reimplementation.
- Officers gain the control-plane reads they're missing (log, audit, chat,
  todos, messages, frozen state) and, if ratified, bounded declared evidence.
  Sessions retain the wider workspace/diff/shell inspection set; MCP loses
  nothing and gains the supervision verb it lacked (`steer_job`).
- Project-scoped callers are scoped server-side (`X-MCP-Scope`), so an
  officer's `list_jobs` covers his century, not the whole visible fleet.

## 2. Where we are (verified 2026-08-06)

Same API, same credentials, same guards on every path — the split is unwritten
client code, not permissions (issue F1/F2). Key facts:

- Agent-side `orchestrator` category: 35 tools; officer gets 12
  (`config/experts/centurion/config.yaml:67-79`) — every mutating verb, almost
  no reads. Sessions with the *Fleet Management* switch on get a fixed 14-name
  kit appended at runtime (`src/api/persistent_session.py:1471-1487`); legacy
  sessions count as on. `steer_worker_job`/`get_stuck_jobs` are selectable by
  expert YAML but unreachable from the UI (presentation list drift —
  `src/core/session_tool_overrides.py`).
- MCP: 105 tools in `orchestrator/mcp/server.py`; bodies are thin
  (`client.get_*()` + `format_*()`). The client (`mcp/client.py`, 109 methods)
  and formatter layer (`services/formatters.py`, stdlib-only) live in the
  orchestrator package, which the agent image doesn't ship — that packaging
  choice is the whole barrier (issue F8). The MCP image itself hand-grafts
  `formatters.py` via Dockerfile COPY (`docker/Dockerfile.mcp:62-68`).
- Nine operations are spelled differently per surface (`get_job` /
  `get_worker_job`, …) while hitting the same routes (issue F9).
- `X-MCP-Scope: project:<uuid>` narrowing exists server-side and agent-side
  tools never send it (issue F5).

## 3. The toolset

The single source of truth once built; until then this table is the alignment
artifact. Canonical names are the MCP spellings (they have consumers outside
this repo); the legacy agent-side spellings are **renamed in place — no
aliases, no deprecation cycle** (Legate 2026-08-06). The rename sweep of
in-repo configs and tests rides S3; DB-stored configs naming legacy spellings
get a one-time scan (§8). "Default" = granted without per-caller
config: `S` sessions (job groups on), `O` officer, `M` MCP. Anything can still
be granted or removed per expert/session config — defaults are just the
starting kit. The one exception is a commissioned background officer's structural
object-plane denial: config may remove more tools but cannot restore those capabilities.

### job_control (writes)

| Canonical | Replaces | Default | Notes |
|---|---|---|---|
| `create_job` | `create_worker_job` | S O M | keeps `slot=` (officer roster) and `required_deliverables` |
| `cancel_job` | `cancel_worker_job` | S O M | |
| `pause_job` | `pause_worker_job` | S O M | |
| `resume_job_with_feedback` | `resume_worker_job` | S O M | the destructive re-plan verb; description keeps the steer-vs-resume warning |
| `approve_job` | `approve_worker_job` | S O M | |
| `steer_job` | `steer_worker_job` | S O M | today agent-only; **new on MCP** — guidance lane, non-destructive |
| `send_message_to_job` | — | S O M | today MCP-only; the mailbox write behind the same guard steer already passes |
| `assign_job`, `promote_job`, `delete_job` | — | M | operator verbs; stay MCP-default-only, grantable elsewhere by explicit config |

### job_inspection (reads)

`O` below now means the **background-officer default under the proposed 2026-08-14
boundary**, not "all inspection." Interactive sessions and MCP keep their prior defaults.
The implementation records a finer `plane=observability|evidence|object` descriptor field
even if Cockpit continues presenting `job_inspection` as one session-facing umbrella.

| Canonical | Replaces | Default | Notes |
|---|---|---|---|
| `list_jobs` | `list_worker_jobs` | S O M | scope-aware (§4.4) |
| `get_job` | `get_worker_job` | S O M | |
| `get_job_summary` | — | S O M | officer rendering omits workspace/object sections; triage only |
| `get_job_progress` | — | S O M | default-on for officer only after the stub/schema repair |
| `get_job_log` | — | S O M | |
| `get_frozen_job` | — | S O M | |
| `get_job_diff` | — | S M | object-plane/current-revision read; officer uses bounded pinned evidence |
| `get_job_file` | `get_job_workspace_file` | S M | Gitea-backed object-plane read; staleness header stays |
| `list_job_files` | `list_job_workspace_files` | S M | object plane |
| `list_job_commits` | — | S M | object plane; a pinned change summary may appear as evidence |
| `get_current_todos`, `get_todos`, `get_todo_archive`, `list_todo_archives` | — | S O M | adopted as-is; folding the four into fewer tools is a later cleanup |
| `get_workspace_overview` | — | S M | object plane |
| `get_shell_state` | — | S M | object plane; the only live pod-proxied read, unavailable for off-mesh VMs |
| `list_message_threads`, `get_message_thread` | — | S O M | closes F2: the officer can finally read the mailbox he writes into |
| `get_audit_trail`, `search_audit` | — | S O M | |
| `get_chat_history` | — | S O M | |
| `get_stuck_jobs` | — | S O M | already both; becomes UI-selectable via generated lists |
| `list_llm_requests`, `get_llm_request` | — | O M | debugging-grade; default-off for sessions, on for the officer (his job is diagnosing workers) |
| `get_audit_timerange`, `get_audit_bulk`, `get_chat_bulk` | — | M | bulk/operator variants |

**Proposed evidence extension (not part of the 2026-08-06 canonical list):**

| Canonical | Default | Notes |
|---|---|---|
| `get_job_completion_report` | O M | server-recorded report, not a workspace path |
| `list_job_evidence` | O M | immutable typed manifest scoped to the job/project |
| `read_job_evidence` | O M | bounded opaque evidence ID; no caller-supplied path or revision |

The exact initial evidence kinds/bounds remain §10 questions in
[[officer_supervision_surface]]. Until ratified and built, the officer delegates a
tester/recon report instead of receiving arbitrary object tools.

~35 existing operations plus the proposed three evidence operations. Everything else on either surface — `get_session_context`,
`get_current_project`, `list_project_jobs`, repositories, catalog, workflows,
knowledge, projects/datasources/sessions/db/sudo admin — **does not move**.

## 4. Design

### 4.1 Shared package: `src/shared/orch_surface/`

Move `orchestrator/mcp/client.py` and `orchestrator/services/formatters.py`
into `src/shared/orch_surface/` **wholesale** (moving whole files is less
surgery than splitting them; the non-job MCP tools simply import from the new
home). `src/` ships in all three images today — agent and orchestrator already
COPY it; the MCP image swaps its per-file formatter graft for
`COPY src/shared` (+ a Tiltfile sync line), killing the fallback-import dance.
This is also exactly where the source-tree flattening
(`source_tree_unification.md`, decided) puts shared code — the package is born
in its final home, and the flattening census regenerates around it.

Constraints: stdlib + `httpx` only; no langchain/langgraph; no imports from
`src.core`/`src.tools`/`orchestrator.*`. Adapters are the only
framework-aware modules, and the langchain adapter is imported only by the
agent runtime.

### 4.2 One descriptor per job tool, three adapters

Each toolset row becomes one async handler + metadata in
`src/shared/orch_surface/jobs/`:

```python
@descriptor(group="job_inspection", plane="observability",
            default={"session", "officer", "mcp"}, grant="explicit")
async def get_audit_trail(client: SurfaceClient, ctx: CallerCtx,
                          job_id: str, limit: int = 50) -> str:
    """Get the audit trail for a job: tool calls, decisions, transitions…"""
    return fmt.format_audit_trail(await client.get_audit_trail(job_id, limit=limit))
```

The handler owns the client call and the formatter call → identical output on
every surface. Both runtimes already derive schemas from signatures +
docstrings, so no second schema language.

- **MCP adapter**: registers the job descriptors with FastMCP; the other ~70
  MCP tools keep their existing hand-written bodies (now importing client +
  formatters from the shared package). `steer_job` appears as a new MCP tool.
- **LangChain adapter**: `create_orchestrator_tools(context)` keeps its
  signature; the job tools in `src/tools/orchestrator/jobs.py` are replaced by
  the descriptor loop. The same change renames every in-repo reference —
  centurion YAML, the session runtime append list, the sitrep prompt text,
  tests — since there are no aliases to bridge. The
  project/repository/catalog/workflow tool files stay as they are.
- **Manifest generation**: the session tool-group lists (`job_control`,
  `job_inspection` as UI switches), the cockpit
  `agent-settings.types.ts` mirror, and a markdown catalogue under `docs/` are
  generated; the hand-list in `session_tool_overrides.py` and its mirror test
  go away for the job groups.

Descriptor metadata feeds the existing agent registry (category, `grant`,
phases, and the proposed supervision `plane`) so phase-gating,
`validate_tool_override_fragment`, and the background-officer capability ceiling see one
world.

### 4.3 Groups and config

Two user-facing groups take the job slice out of the flat `orchestrator` category,
which lives on for the untouched non-job tools (`get_session_context`,
projects, repositories). Because `validate_tool_override_fragment` rejects
names filed under the wrong category, every in-repo config that lists job
tools under `orchestrator:` flips in the same commit — the validator makes the
rename atomic by construction. Centurion config flips to:

```yaml
tools:
  job_control: true      # per-tool defaults from the descriptor table
  job_inspection: true   # officer resolver admits observability + approved evidence only
  orchestrator: [get_session_context]   # plus the untouched non-job tools he keeps
```

For sessions, that remains a convenient all-inspection switch. For a runtime with
`officer.enabled=true`, the generated grant applies the hard ceiling from
[[officer_knowledge_plane]] and [[officer_supervision_surface]]: object-plane descriptors
are removed even if an override names them. This is a caller capability rule, not a second
hand-written tool list.

### 4.4 Project scoping

The shared client sends `X-MCP-Scope: project:<uuid>` whenever `CallerCtx`
carries a single project binding — officers always, sessions when
project-bound, MCP as its auth already does. Server side already consumes it;
this is finishing the client half. Fixes the officer's fleet-wide job lists
(conference finding F1) with no endpoint changes.

## 5. Slices

| # | Slice | Depends on | Acceptance |
|---|-------|-----------|------------|
| S1 | Move client + formatters to `src/shared/orch_surface/`; all imports switch; `Dockerfile.mcp` graft → `COPY src/shared`; Tiltfile sync line | — | MCP `tools/list` byte-identical (`schema_artifact.py` bake); three images build; MCP restarts on a `src/shared` touch in Tilt |
| S2 | Job descriptors + MCP adapter for the toolset rows; `steer_job` lands on MCP | S1 | Existing MCP job tools name/schema/output-identical (artifact + recorded sample-call diff); steer_job smoke on k3d |
| S3 | LangChain adapter replaces the 12 agent job tools + in-repo rename sweep (centurion YAML, session append list, sitrep text, tests) | S2 | Canonical names resolve; `grep -r` finds no legacy spelling in-repo; updated `tests/test_orchestrator_jobs_tool.py` + session tool tests green; officer k3d turn works on new names |
| S4 | Generated group lists + cockpit mirror for `job_control`/`job_inspection`; descriptor plane metadata and caller defaults | S3 | generated-file CI check; session toolset selectable; current officer grant snapshot is canonical and unchanged; cockpit Vitest green. The proposed object-plane ceiling is an S5 policy activation, not part of this implementation slice. |
| S5 | Activate any ratified expanded background-officer defaults, the object-plane ceiling, and bounded evidence policy | S4 | Pending: officer reads the ratified observability/evidence subset on a live worker job without arbitrary file/shell/repository readers; session switch retains the full inspection set |
| S6 | Scope header in the shared client | S1 | concurrency tests prove header isolation/reset; k3d: project-bound `list_jobs` returns only that project's jobs; unscoped callers remain unscoped |

The officer stopgap (hand-writing ~8 readers into today's `jobs.py`) stays
rejected: the Resavio officer is held pending the `project_officers` migration,
and S1–S3 are mechanical.

## 6. Decisions — all ratified by the Legate 2026-08-06

1. **Package placement** — `src/shared/orch_surface/` (§4.1). **DECIDED.**
2. **Descriptor format** — Python decorators, no YAML manifest. **DECIDED.**
3. **Session defaults** — both `job_control` and `job_inspection` ride the
   existing Fleet Management switch (one user-facing concept: "manage jobs
   from this session"), with `job_inspection` additionally default-on for
   project-scoped sessions. **DECIDED.**
4. **Name convergence** — canonical = MCP spellings, **hard rename, no
   aliases, no deprecation cycle** (amended from the drafted alias
   recommendation). **DECIDED.**

The 2026-08-14 operating-boundary review does not reopen those four decisions. It proposes
one additional grant decision: background officers receive control-plane observability,
not the object-plane subset of `job_inspection`; bounded evidence is the explicit bridge.
That amendment and its initial bounds remain **PROPOSED** in
[[officer_supervision_surface]] and must be ratified before S5.

## 7. Verification

- **Byte-compat gate (S1/S2):** `schema_artifact.py` + `image_smoke.py`
  tools/list bake before/after; recorded sample-call transcript (one call per
  toolset row against a k3d fixture job) diffed on rendered text.
- **Unit gates:** `pytest tests/test_orchestrator_jobs_tool.py`, session
  tool-group tests, `ruff`, cockpit vitest for generated types; full suite at
  slice boundaries (local baseline: 8 known env failures).
- **k3d end-to-end (S5/S6):** officer supervises and dispositions a live worker job using
  truthful control-plane reads and bounded evidence; resolved tools contain no arbitrary
  file/shell/repository readers; scoped listing; full-inspection session switch walk;
  README smoke path.
- **Tilt discipline:** MCP resource must visibly restart on a `src/shared/`
  edit (the known partial-edits trap).

### 7.1 Implementation record — 2026-08-14

S2/S3/S4/S6 now use 37 shared descriptors (10 control, 27 inspection) under
`src/shared/orch_surface/jobs/`. Each descriptor owns its async handler and
rendering; thin FastMCP and LangChain adapters register the same signature,
docstring, metadata, and handler. `job_control` / `job_inspection` registry
membership, session defaults, the Cockpit mirror, and the committed markdown
catalogue are generated or derived from those descriptors. The generator is
checked with `python scripts/generate-job-surface.py --check`.

The reviewed MCP schema changes are exactly:

- add `steer_job`;
- add optional `slot` to `create_job`. It is hidden-context-safe and maps to
  `context.officer_slot`, with the explicit argument winning. No
  `project_id`, `user_id`, `thread_id`, or `parent_job_id` public argument was
  added.

A by-name pre/post comparison of the complete MCP artefact found 105 → 106
tools: added `steer_job`, removed none, and changed only `create_job`. Its only
property delta was optional `slot`; its required set was unchanged. The
baseline digest was
`sha256:7f15e6d90b6b00b88cfd5c1f743ef13e3c4684ca764bafae60ea85519840f18f`;
the implemented digest is
`sha256:f78367df43c93988cbf5590e0ace6d60929969ec2c78dfdd00e839b3963e3046`.
`python -m orchestrator.mcp.image_smoke` passed with 106 tools, two fresh
clients, one create mutation, and one urgent steer call. Directly executing
`python orchestrator/mcp/schema_artifact.py` had a pre-edit import-path failure
(`ModuleNotFoundError: src`); the supported module invocation was used for both
artefacts.

Repository verification run:

- focused descriptor/job/MCP/migration gate: 77 passed;
- MCP suite: 146 passed;
- session/policy/config/critic regression set: 962 passed, 1 skipped;
- full Cockpit Vitest: 2,105 passed across 129 files;
- Cockpit i18n parity/hardcoded-copy check and production build passed (the
  build retained its pre-existing bundle/CommonJS budget warnings);
- Ruff check and format check, generated-artifact drift check, `git diff
  --check`, and both Helm CI lint value sets passed.

The broad Python run reached 17,972 passed and 163 skipped after three known
environment-dependent tests were deselected, then reported eight failures.
Across the run and deselections, the local environment limitations were seven
unchanged MCP test-server transport failures, three missing-`arxiv` contract
failures under the system Python, and one absent localhost PostgreSQL test
database. The three research-client tests passed under the repository
virtualenv; the seven MCP transport tests still reproduced there in untouched
MCP-manager code. CI's supported Python is 3.12; the host interpreters used for
those broad runs were 3.14 and 3.13 respectively.

The running local `k3d-srw` cluster applied migration 0156 successfully after
the missing-`tools` SQL NULL case was found and corrected; the final ledger had
zero dirty rows and expert configs had zero SQL NULLs. Live protocol checks
then confirmed 106 tools, optional `create_job.slot`, canonical agent
`create_job`, project-scoped agent and MCP `list_jobs`, non-urgent `steer_job`,
and cancellation of the test job. Temporary project-scoped MCP tokens were
revoked. An earlier urgent steer attempt hit the existing 409 inbound-reply
guard after its tiny test job had already transitioned to `paused`; the image
smoke and unit contracts cover urgent forwarding, and this slice does not
change that unrelated routing/transition behavior.

Migration 0156 hard-renames stored list-form tool policies and splits the old
orchestrator job members into the two new groups. Unexpected historical raw
policy shapes are left untouched rather than guessed or silently narrowed; a
pre-rollout scan remains prudent for databases that predate canonical
save-boundary normalization. The existing critic verdict tool was renamed to
`approve_job_verdict` so the canonical lifecycle `approve_job` has one
unambiguous active schema.

## 8. Risks

- **MCP output drift** → the byte-compat gate per slice; job domain migrates
  with recorded diffs, everything else untouched.
- **Legacy spellings in stored config** → in-repo references flip atomically
  in S3 (validator-enforced). DB-stored expert rows and session
  `config_override.tools` fragments may still name `*_worker_job` /
  `*_workspace_*` tools: S3 includes a one-time scan of those tables; hits are
  rewritten to canonical names (dev first, prod rows before rollout). The
  boundary validator fails loud (400), not silent, on anything missed.
- **Flattening collision** → built in the flattening's target location; if
  flattening runs first, S1 becomes a no-op move.
- **Agent image weight** → client is httpx-only, formatters stdlib-only
  (verified); import constraints keep it that way.
- **Two registries during S2–S3** → descriptor metadata is fed into
  `TOOL_REGISTRY`; legacy entries deleted with S3.
- **Caller-default drift** → generate the officer/session/MCP manifests from descriptor
  metadata and snapshot the resolved background-officer toolset. `workspace.backend:none`
  alone is not the security boundary; runtime officer filtering is.

## 9. Non-goals

- Unifying the non-job MCP domains (projects, datasources, knowledge, session
  admin, DB introspection, sudo, models/catalog) — they stay MCP-only surfaces;
  the officer does not need to create projects.
- The agent lifecycle client (`src/api/orchestrator_client.py`) — different
  auth story, machine-to-machine.
- Cockpit `api.service.ts`; any endpoint behaviour change.
- Workspace-local `kb_*` tools (direct store access, different mechanism).
- Full-catalogue descriptor migration and tool deferral
  (`project_tool_deferral`) — worthwhile later; not needed for a ~35-tool set.
- Turning job tools on for **workers** by default (`default` sets make room
  for it; enabling it is its own product decision).
