# Orchestrator tool-surface fragmentation — one API, many hand-written front doors

**What this is:** a design issue, not a bug report. The orchestrator's REST API is
exposed to LLM callers through several independently hand-maintained tool surfaces
plus a dozen ad-hoc HTTP clients. They share no client code, no formatters, no tool
registry, and no category vocabulary. They drift, and the drift has already cost
field incidents: the Centurion's night-1 false convictions
(`officer_blind_reads_and_worker_bureaucracy.md`) were caused by his surface lacking
readers that the MCP surface has had since February.

The proposal is to collapse them onto one shared core — client + formatters + tool
registry — with thin per-runtime adapters, so that MCP, persistent sessions, officers
and workers select from *the same* catalogue instead of each maintaining a private
subset of it.

**Status:** decided and in execution — see
`docs/features/unified_orchestrator_tool_surface.md` (2026-08-06), which
re-scoped the proposal to **one shared job-management toolset** (not a
full-catalogue unification) and records the ratified decisions; this file
remains the findings record. Filed 2026-08-03 out of the officer-instrument
investigation. The full-catalogue idea and `project_tool_deferral` (unbuilt)
stay related background — deferral is what would make a large shared catalogue
affordable per-caller.
Related: `officer_blind_reads_and_worker_bureaucracy.md` (F1–F3 are symptoms of this
issue), `agent_tool_fixed_vocabularies_invisible_to_model.md`,
`docs/features/centurion.md` §4, and
`docs/features/officer_supervision_surface.md` (2026-08-14 caller-boundary amendment:
shared implementation does not imply identical default capabilities).

---

## 1. Inventory

| Surface | Where | Tools | LOC | Consumers | Auth headers |
|---|---|---|---|---|---|
| MCP server | `orchestrator/mcp/server.py` + `mcp/client.py` + `services/formatters.py` | **104** | 8,570 | Claude Code, external MCP clients | `X-Internal-Key` + `X-MCP-User-Id` + `X-MCP-Scope` |
| Agent `orchestrator` category | `src/tools/orchestrator/{jobs,projects,repositories,workflows,catalog}.py` | **35** | 3,593 | persistent sessions, officers | `X-Internal-Key` + `X-MCP-User-Id` |
| Agent lifecycle client | `src/api/orchestrator_client.py` | 40 methods (not LLM-facing) | 1,912 | agent runtime | `X-Internal-Key` |
| Ad-hoc `httpx` callers | 13 modules under `src/` | — | — | knowledge, messaging, loop, canvas, capabilities… | varies |
| Cockpit | `cockpit/src/app/core/services/api.service.ts` | — | 2,397 | UI | Bearer / OIDC |

Thirteen modules under `src/` construct their own `httpx.AsyncClient`; thirteen read
`ORCHESTRATOR_URL` independently. There is no shared timeout, retry, error-shape or
auth policy across them.

The two LLM-facing surfaces are **almost entirely disjoint vocabularies**: 104 + 35
tools, 133 distinct names, and exactly **6 names in common** (`get_expert`,
`get_skill`, `get_stuck_jobs`, `list_experts`, `list_project_jobs`, `list_skills`).
Not because they cover different ground — see F9 — but because the same operations
were named differently on each side.

Of the agent-side 35, a Centurion is given **12** (`config/experts/centurion/config.yaml:67-79`)
and a user-configurable session may select **14** (`src/core/session_tool_overrides.py:20-36`).
A default session gets **0** — `config/session_base.yaml:117` ships
`orchestrator: [ ]` as an opt-in group.

---

## 2. Findings

| # | Finding | Site | Status |
|---|---------|------|--------|
| F1 | **The surfaces are not capability tiers — they are the same API with the same credentials.** `_get_client` sends `X-Internal-Key` + `X-MCP-User-Id`; the MCP client's `set_scope_headers` sends the same two plus `X-MCP-Scope`. Both target `:8085/api/…`; both satisfy `require_job_access`. Nothing about identity, transport or privilege separates a session tool from an MCP tool | `src/tools/orchestrator/jobs.py:182-205`; `orchestrator/mcp/client.py:405-414`; `orchestrator/security/auth.py:631-644` | Open |
| F2 | **Proof by existing behaviour**: the officer already POSTs `/api/jobs/{id}/messages/{thread_id}/reply` on every steer (`"officer"` is a magic thread key on that route). The GET siblings on the same prefix behind the same guard — `/api/jobs/{id}/messages` and `/messages/{thread_id}` — are unwrapped on the agent side. He can write into a mailbox he cannot read. Not forbidden: unwritten | route `orchestrator/main.py:10191`; readers `:10283`, `:10323`; caller `src/tools/orchestrator/jobs.py` `steer_worker_job` | Open |
| F3 | **The formatter layer is not shared.** `orchestrator/services/formatters.py` is 2,649 lines and ~85 `format_*` functions covering every orchestrator read. The agent side re-implements a handful privately and does it worse: `_format_job_summary` drops the `audit_count` the API returns and prints `job['current_phase']` / `job['progress']` — **columns that do not exist on the `jobs` table**, so those two branches have never rendered | `orchestrator/services/formatters.py`; `src/tools/orchestrator/jobs.py:299-336`, `:425-448`; schema `orchestrator/database/schema_current.sql` (jobs) | Open — see companion doc |
| F4 | **The user-selectable allowlist has already drifted from what ships.** The `orchestrator` category ships 35 tools; `SESSION_TOOL_OVERRIDE_NAMES["orchestrator"]` allows 14. Absent from the allowlist but live in code: `steer_worker_job`, `get_stuck_jobs`, `checkout_project_repository` — so a session user cannot enable the two supervision verbs through any UI path; only an expert YAML can. `agent_catalog` allows 5 of 9, `workflows` 7 of 9 (the four `*_bundle` tools are unreachable) | `src/core/session_tool_overrides.py:20-60` vs `src/tools/orchestrator/*.py`; cockpit mirror `cockpit/src/app/views/agent-settings/agent-settings.types.ts:62`; pinned by `tests/test_session_tool_group_mirror.py` | Open |
| F5 | **The project-scoping mechanism exists and the agent tools don't use it.** `X-MCP-Scope: project:<uuid>` is captured into `user['scopes']` and consumed by `mcp_scope_project_id()` as an explicit AND-filter. The agent-side client never sends it. This is exactly why the officer's `list_worker_jobs` returns the whole visible fleet instead of his century — and why `GET /api/jobs` was never given a `project_id` param: MCP callers already had a way to narrow | `orchestrator/security/auth.py:642-643`, `security/access.py:225-234`; unused by `src/tools/orchestrator/jobs.py:182` | Open |
| F6 | **Adding one tool costs N synchronised edits.** P0-A (`ef3ec62b`) repointed a single reader and had to touch: the tool definition, `src/core/session_tool_overrides.py`, `src/core/loader.py`, `src/api/persistent_session.py`, `config/experts/centurion/config.yaml`, `config/session_base.yaml`, the sitrep prompt text in `orchestrator/services/session_wake.py`, the cockpit type mirror, and three test files. A tool that also wants MCP parity needs a fourth copy in `mcp/client.py` + `mcp/server.py` + a formatter | `git show ef3ec62b --stat`; mirror test `tests/test_session_tool_group_mirror.py` | Open |
| F7 | **The category vocabulary is agent-private.** `src/tools/registry.py` knows 24 categories (`workspace`, `core`, `research`, … `orchestrator`, `canvas`, `agent_catalog`, `workflows`). The MCP server has no categories at all — 104 flat tools. Neither vocabulary is derivable from the other, so "give the officer what Claude Code has" cannot be expressed as a config change | `src/tools/registry.py:~600-760`; `orchestrator/mcp/server.py` | Open |
| F8 | **MCP tools are already thin adapters**, which is what makes the refactor cheap: a typical body is `client.get_*()` + `fmt.format_*()` and nothing else. The reusable core is *already factored out* on the MCP side; it simply lives in the orchestrator package, which the agent image does not ship | e.g. `orchestrator/mcp/server.py:2716` (`search_audit`); packaging note `src/tools/orchestrator/jobs.py:~215` | Open |
| F9 | **Where both surfaces do cover the same endpoint, the tool is renamed** — so even the overlap is unshareable, and the model-facing vocabulary depends on which runtime you are in. Nine such pairs: `create_job`/`create_worker_job`, `list_jobs`/`list_worker_jobs`, `get_job`/`get_worker_job`, `cancel_job`/`cancel_worker_job`, `pause_job`/`pause_worker_job`, `approve_job`/`approve_worker_job`, `resume_job_with_feedback`/`resume_worker_job`, `get_job_file`/`get_job_workspace_file`, `list_job_files`/`list_job_workspace_files`. The `_worker_` infix encodes the delegation-surface origin (§3), not a semantic difference — both hit the same routes | `orchestrator/mcp/server.py` vs `src/tools/orchestrator/jobs.py` | Open — rename is a breaking change for existing expert configs; see §6.5 |

---

## 3. What the divergence costs, concretely

The Centurion is the sharpest case because he does the same job an operator with MCP
does — supervise and investigate workers — and was dressed from the delegation
surface. `src/tools/registry.py:697` still names that surface's purpose verbatim:
*"Orchestrator tools (job delegation for persistent agents)"*. It was created
2026-03-31 inside "Introduce persistent agent deployment and orchestrator
integration"; the MCP server was created 2026-02-01 and has 52 commits since,
because every wall hit while debugging a job became a tool. The officer is five days
old and inherited whatever existed.

The result is that his twelve tools kept nearly every verb that **changes** something
— create, cancel, pause, resume, approve, steer — and dropped nearly every verb that
**observes** something. Missing on the agent side, present in MCP, all read-only, all
behind gates his credentials already pass:

`list_message_threads`, `get_message_thread`, `get_audit_trail`, `search_audit`,
`get_audit_timerange`, `get_audit_bulk`, `get_chat_history`, `get_chat_bulk`,
`list_llm_requests`, `get_llm_request`, `get_job_log`, `get_current_todos`,
`get_todos`, `get_todo_archive`, `list_todo_archives`, `get_shell_state`,
`get_workspace_overview`, `get_frozen_job`, `get_job_summary`, `list_job_commits`,
`get_job_diff`, `list_job_files`, `get_agent_system_info`, `get_graph_changes`.

`get_shell_state` deserves a note: it proxies to the pod and is the platform's only
genuinely **live** read — everything else is a Gitea snapshot. (Caveat: pod-only; the
orchestrator is off-mesh for VM workspaces, and the officer's heavy slot runs on VMs.)

Regular sessions are worse off than the officer, not better: they ship with the group
empty and can opt into 14 of 35. A session asked to "check on that job" has no reader
at all unless someone configured one.

---

## 4. Proposal

**One core, three adapters, one catalogue.**

**4.1 Extract a shared client + formatter package.** The MCP stack's client
(`mcp/client.py`, 2,728 lines of typed REST wrappers) and formatters
(`services/formatters.py`, 2,649 lines) are already the right abstraction — they are
just packaged where only the orchestrator can import them. Move them to a package
both images ship (candidate: a top-level `srw_api/` or an extension of the existing
shared-module arrangement that already lets the orchestrator import
`src.core.session_tool_overrides`). Everything below depends on this step and nothing
else does, so it can land alone and be verified by both images importing it.

**4.2 One tool descriptor, N adapters.** Define each tool once — name, category,
description, arg schema, `client` call, `formatter` call, and a `surfaces` set
(`mcp` / `session` / `officer` / `worker`). Generate:
- the MCP registration (`@mcp.tool`),
- the LangChain `@tool` for the agent runtime,
- the allowlist entries that `session_tool_overrides.py` and the cockpit type file
  currently hand-maintain.

F6's nine-edit change becomes one descriptor plus a generated-file check in CI. The
existing mirror test (`tests/test_session_tool_group_mirror.py`) becomes redundant —
it exists only to police a duplication that would no longer exist.

**4.3 Shared category vocabulary.** Promote the agent's 24 categories to the shared
package and category-tag the 104 MCP tools. Then "give the officer what an operator
has" is `tools: {job_inspection: [...]}` in one YAML, and the same group is available
to sessions, workers and conferences. Suggested new read-side groups, carved out of
today's flat `orchestrator`: `job_inspection` (audit/chat/log/todos/diff/shell),
`job_control` (the mutating verbs that exist today), `fleet` (agents/stats/stuck),
`knowledge_ops`, `catalog`, `workflows`.

**4.4 Pass the scope header.** Agent-side clients should send
`X-MCP-Scope: project:<uuid>` when the caller is project-bound (every officer is).
That fixes fleet-wide leakage on the read side (F5, and conference finding F1) without
adding a `project_id` param to every endpoint.

**4.5 Deferral, not curation.** The reason each surface hand-picks a subset is context
cost. `project_tool_deferral` is the structural answer — the same mechanism this
investigation was conducted under, where a large catalogue is searchable and schemas
load on demand. With deferral, the default answer to "should the officer have this
tool?" becomes *yes* instead of a config negotiation. Without it, 4.3's groups are
still an improvement but the per-caller subsets stay.

**Non-goals.** Not proposing to merge the *lifecycle* client
(`src/api/orchestrator_client.py` — register/heartbeat/report_completion) into this;
it is machine-to-machine, not LLM-facing, and its auth story is different. Not
proposing to touch the cockpit's `api.service.ts`. Not proposing to change any
endpoint's behaviour — this is entirely a client-side consolidation.

---

## 5. Slices

| # | Slice | Depends on | Verifiable by |
|---|-------|-----------|---------------|
| S1 | Extract client+formatters into a package both images import; MCP switches to it with zero behaviour change | — | MCP tool output byte-identical before/after; both images build |
| S2 | Tool-descriptor registry + MCP adapter generated from it | S1 | 104 tools still registered, names/schemas unchanged |
| S3 | LangChain adapter generated from the same descriptors; `src/tools/orchestrator/*.py` becomes descriptors, its private formatters deleted | S2 | Existing 35 tool names/outputs preserved; `tests/test_orchestrator_jobs_tool.py` green |
| S4 | Allowlists + cockpit type file generated; delete `SESSION_TOOL_OVERRIDE_NAMES` hand-list and its mirror test | S3 | F4's three drifted tools become selectable; new mirror check is a generated-file diff |
| S5 | Category re-carve (`job_inspection` etc.); officer + session defaults updated to include the read groups | S4 | Officer can read messages/audit/todos on a live job in k3d |
| S6 | Scope header on agent-side clients | S1 | Officer's `list_worker_jobs` returns only his century |
| S7 | Deferral for the shared catalogue | `project_tool_deferral` | Officer holds ≤12 resident schemas while reaching ~100 tools |

S1–S3 are mechanical and carry the bulk of the risk reduction. S5 is the slice that
actually fixes the Centurion; it is small once S3 lands and could be cherry-picked
early as a stopgap (hand-write ~8 readers into `jobs.py`) if the officer comes off
hold before this lands.

---

## 6. Decisions needed

1. **Package placement.** A new top-level shared package, versus extending whatever
   arrangement currently lets the orchestrator import `src.core.*`. This decides
   whether the agent image grows an orchestrator dependency or both grow a third.
   Interacts with `project_source_tree_flattening` (decided-next) — worth sequencing
   together rather than fighting it.
2. **Descriptor format.** Python objects (typed, refactorable, but codegen at import
   time) versus a YAML/JSON manifest (config-native, matches the rest of the system,
   but a second schema to maintain).
3. **Scope of S5's default grant.** Does a *default* session get the read groups, or
   only officers and opted-in sessions? Cheap either way after S4; it is a product
   call about what a plain chat session should be able to see.
4. **Whether S7 blocks S5.** If deferral is close, the category re-carve could be
   sized for a deferred world (broad groups) rather than a resident one (tight
   groups).
5. **Whether to converge the names (F9).** Collapsing `*_worker_job` onto the MCP
   `*_job` names is the clean end state, but it breaks every expert YAML, the
   session allowlist, the cockpit type mirror and the app-guide roster that spell
   the current names. Options: (a) rename with a descriptor-level alias table so old
   names keep resolving; (b) keep both names as aliases of one descriptor
   indefinitely; (c) leave the divergence and accept that the shared catalogue has
   two spellings for nine operations. (a) is the recommendation — the alias table is
   ~9 lines and can be dropped after one deprecation cycle.

---

## 7. Appendix — the diff, by domain

Agent-side `orchestrator` category, as shipped (35):

- **jobs.py (12)** — `get_session_context`, `create_worker_job`, `list_worker_jobs`,
  `get_worker_job`, `get_job_workspace_file`, `list_job_workspace_files`,
  `approve_worker_job`, `resume_worker_job`, `cancel_worker_job`, `pause_worker_job`,
  `steer_worker_job`, `get_stuck_jobs`
- **projects.py (2)** — `get_current_project`, `list_project_jobs`
- **repositories.py (3)** — `list_project_repositories`,
  `get_default_project_repository`, `checkout_project_repository`
- **workflows.py (9)** — `list_automations`, `get_automation`, `list_automation_runs`,
  `propose_automation`, `get_automation_bundle`, `set_automation_bundle`,
  `get_project_loop`, `list_project_loop_jobs`, `explain_project_loop`
- **catalog.py (9)** — `list_experts`, `get_expert`, `list_skills`, `search_skills`,
  `get_skill`, `get_expert_bundle`, `set_expert_bundle`, `get_skill_bundle`,
  `set_skill_bundle`

MCP-only, by domain (the 98-name gap; read tools unless noted). Nine of these have a
renamed agent-side counterpart rather than being genuinely absent — see F9:

- **Job inspection** — `get_job`, `get_job_summary`, `get_job_progress`, `get_job_log`,
  `get_frozen_job`, `get_job_diff`, `get_job_file`, `list_job_files`,
  `list_job_commits`, `list_job_tags`, `get_workspace_file`, `get_workspace_overview`,
  `get_shell_state`, `get_current_todos`, `get_todos`, `get_todo_archive`,
  `list_todo_archives`, `get_graph_changes`
- **Audit / transcript** — `get_audit_trail`, `get_audit_bulk`, `get_audit_timerange`,
  `search_audit`, `get_chat_history`, `get_chat_bulk`, `get_thread_log`,
  `list_llm_requests`, `get_llm_request`
- **Messages** — `list_message_threads`, `get_message_thread`, `send_message_to_job`
  (write)
- **Fleet / stats** — `list_agents`, `get_agent_stats`, `get_agent_system_info`,
  `get_job_stats`, `get_daily_stats`, `get_memory_stats`, `deregister_agent` (write)
- **Job control not on the agent side** — `assign_job`, `promote_job`, `delete_job`
  (writes)
- **Renamed counterparts, not gaps (F9)** — `create_job`, `create_project_job`,
  `list_jobs`, `get_job`, `cancel_job`, `pause_job`, `approve_job`,
  `resume_job_with_feedback`, `get_job_file`, `list_job_files`. Each has an
  agent-side `*_worker_job` / `*_workspace_*` twin hitting the same route
- **Projects / members / datasources** — `get_project`, `create_project` (write),
  `update_project` (write), `delete_project` (write), `list_projects`,
  `list_project_members`, `add_project_member` (write), `update_project_member`
  (write), `remove_project_member` (write), `list_project_datasources`,
  `list_datasources`, `create_datasource` (write), `update_datasource` (write),
  `delete_datasource` (write), `test_datasource`, `link_datasource_to_project`
  (write), `unlink_datasource_from_project` (write)
- **Knowledge / sources / citations** — `search_knowledge`, `list_knowledge_notes`,
  `get_knowledge_note`, `get_knowledge_summary`, `update_knowledge_note` (write),
  `delete_knowledge_note` (write), `export_knowledge`, `reindex_knowledge` (write),
  `list_job_sources`, `search_job_sources`, `get_source_detail`,
  `get_source_annotations`, `get_source_tags`, `list_job_citations`,
  `get_citation_detail`, `get_citation_stats`
- **Sessions** — `list_persistent_threads`, `get_persistent_thread`,
  `get_persistent_thread_messages`, `get_persistent_thread_ide`,
  `create_persistent_thread` (write), `resume_persistent_thread` (write),
  `end_persistent_thread` (write)
- **DB introspection** — `list_tables`, `get_table_schema`, `query_table`
- **Sudo / approvals** — `list_sudo_requests`, `approve_sudo_request` (write),
  `deny_sudo_request` (write)
- **Catalog** — `list_models`, `reload_experts` (write), `reload_skills` (write),
  `get_project_expert`, `list_project_experts`

Note that the gap is not uniformly "reads the agent should have". Several MCP tools
are operator-only by intent (`delete_project`, `query_table`, `reload_*`). The
descriptor's `surfaces` field (4.2) is where that judgement belongs — recorded once,
per tool, instead of implied by which file someone happened to write it in.
