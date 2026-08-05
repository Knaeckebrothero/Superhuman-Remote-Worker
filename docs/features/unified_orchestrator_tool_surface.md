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
  - shared tool catalogue
  - one catalogue
related:
  - "[[orchestrator_tool_surface_fragmentation]]"
  - "[[source_tree_unification]]"
  - "[[centurion]]"
  - "[[shared_application_action_layer]]"
  - "[[application_tool_surface_baseline]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
---

# Unified Orchestrator Tool Surface

**Date:** 2026-08-06
**Status:** DRAFT — design for review; execution not started.
**Findings authority:** `docs/issues/orchestrator_tool_surface_fragmentation.md`
(F1–F9, verified 2026-08-03). This doc is the execution plan for the decision
recorded there: *stop maintaining separate orchestrator tool surfaces* (Legate,
2026-08-03 — "otherwise we have 20 different ways agents interact with the
orchestrator and that's a maintenance nightmare").
**Supersedes:** `shared_application_action_layer.md` (2026-07-08) — same core
idea ("own product-level tool semantics once, expose through thin adapters"),
now grounded in the verified inventory and sequenced against the source-tree
flattening. `application_tool_surface_baseline.md` remains useful as the
2026-07-08 inventory snapshot.

---

## 1. Goal

One tool catalogue, defined once, consumed by every LLM-facing runtime. When
this feature is done:

- **Adding a tool is one edit**: a descriptor (handler + metadata) in the shared
  package. The MCP registration, the LangChain tool, the session tool-group
  lists, and the cockpit type mirror are all derived from it. Today the same
  change is ~9 synchronised edits on the agent side plus 3 more for MCP parity
  (issue F6).
- **Equipping a caller is config, not code**: "give the officer what an operator
  has" becomes naming a category group in `config/experts/centurion/config.yaml`.
  Today it is impossible — the officer's runtime cannot reach 98 of the 133
  tool names because their bodies were never written on his side (issue §3).
- **The vocabulary is one vocabulary**: the same operation has the same name,
  description, and output format whether called from Claude Code over MCP, a
  persistent session, an officer, or (later) a worker. Today nine operations
  are spelled differently per surface and even the six shared names format
  output differently (issue F3/F9).
- **Project-scoped callers are scoped**: agent-side calls carry
  `X-MCP-Scope: project:<uuid>` so an officer's job lists cover his century,
  not the whole user-visible fleet (issue F5; conference finding F1).

The immediate product payoff is the Centurion: his night-1 false convictions
came from a kit with every verb that *changes* a job and almost none that
*observes* one. The structural payoff is that the next 50 tools cost one file
each.

## 2. Where we are (verified 2026-08-06)

Two hand-written LLM-facing surfaces over the same REST API with the same
credentials and the same server-side guards:

| Surface | Definition | Tools | Consumers |
|---|---|---|---|
| MCP server | `orchestrator/mcp/server.py` (105 `@mcp_tool`) + `mcp/client.py` (109 typed methods) + `services/formatters.py` (~85 `format_*`) | 105 | Claude Code, external MCP clients |
| Agent `orchestrator` category | `src/tools/orchestrator/{jobs,projects,repositories,workflows,catalog}.py` — closures over `ToolContext`, private ad-hoc formatting | 35 | persistent sessions, officers, conferences |

- **The packaging asymmetry is the entire barrier** (issue F8). The orchestrator
  image ships `orchestrator/` + `src/` and imports `src.core.*` freely
  (`docker/Dockerfile.orchestrator:79-85`). The agent image ships only `src/`
  (`docker/Dockerfile.agent:135-137`) — it cannot import the MCP client or the
  formatter layer. The MCP image hand-grafts `services/formatters.py` +
  `security/anti_framing.py` via Dockerfile COPY (`docker/Dockerfile.mcp:62-68`)
  and reaches them through a fallback import dance.
- **Who gets what today**: the officer gets 12 names
  (`config/experts/centurion/config.yaml:67-79`). A session whose *Fleet
  Management* switch is on gets a fixed 14-name kit force-appended at runtime
  (`src/api/persistent_session.py:1471-1487`); sessions created before the
  switch existed count as on (marker-absent = enabled,
  `persistent_session.py:161-176`). Neither kit contains a single
  observability read beyond the two Gitea file readers.
- **Correction to the issue doc**: the 14-name list in
  `src/core/session_tool_overrides.py` is now *presentation-only* (the four UI
  switches); request-boundary validation moved to
  `src/core/tool_policy.validate_tool_override_fragment`, which accepts any
  registry-true name. So `steer_worker_job` / `get_stuck_jobs` /
  `checkout_project_repository` are unreachable *from the UI*, but an expert
  YAML or a raw API fragment can grant them. The drift is a UI gap, not a
  policy gate.
- **The missing 98 names** are catalogued in the issue doc's appendix. The
  high-value block for supervision is all read-only: job log, audit trail,
  chat history, todos, diffs, commits, message threads, frozen state, shell
  state, LLM requests.
- `orchestrator/services/formatters.py` imports stdlib only (`json`, `re`,
  `typing`) — it can ship in the agent image at zero dependency cost.
- `src/` is a real package (`src/__init__.py` exists) and the orchestrator
  already imports ~20 modules from it. Shipping shared code *in `src/`* is the
  established arrangement, not a new invention.

## 3. Design

### 3.1 The shared package: `src/shared/`

Create `src/shared/orch_surface/` (working name; final name is Decision 1):

```
src/shared/
  __init__.py
  orch_surface/
    __init__.py
    client.py        # moved from orchestrator/mcp/client.py (109 methods)
    formatters.py    # moved from orchestrator/services/formatters.py
    context.py       # CallerCtx: user_id, project_id, thread_id, surface
    categories.py    # shared category vocabulary + group definitions
    descriptors/     # one module per domain: jobs.py, audit.py, projects.py, …
    adapters/
      mcp.py         # FastMCP registration from descriptors
      langchain.py   # agent-runtime @tool generation from descriptors
      manifest.py    # generated allowlists / cockpit types / docs
```

Why `src/shared/` and not a new top-level package (Decision 1 rationale):

- It works in **all three images today**: agent and orchestrator images already
  ship `src/`; the MCP image adds one `COPY src/shared /app/src/shared` line
  (plus `src/__init__.py`) — the same graft mechanism it already uses for
  formatters, minus the per-file cherry-picking. The formatters fallback-import
  dance dies; one canonical path (`src.shared.orch_surface…`) everywhere.
- It is **exactly where the source-tree flattening wants it**. The flattening
  plan (`source_tree_unification.md`, decided, awaiting green-light) targets
  `src/{agent,orchestrator,mcp_server,vm_controller,shared}` and already
  amended *formatters.py → shared/*. Building here now means the flattening
  sweep moves nothing for this package — it is born in its final home. The
  flattening census (step 0) regenerates its manifest at execution time, so
  landing this first is additive, not conflicting.
- Import constraints (enforced by review now, import-linter once the flattening
  lands): stdlib + `httpx` only; **no langchain, no langgraph, no imports from
  `src.core`/`src.tools`/`orchestrator.*`**. The adapters are the only modules
  allowed to know about their runtime's framework, and the langchain adapter is
  imported only by the agent runtime.

### 3.2 One descriptor, N adapters

Each tool is defined once as an async handler plus metadata. Sketch:

```python
# src/shared/orch_surface/descriptors/audit.py
@descriptor(
    category="job_inspection",
    surfaces={"mcp", "session", "officer"},
    grant="explicit",                       # carried into the agent registry
    aliases=(),                             # legacy spellings, see §3.5
)
async def get_audit_trail(client: SurfaceClient, ctx: CallerCtx,
                          job_id: str, limit: int = 50) -> str:
    """Get the audit trail for a job: tool calls, decisions, transitions…"""
    data = await client.get_audit_trail(job_id, limit=limit)
    return fmt.format_audit_trail(data)
```

The handler owns the client call *and* the formatter call, so output is
byte-identical on every surface. Both runtimes already derive schemas from
signatures + docstrings (FastMCP and LangChain `@tool` alike), so the
descriptor needs no second schema language — this is Decision 2's
recommendation: **Python descriptors, no YAML manifest**.

Adapters:

- **MCP** (`adapters/mcp.py`): registers every descriptor with `"mcp"` in
  `surfaces` under its canonical name; binds `client` from the MCP auth
  context exactly as `_get_client()` does today. `orchestrator/mcp/server.py`
  shrinks to auth + transport + the registration loop.
- **Agent runtime** (`adapters/langchain.py`): `create_orchestrator_tools(context)`
  keeps its signature but becomes a loop: for each descriptor whose surfaces
  admit the caller, wrap the handler in a LangChain `@tool`, binding the client
  from `ToolContext` (`user_id`, and `project_id` for scope — §3.4). The five
  files under `src/tools/orchestrator/` are deleted.
- **Manifest** (`adapters/manifest.py`): generates the session tool-group
  lists, the cockpit `agent-settings.types.ts` mirror, and a markdown catalogue
  under `docs/`. CI diffs the generated files; the hand-maintained
  `SESSION_TOOL_OVERRIDE_NAMES` and its mirror test are deleted.

Metadata carried per descriptor: `category`, `surfaces`
(`mcp` / `session` / `officer` / `worker` — operator-only judgment is recorded
here per tool, e.g. `query_table` and `reload_experts` stay `{"mcp"}`), `grant`
(`code`/`explicit`, feeding the existing registry classification), `phases`
(worker runtime), `aliases`.

### 3.3 Category re-carve

The flat 35-tool `orchestrator` category and the flat 105-tool MCP server both
dissolve into shared groups. Proposed set (exact membership is the S2
descriptor table, reviewed as a generated catalogue doc):

- `job_control` — create/cancel/pause/resume/approve/steer (+ MCP-only
  assign/promote/delete marked operator)
- `job_inspection` — log, audit, chat, todos, archives, diff, commits, files,
  frozen state, shell state, LLM requests, message threads
- `fleet` — agents, stats, stuck jobs
- `projects` — current/get/list, members (writes operator), project jobs
- `repositories` — today's three
- `datasources`, `sessions`, `db_admin`, `sudo` — MCP-only at first; recorded
  in `surfaces`, available to grant later without new code
- `catalog`, `workflows`, `catalog_authoring` — as today, now shared

Back-compat: the config key `orchestrator` keeps resolving as an alias for the
union of the groups a caller's surface admits, so existing expert YAMLs and the
Fleet Management switch keep working unchanged until re-pointed.

Explicit non-merge: the agent's workspace-local `knowledge` tools (`kb_*`)
talk to the stores directly and are **not** part of this surface; the MCP
knowledge tools remain REST-backed catalogue entries. Unifying those two
mechanisms is a different feature.

### 3.4 Project scoping

The shared client sends `X-MCP-Scope: project:<uuid>` whenever `CallerCtx`
carries a single project binding. Officers always do; sessions do when
project-bound (multi-project sessions send none, unchanged behaviour). The
server side already consumes it (`mcp_scope_project_id()` AND-filter) — this is
purely finishing the client half. Fixes fleet-wide `list_worker_jobs` leakage
without touching any endpoint.

### 3.5 Naming convergence (issue F9)

Canonical names are the MCP spellings (`get_job`, `create_job`, `cancel_job`,
…): they have external consumers (Claude Code configs beyond this repo), the
agent-side spellings live in repo-controlled YAML. The nine renamed pairs
become one descriptor each with the legacy `*_worker_job` / `*_workspace_*`
spelling in `aliases`; the langchain adapter registers aliases as deprecated
synonyms (same handler), the manifest marks them, and after one deprecation
cycle the aliases drop. Centurion/session configs flip to canonical names in
S5. This is Decision 5, option (a) — the alias table is ~9 lines.

### 3.6 Relationship to tool deferral

Deferral (`project_tool_deferral`, unbuilt) is what eventually makes "grant the
whole catalogue" affordable per-caller; it is **not** a prerequisite (Decision
4). Groups are sized for a resident-schema world now — the officer's post-S5
kit lands around ~30 resident tools, well inside session budgets — and S7
flips breadth later without re-carving.

## 4. What each caller ends up with

- **Officer (Centurion)**: `job_control` + `job_inspection` + `projects` +
  `fleet` reads + existing catalogue groups — the operator's investigation kit
  with operator-only writes excluded via `surfaces`. Night-1's missing reads
  (log/audit/chat/todos/diff/messages) all present. Scoped to his century.
- **Sessions**: Fleet Management switch maps to `job_control` (+ the reads it
  already implied); `job_inspection` + `projects` + `repositories` reads become
  default-on for project-scoped sessions (Decision 3 — product call). The
  three UI-unreachable tools become selectable because the switch lists are
  generated from the same descriptors.
- **MCP / Claude Code**: vocabulary unchanged (canonical names *are* the MCP
  names); output text unchanged through S2's byte-compat gate; gains category
  tags in tool annotations.
- **Workers**: no default change. `surfaces` gains a `worker` value so the
  delegation-adjacent subset can be granted per-expert later; actually turning
  any group on for workers is its own product decision.
- **Conferences**: inherit whatever the officer's expert grants — the S9
  conference finding F1 (user-wide reach) is closed by §3.4 scoping, not by a
  separate kit.

## 5. Slices

| # | Slice | Depends on | Acceptance |
|---|-------|-----------|------------|
| S1 | Move `mcp/client.py` + `services/formatters.py` into `src/shared/orch_surface/`; MCP + orchestrator switch imports; Dockerfile.mcp graft becomes `COPY src/shared`; Tiltfile `srw-mcp` sync adds `src/shared/` | — | MCP `tools/list` artifact byte-identical (`schema_artifact.py` bake); all three images build; k3d MCP health + one smoke call green; Tilt edit-signal lands for a `src/shared` touch |
| S2 | Descriptor registry + MCP adapter; the 105 server bodies become descriptors (start with jobs/audit domains, finish the tail mechanically) | S1 | 105 registrations, names + schemas + output text unchanged (artifact diff + recorded sample-call diffs); server.py contains no per-tool bodies |
| S3 | LangChain adapter replaces `src/tools/orchestrator/*`; registry metadata sourced from descriptors | S2 | The 35 agent names resolve (canonical or alias); `tests/test_orchestrator_jobs_tool.py` + session tool tests green; officer k3d smoke turns a wake with the same 12 tools |
| S4 | Generated manifests: session groups, cockpit types, catalogue doc; delete `SESSION_TOOL_OVERRIDE_NAMES` hand-list + mirror test | S3 | Generated-file CI check; steer/stuck/checkout selectable in Settings→Tools; cockpit vitest green |
| S5 | Category re-carve + config flips: centurion + session_base name the new groups (canonical spellings); `orchestrator` key aliased | S4 | k3d: officer reads log/audit/chat/todos/diff/messages on a live worker job; session toggles honoured end-to-end; existing expert YAMLs unmodified still resolve |
| S6 | Scope header in the shared client | S1 (can ride any later slice) | k3d: officer `list_jobs` returns only century jobs; unscoped session behaviour unchanged |
| S7 | Deferral for the shared catalogue | `project_tool_deferral` | Officer reaches the full catalogue within resident-schema budget |

S1–S3 are mechanical and carry the risk; S5 is the officer payoff; S6 is small
and can land right after S1 if the officer returns early.

**Stopgap track (not recommended, kept for the record):** hand-writing ~8
readers into `src/tools/orchestrator/jobs.py` buys the officer eyes in ~a day
if he must come off hold before S1–S3 land. The bodies would target the same
routes and be deleted at S3. Since the Resavio officer is deliberately held
pending the `project_officers` migration anyway, the recommendation is to skip
the stopgap and land the real thing.

## 6. Decisions

1. **Package placement** — RECOMMENDED: `src/shared/orch_surface/` (rationale
   §3.1: works in all images today, is the flattening's target layout, kills
   the MCP graft dance). **OPEN — Legate.**
2. **Descriptor format** — RECOMMENDED: Python decorators/dataclasses, no YAML
   manifest (both runtimes already derive schemas from signatures; a manifest
   would be a second schema language with codegen anyway). **OPEN — Legate.**
3. **Default session grant** — RECOMMENDED: `job_inspection` + `projects` +
   `repositories` reads default-on for project-scoped sessions; `job_control`
   stays behind the Fleet Management switch; capability grants remain the PDP
   either way. **OPEN — Legate (product call).**
4. **Does S7 (deferral) block S5 (re-carve)?** — RECOMMENDED: no; size groups
   resident, let deferral widen later. **OPEN — Legate.**
5. **Name convergence** — RECOMMENDED: canonical = MCP spellings, legacy
   agent-side names as deprecated aliases for one cycle (§3.5). **OPEN —
   Legate.**

## 7. Verification

- **Byte-compat harness (S1/S2 gate):** bake `tools/list` via the existing
  `orchestrator/mcp/schema_artifact.py` + `image_smoke.py` path before and
  after each slice; additionally record a fixed sample-call transcript (one
  read per domain against a k3d fixture job) and diff the rendered text. MCP
  consumers see zero drift until the deliberate S5 config flips.
- **Per-slice unit gates:** `pytest tests/test_orchestrator_jobs_tool.py
  tests/test_session_tool_group_mirror.py` (until S4 replaces the latter),
  `ruff check src/ orchestrator/ tests/`, cockpit `vitest` for the generated
  types, full `pytest tests/ -x -q` at slice boundaries (baseline: 8 known
  local env failures).
- **k3d end-to-end (S5/S6 gate):** provision a smoke officer against a live
  worker job; verify each new read returns real data; verify scoped listing;
  walk a session through Settings→Tools enabling `job_inspection`; run the
  README smoke path to catch collateral.
- **Tilt discipline:** every slice's edit-signal check (uvicorn reload / MCP
  watchfiles restart) — the MCP resource must visibly restart on a
  `src/shared/` touch, else the sync line is wrong (the known
  partial-edits trap).

## 8. Risks

- **Output drift breaking MCP consumers** — the harness above; S2 migrates
  domains incrementally with the diff gate per domain.
- **Expert YAML / stored session config breakage** — aliases (§3.3, §3.5) keep
  every existing spelling resolving; S5 flips only in-repo configs; DB-backed
  expert rows are re-validated against the registry which now knows aliases.
- **Flattening collision** — mitigated by building in the flattening's own
  target location; the census regenerates manifests; if flattening goes first
  instead, this feature's S1 becomes "move into the already-existing shared/".
- **Agent image weight / dependency creep** — formatters are stdlib-only
  (verified); the client is httpx-only; the import constraint in §3.1 keeps it
  that way, enforced by import-linter post-flattening.
- **Two registries during S2–S3** (descriptors + legacy `TOOL_REGISTRY`) — the
  langchain adapter feeds descriptor metadata *into* `TOOL_REGISTRY` so
  phase-gating, grant classification, and `validate_tool_override_fragment`
  see one world; the legacy hand entries are deleted with S3.
- **Session context budget** — post-S5 officer/session kits grow to ~30
  resident tools; acceptable now, S7 is the structural answer if defaults
  widen further.

## 9. Non-goals

- The agent **lifecycle** client (`src/api/orchestrator_client.py` —
  register/heartbeat/completion) stays separate: machine-to-machine, different
  auth story.
- The cockpit's `api.service.ts` is untouched.
- No endpoint behaviour changes — this is client-side consolidation only.
- Workspace-local `kb_*` tools stay a separate mechanism (§3.3).
- Turning any group on for **workers** by default.
