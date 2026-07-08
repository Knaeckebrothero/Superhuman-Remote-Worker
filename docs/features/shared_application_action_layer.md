---
tags:
  - feature
  - architecture
  - tooling
  - mcp
  - sessions
  - orchestrator
aliases:
  - shared app actions
  - application action layer
  - app action registry
related:
  - "[[mcp]]"
  - "[[builder_to_sessions_consolidation]]"
  - "[[session_job_management_toolset_rework]]"
---

# Shared Application Action Layer

**Status:** Proposed - design note created 2026-07-08.

## Context

The old builder agent has been removed because persistent sessions can cover the
same interactive workflows with better continuity and application context.
However, the builder tool surface has not yet been folded into sessions, and the
MCP server also needs to track newer application functionality.

Today the application has two different tool surfaces:

- The MCP server in `orchestrator/mcp/server.py`, which exposes FastMCP tools
  backed by REST calls through `AsyncCockpitClient`.
- Persistent session tools in `src/tools/orchestrator/jobs.py`, which expose
  LangChain tools backed by direct orchestrator REST calls.

This creates drift. The same product capability can require one implementation
for sessions, one for MCP, and sometimes another backend route or service. The
MCP surface has also grown into a mix of product actions, diagnostics, and
operator-oriented tools, which makes it a poor primary runtime surface for
sessions.

## Decision

Introduce a shared application action layer that owns product-level tool
semantics once, then expose those actions through thin adapters for sessions,
MCP, and any future surface.

Sessions should not call the current MCP server as their primary internal tool
runtime. MCP remains an external integration protocol and can later be consumed
by sessions for third-party tools, but SRW's own application actions should live
in a shared typed layer.

Project repository checkout should be a first-class session action. For the
near-term SRW development workflow, a persistent session should be able to list
the current project's repositories, clone the selected project repository into
its own workspace, start the application, run tests, inspect the UI/API, and
create follow-up jobs or loop feedback from that live context.

MCP should not expose raw credentialed clone URLs by default. MCP can expose
project repository metadata and job repository inspection now. Direct MCP clone
support can wait until external MCP clients are an important workflow; when
added, it should use a safe clone-capability action rather than leaking secrets
or assuming the orchestrator's internal Gitea URL is reachable from the caller.

## Goals

- Define application actions once and adapt them to sessions and MCP.
- Keep orchestrator REST routes and services authoritative for persistence,
  final job status, access control, and side effects.
- Use structured inputs and outputs instead of formatted strings as the primary
  internal contract.
- Carry caller context consistently: user, project scope, thread/session, MCP
  token scope, permission mode, and audit metadata.
- Support capability and grant checks at both tool registration time and action
  execution time.
- Reduce MCP bloat by grouping actions into explicit product, automation, and
  operator/debug bundles.
- Make it possible to replace the removed builder agent with session-visible
  product actions rather than a separate agent-specific toolset.

## Non-Goals

- Do not resurrect the builder agent.
- Do not make sessions depend on the current FastMCP server for SRW-internal
  product workflows.
- Do not expose operator/debug/admin MCP tools to sessions by default.
- Do not rewrite all orchestrator routes or persistence services as part of the
  first increment.
- Do not solve external MCP tool consumption in this layer. That is a related
  but separate bridge with its own allowlist, secret handling, and approval
  model.

## Current State

### MCP

The MCP server authenticates an `srw_*` token or OAuth-created token, verifies it
through internal orchestrator endpoints, and forwards request context to REST
routes with headers such as `X-MCP-User-Id`, `X-MCP-Scope`, and
`X-Internal-Key`.

This gives external MCP callers access through normal orchestrator visibility
checks plus MCP scope restrictions. It is useful for integrations, but the tool
surface mixes user-facing capabilities with diagnostics and operational helpers.
Many tools return human-formatted summaries rather than structured results.

### Persistent Sessions

Persistent sessions receive a short-lived session token from the orchestrator
and then run in the agent runtime. Session setup resolves the thread owner into
`ToolContext.user_id`, and native orchestrator tools call REST with
`X-Internal-Key` and `X-MCP-User-Id`.

Session-created worker jobs can inherit ownership and project context from the
thread, but the session tool implementation is separate from MCP and currently
has known gaps:

- Job IDs are truncated in list output, which breaks follow-up actions that need
  the full UUID.
- Job summaries are thin for interactive supervision workflows.
- Automations and builder-replacement workflows are not yet exposed.
- Job reads are based on the user-visible set and are not clearly constrained to
  the session's current project scope.

## Proposed Architecture

### Action Core

Add a small action registry that is independent of FastMCP and LangChain.

Each action should define:

- `name` and `namespace`
- `description`
- `audiences`, for example `session`, `mcp`, `ops`, or `admin`
- `input_model` and `output_model` using Pydantic models
- `mutability`, for example `read`, `write`, `destructive`, `budget`, or
  `recurring`
- `required_grants` and optional minimum project role
- `scope_policy`, describing owner, project, admin, or MCP scope behavior
- `confirmation_policy` for actions that need user approval
- `handler(context, input)` returning structured output

The action core should not import FastMCP, LangChain, or UI code.

### Action Context

All adapters should build an `ActionContext` with the same shape:

- `user_id`
- `auth_method`
- `thread_id`
- `project_ids`
- `mcp_scopes`
- `permission_mode`
- `capability_grants`
- `source`, for example `session`, `mcp`, or `internal`
- `request_id` or audit correlation ID

The context should be used for both action selection and execution checks. Tool
registration-time filtering improves UX, but execution-time checks remain
mandatory.

### Adapters

Session adapter:

- Converts eligible actions into LangChain structured tools.
- Uses the persistent session `ToolContext` to build `ActionContext`.
- Applies session permission mode, thread/project context, and capability
  grants.
- Excludes operator/debug actions by default.

MCP adapter:

- Converts eligible actions into FastMCP tools.
- Builds `ActionContext` from the verified MCP token, user ID, and MCP scope.
- Keeps product tools separate from operator/debug bundles.
- Returns structured JSON where possible, with optional concise human summaries.

REST and service layer:

- Remains authoritative for data writes, final job status, and durable state.
- Can continue to be called through REST in the first phase.
- Can later expose shared service functions where that removes real duplication.

## Tool Bundles

The registry should support explicit bundles so each surface gets the right
amount of power.

### `jobs.core`

- List and search visible jobs.
- Get job summary, status, progress, artifacts, logs, and audit events.
- Create worker jobs from a session or MCP caller with correct ownership.
- Approve, resume, pause, or cancel jobs when permitted.

### `sessions.core`

- List and inspect sessions visible to the caller.
- Open or resume a session connection where permitted.
- Read recent session activity and linked jobs.

### `projects.core`

- List projects visible to the caller.
- Read current project details, members, repositories, and data source metadata.
- Gate write actions behind explicit project grants.

### `repositories.core`

- List repositories attached to the current or selected project.
- Read repository metadata with credentials redacted.
- Resolve the project's default or jobs repository.
- Test whether the repository is reachable from an SRW workspace backend.
- Check out a project repository into the running session workspace when the
  session has a sandbox or VM backend.
- Inspect job repositories through existing tree, file, commit, diff, and tag
  views.

Session repository checkout is the priority surface for this bundle. The
initial session tool should support the common review workflow:

1. Resolve the current project from the thread context.
2. List project repositories and select the jobs, source, or reference repo.
3. Clone the selected repo into the session workspace, usually at workspace root
   for the jobs repo or under `repos/<name>` for auxiliary repos.
4. Return the local checkout path, branch, read/write mode, and suggested next
   actions such as install, test, or start commands when known.

MCP product tools may expose repository list/read/test actions, but raw clone
URLs remain optional and must be redacted or replaced with a short-lived,
repo-scoped clone capability if they are ever exposed to external clients.

### `repositories.manage`

- Add, update, or remove project repositories.
- Create managed repositories where supported.
- Require project owner/editor policy, confirmation for destructive changes,
  and secret redaction in all returned values.

### `automations.core`

- List and inspect automations.
- Propose an automation from session context.
- Create automations disabled by default unless the caller has an explicit grant.
- Pause, resume, update, or run automations with audit logging.

### `project_loops.core`

- Get the active or most recent project self-improvement loop.
- List jobs spawned by the loop.
- Explain loop state, current stage, remaining budget, last error, and useful
  next actions.

Sessions should receive loop read/status/list-jobs actions by default for the
current project. Loop start/pause/resume/stop actions should not be session
tools: they can spawn or extend expensive recurring work, and they do not save
enough user time to justify agent control. Keep loop lifecycle control in
Cockpit or another explicit user-driven surface. Session loop tools should help
the agent inspect an ongoing loop, check out the loop's project repository, run
the application locally, and help the user decide what feedback or follow-up
work to send back into the loop.

### `knowledge.core`

- Search project knowledge.
- Read and write project-scoped notes, instructions, and source metadata when
  permitted.

### `ops.debug`

- Agent diagnostics, raw stats, audit queries, logs, and operational helpers.
- Admin or operator MCP only.
- Not exposed to ordinary persistent sessions by default.

## Security Model

- Orchestrator access checks remain the final authority.
- Action metadata determines whether an action is visible to a caller.
- Execution always re-checks grants, project roles, MCP scope, and mutability
  policy.
- Mutating, budget-affecting, recurring, and destructive actions can require
  explicit confirmation depending on session mode and caller grants.
- Automation creation defaults to proposal or disabled state unless the caller
  has a grant that allows active recurring work.
- MCP project scope must intersect with normal identity visibility; MCP scope
  should not broaden access.
- Product session actions should default to the session thread's project scope
  unless the action is explicitly marked cross-project.
- Every action execution should emit audit metadata: source, user, thread,
  project, action name, target resource, and result.

## Suggested Roadmap

### Phase 0: Session Job Tool Cleanup

- Stop truncating job IDs in session job list output.
- Add richer job summaries that include status, owner/project context, created
  time, updated time, and next useful actions.
- Add a short-ID resolver only as a convenience, not as the primary contract.
- Document whether session job reads are project-scoped or user-wide, then make
  the implementation match that decision.

### Phase 1: Jobs Action Slice

- Add the initial action core and registry around job operations.
- Port the current session job tools to the session action adapter.
- Port MCP job wrappers to call the same job actions.
- Add focused tests for user-owned, project-visible, admin, and MCP-scoped job
  access.
- Ensure job creation attributes ownership correctly for both sessions and MCP.

### Phase 2: MCP Surface Rework

- Split MCP tools into product and operator/debug bundles.
- Keep backward-compatible aliases where needed, but route through the action
  registry.
- Prefer structured outputs and move human formatting to adapter-level helpers.
- Remove duplicated job/session logic after parity tests pass.

### Phase 3: Project Repository Checkout and Loop Inspection

- Add `projects.get_current` and project repository read actions for sessions.
- Add `repositories.checkout_session_repo` for persistent sessions with sandbox
  or VM workspaces.
- Return structured checkout metadata: project ID, repository ID/name, role,
  local path, branch, backend, read/write mode, and any warning.
- Add `project_loops.get`, `project_loops.list_jobs`, and
  `project_loops.explain_state` for sessions.
- Keep loop start/pause/resume/stop out of the session tool surface because
  these actions spawn or control ongoing budget-consuming work.
- Keep MCP repository clone support out of the first slice. MCP may expose
  project repository metadata and job repository inspection, but external clone
  capabilities should wait for a deliberate token/URL/grant design.

### Phase 4: Builder-Replacement Actions

- Add session-visible product actions for job drafting and refinement.
- Support creating and editing job instructions, descriptions, expert config,
  linked context, and review state.
- Make these actions usable from the session/canvas workflow instead of a
  separate builder agent.

### Phase 5: Automations

- Add automation read actions first.
- Add propose/create/update actions with grant checks and audit logging.
- Keep new automations disabled by default unless the caller has an explicit
  activation grant.
- Add pause, resume, and run-now actions after approval semantics are clear.

### Phase 6: Projects, Knowledge, and Sources

- Add project read actions needed by sessions and MCP.
- Add knowledge and source metadata actions for project-scoped context work.
- Gate write actions by project role and explicit capability grants.

### Phase 7: External MCP Consumption

- Add a separate session bridge for consuming third-party MCP servers.
- Use explicit allowlists, secret scoping, tool approval, and per-session
  visibility.
- Do not couple this bridge to the internal SRW MCP adapter.

### Phase 8: Deprecate Duplicates

- Remove old duplicated session and MCP implementations after parity is covered.
- Keep compatibility wrappers only where external clients depend on existing MCP
  tool names.
- Update docs and tests to treat the action registry as the source of truth.

## Acceptance Criteria

- At least one complete capability slice, starting with jobs, is implemented once
  and exposed through both sessions and MCP.
- Session job list output includes full stable identifiers.
- MCP and session job creation both assign the correct user and project context.
- Product actions return structured outputs that adapters can render.
- Persistent sessions can list project repositories and check out a permitted
  project repository into the active session workspace.
- Persistent sessions can inspect the current project's self-improvement loop
  and list jobs spawned by that loop.
- Operator/debug actions are not visible to ordinary sessions.
- MCP product and operator/debug surfaces are separable.
- Tests cover owner, project, admin, session, and MCP-scope access paths.

## Open Decisions

- Should ordinary session product actions always be constrained to the thread's
  project scope, or should some actions default to all user-visible resources?
  Recommended default: project-scoped, with explicit cross-project actions.
- Should MCP bundles be separate servers, separate tool prefixes, dynamic tool
  filtering, or a combination?
- Should MCP ever expose external clone capability for project repositories, and
  if so should that be a short-lived token, temporary collaborator grant, or a
  server-managed workspace checkout?
- Should action handlers call orchestrator REST long term, or should shared
  service functions become the internal boundary for selected capabilities?
- What structured MCP output shape should be standard for human-facing clients:
  raw JSON only, JSON plus summary, or content blocks?
- What exact grant should activate recurring automations from a session?
- How should external MCP tool imports appear in the session UX and approval
  model?
