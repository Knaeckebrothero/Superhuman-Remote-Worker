---
tags:
  - skills
  - onboarding
  - product
  - documentation
  - capabilities
  - self-knowledge
---

# SRW Self-Knowledge and App Guide

> **Status**: v1 `app-guide` shipped 2026-07-08. M1a managed delivery, M1b
> Email/OKF guidance plus the connector-type drift gate, M1c
> Automations/Fleet guidance plus live tool-group coverage, M1d
> Canvas/browser plus permission/workspace guidance, and M1e project
> loops/campaigns plus Protected Cloud guidance were implemented through
> 2026-07-24. M1d also repaired the automation expert-selection contract that
> the guide audit exposed. The M1f closure plan was defined 2026-07-25 and is
> now in progress. Its core-reference implementation completed 2026-07-25;
> break-glass health, M1 evaluation/deployment gates, the live capability plane,
> visual help, and the later roadmap remain open.
>
> Companion to [[default_skill_roster]] (the bundled-skill roster),
> [[agent_skills]] (the skills runtime), [[default_expert_roster]] (the shipped
> roles), and the bundled `config/experts/product-qa/` role where applicable.

## Executive decision

Make SRW able to explain itself through a managed, progressively disclosed
`app-guide` system skill backed by three distinct sources of truth:

1. **Immutable, versioned user guidance shipped with the running product**
   explains concepts and workflows. Authoritative guide bytes must be readable
   without a mutable workspace and refreshed when an existing session resumes
   after an upgrade.
2. **A runtime product-capability service** reports what that build supports,
   what the deployment has enabled, what the current user may use, and what the
   current session appears able to access at evaluation time.
3. **Component provenance** identifies the actual orchestrator, agent, Cockpit,
   and guide revisions involved. SRW must represent mixed-version deployments
   instead of pretending they have one global build identity.

Capability results are advisory snapshots for explanation, planning, and UI.
They are never authorization tokens. Every operation still re-authorizes the
caller and rechecks flags, resource scope, service state, and session
preconditions at execution time.

The public source repository is an optional, revision-pinned fallback for
implementation questions and gaps in the bundled guide. It is not the primary
authority for user-facing capability claims: the default branch can be ahead of
the installed version, a deployment can run a fork, and `docs/features/`
contains proposed as well as shipped behavior.

All user-facing persistent experts should be able to answer product questions;
autonomous workers do not receive the guide by default. The normal interactive
Assistant is the primary consumer. Users should not have to switch to a
dedicated support expert to ask how SRW works. A dedicated Product Guide expert
remains useful for a public documentation bot or evaluation, but it is not the
main product interaction.

## Problem

SRW's most useful capabilities live outside the immediate context of a session
model. A session can see its current tools, but that alone does not explain:

- sessions versus autonomous worker jobs;
- which experts exist and when to use each one;
- projects, loops/campaigns, and automations;
- how datasources are created, scoped, and attached;
- which cloud, knowledge, browser, Canvas, and workspace features exist;
- which features are deployment-dependent or grant-gated; or
- how to enable a capability that is not ready in the current session.

This causes two product failures:

1. Users cannot discover work SRW could do for them.
2. Agents guess from model priors and teach users UI labels or features that do
   not exist in the installed version.

Traditional tutorials do not solve this well for a fast-moving, solo-developed
product. They are expensive to keep current, are rarely read at the moment of
need, and duplicate the conversational interface SRW already has. The goal is
not to eliminate documentation; it is to make small, versioned pieces of it
available contextually through the agent and validate as much of it as code can
validate.

## Goals

- Answer "what is SRW?", "can you do X?", "how do I do X?", and "why can't I
  do X here?" accurately.
- Distinguish product support from deployment, user, and session availability.
- Keep product guidance version-matched to the relevant running components and
  expose uncertainty when they differ.
- Load only the relevant guide topic instead of injecting a full manual into
  every turn.
- Give the shortest path to the user's goal, then offer an action the agent can
  actually perform.
- Provide safe visual help through deep links, generated screenshots, and
  eventually coach marks on the real Cockpit UI.
- Detect documentation drift mechanically where possible and use AI to draft
  reviewable fixes where deterministic checks are insufficient.
- Degrade honestly when the guide, runtime capability service, or public source
  fallback cannot answer.

## Non-goals

- Treating the entire repository as user documentation.
- Generating all user guidance automatically from source code.
- Claiming that a feature is usable merely because its implementation exists.
- Replacing expert identity, tool descriptions, or project onboarding with one
  large product prompt.
- Building a full video-tutorial platform in the first release.
- Letting an agent navigate or operate the user's Cockpit without an explicit
  user action.
- Exposing secret values, hidden administrator configuration, or capabilities
  the caller is not allowed to inspect.

## Research basis and derived constraints

This design combines repository evidence with primary guidance from adjacent
standards and mature product-help systems. The sources are precedents, not
drop-in architecture; SRW's own trust boundaries and deployment model remain
decisive.

| Area | Primary guidance | Constraint adopted here |
|---|---|---|
| Skill shape and evaluation | [Agent Skills specification](https://agentskills.io/specification), [creation best practices](https://agentskills.io/skill-creation/best-practices), and [evaluation guidance](https://agentskills.io/skill-creation/evaluating-skills) | Keep metadata/body bounded, link focused references one level deep, test positive and near-miss triggers in fresh contexts, and compare against no-skill or the previous version. |
| Capability snapshots | [Kubernetes SelfSubjectRulesReview](https://kubernetes.io/docs/reference/kubernetes-api/definitions/self-subject-rules-review-v1-authorization/) and [SelfSubjectAccessReview](https://kubernetes.io/docs/reference/kubernetes-api/definitions/self-subject-access-review-v1-authorization/) | A broad self-capability result may be incomplete and is useful for explanation/UI, but each real operation must authorize again. Preserve `unknown` and evaluation errors. |
| Scoped evaluation | [OpenFeature evaluation context](https://openfeature.dev/specification/sections/evaluation-context/) and [evaluation types](https://openfeature.dev/specification/types/) | Resolve authenticated deployment, project/user, and invocation context with explicit precedence; distinguish disabled, stale, not-ready, and error states. |
| Protocol contracts | [MCP lifecycle and capability negotiation](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) and [MCP tool results](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) | Version the schema, publish a registry revision and observation time, bound results, validate structured output, and provide a concise text fallback. |
| Provenance | [OCI image annotations](https://specs.opencontainers.org/image-spec/annotations/), [SLSA build provenance](https://slsa.dev/spec/v1.2/build-provenance), and [GitHub permanent links](https://docs.github.com/en/repositories/working-with-files/using-files/getting-permanent-links-to-files) | Stamp source/revision/version per component, distinguish declared from verified provenance, and use full immutable commit IDs rather than moving branches or display SHAs. |
| Source safety | [OWASP prompt-injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) and [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) | Treat repository content as delimited untrusted data and use a read-only, allowlisted, size-bounded, SSRF-resistant retriever. Sanitization alone is not a security boundary. |
| Help UX and accessibility | [Fluent onboarding](https://fluent2.microsoft.design/onboarding/), [WCAG Consistent Help](https://www.w3.org/WAI/WCAG22/Understanding/consistent-help), [ARIA dialog guidance](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/), and [WCAG Focus Not Obscured](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum) | Help is contextual, optional, dismissible, reopenable, consistently discoverable, keyboard operable, and never allowed to obscure or trap the task it explains. |
| Screenshots and selectors | [GitHub screenshot guidance](https://docs.github.com/en/contributing/writing-for-github-docs/creating-screenshots), [Playwright visual comparisons](https://playwright.dev/docs/test-snapshots), and [Playwright locators](https://playwright.dev/docs/locators) | Use screenshots selectively, retain complete text steps, generate in a pinned environment, and validate accessible roles/names in addition to stable help anchors. |
| Content and eval design | [Diátaxis](https://diataxis.fr/), [LangSmith trajectory evaluation](https://docs.langchain.com/langsmith/trajectory-evals), and [ARES](https://aclanthology.org/2024.naacl-long.20/) | Separate explanation, how-to, reference, and troubleshooting content; evaluate routing, retrieval relevance, faithfulness, response quality, and tool trajectory separately. |
| Rich interactive help | [MCP Apps overview](https://modelcontextprotocol.io/extensions/apps/overview) and [SEP-1865](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp) | Evaluate the standard Apps path before locking SRW into a bespoke interactive help-card protocol; retain native Cockpit and text fallbacks. |

## User stories

The system must support at least these question shapes:

- "What can I do here?"
- "What is the difference between a session, a job, and a project?"
- "Can you run this in the background?"
- "Can you work on several jobs at once?"
- "How do automations and project loops work?"
- "Which expert should I use for this?"
- "Can you read my email?"
- "How can I share only selected email messages with you?"
- "Can you query my production database without changing it?"
- "Why is Canvas/browser sharing unavailable in this session?"
- "Can you do this yourself, or can you only show me how?"
- "Is this feature part of SRW or only planned?"

## The truth model

"Can you do X?" is not one boolean question. The answer is a join across four
layers:

| Layer | Question | Authority | Example |
|---|---|---|---|
| Build | Does this installed SRW release implement it? | Versioned capability registry and bundled guide | This build supports email datasources |
| Deployment | Is it enabled and configured here? | Runtime flags, edition, provider configuration, and service health | Email is enabled; voice is not configured |
| User | May this caller use it? | Effective capability grants and ownership rules | The user may create read-tier email sources but may not enable unattended send |
| Session | Is it ready in this conversation/job? | Actual tools, workspace backend, attached datasources, and current scope | Email exists but no mailbox is attached to this session |

A fifth projection describes **agent actionability**:

- `can_execute` — the current agent has a safe tool for the requested action;
- `can_propose` — it can prepare a dry run or approval-gated change;
- `can_guide` — it can explain the Cockpit workflow but cannot perform it; or
- `unavailable` — neither the product nor the current context can do it.

The agent must not collapse these statements. In particular:

- "SRW supports X" does not mean "X is enabled here".
- "Your deployment enables X" does not mean "you are allowed to use X".
- "You may use X" does not mean "this session currently has X attached".
- "I can explain X" does not mean "I can perform X for you".

Resolution is monotonic and fail-closed for claims:

1. The component/build registry establishes the upper bound. A later layer
   cannot enable a capability absent from the relevant running component.
2. Deployment configuration and health may narrow support.
3. The authenticated request, active project, and user grant policy may narrow
   it further. Model-supplied filters never override authenticated identity or
   project scope.
4. The live session overlay observes attached resources, actual loaded tools,
   backend features, and current readiness.
5. `agent_action` is derived from those observations but means "appears
   actionable now", not "pre-authorized".

If any required resolver is unavailable, stale, or incomplete, the affected
layer is `unknown`; it is never silently converted to `disabled` or `denied`.
The snapshot is authoritative only for explaining what SRW observed at its
`evaluated_at` time. Execution-time policy and validation always win.

## Current state

### Shipped v1

The bundled `config/skills/app-guide/` already provides the correct content
shape:

- `SKILL.md` supplies the Layer-1 trigger, the Layer-2 procedure, a topic index,
  and anti-hallucination rules.
- `references/` supplies short Layer-3 user guides for overview, sessions,
  jobs, experts, projects/loops, datasources, memory/knowledge, and
  files/integrations.
- The skill is model-invoked and unbound, so unrelated work pays only the
  catalog-entry cost.
- `tests/test_bundled_skills.py` verifies parsing, budgets, grounding language,
  and agreement between the `SKILL.md` index and reference files.

### Shipped M1a — managed delivery foundation

The first M1 slice makes the guide a persistent-session product floor:

- every persistent session injects the current `app-guide` catalog entry and
  workspace-independent `read_product_guide(topic_id)` tool after final backend
  filtering, including the `none` tier and configurations with DB skills or DB
  experts disabled;
- ordinary worker catalogs exclude the reserved managed guide by default;
- the runtime removes frozen/owner/project/global `app-guide` replacements,
  reloads the running bundle, stamps a deterministic content digest, and scopes
  the menu entry to successful reader-tool instantiation;
- the reader accepts only `index` or a bounded logical topic ID, verifies the
  digest, and returns the procedure plus at most one focused reference;
- `app-guide` is never materialized as an ordinary workspace skill, while
  `use_skill` refuses to read a same-name workspace copy;
- create/import/update validation reserves the `app-guide` name while
  duplication remains possible under a distinct generated name; and
- focused resolution, tool, persistent-session, backend-tier, CRUD, and bundle
  tests cover current delivery, stale-byte rejection, non-shadowing, digest
  refresh, invalid paths, and fail-closed behavior.

This is not the M1 exit gate. The broader reference-content audit,
break-glass/degraded-health surface, compaction evaluation, held-out
trigger/answer evaluations, and fresh/resumed k3d matrix remain open below.

### Shipped M1b — connector content and first drift gate

The next M1 slice repairs the two highest-value connector journeys and gives
them a deterministic inventory boundary:

- `datasources-email` now covers provider/app-password setup, the four access
  tiers, selective folder sharing, recipient limits, attachment, and the
  current fail-closed send behavior;
- `datasources-okf` now covers the Git/root/auth flow, central indexing and
  readiness, incremental versus full rebuilds, attachment, lite-tier support,
  and external read-only behavior;
- Cockpit's English and German Email hints now describe the runtime's
  fail-closed direct-send behavior instead of promising the not-yet-built
  human-approval queue;
- both references carry content-type, capability-ID, and journey-ID metadata
  while remaining one level below the compact routing skill;
- `src/core/datasource_catalog.py` is the canonical build-level inventory for
  the 12 internal datasource types (called Connectors in the product);
- backend creation validation and its API description consume that inventory;
  and
- `tests/test_datasource_catalog.py` asserts parity with Cockpit's type union,
  authoring selector and filters, the agent credential/tool groupings, and a
  routable guide-coverage decision for every type.

This is intentionally a build inventory, not the Phase 2 capability plane. It
does not claim that a connector is enabled, granted, attached, healthy, or
usable in the current session.

### Shipped M1c — Automations and fleet actionability

This slice repairs the two highest-value work-dispatch journeys and makes their
agent actionability explicit:

- `automations` now explains the shipped schedule-only job template, presets
  and cron/timezone preview, project scoping, Run-now semantics, pause/resume,
  retained past jobs, max-fires and catchup controls, at-least-once delivery,
  and the current absence of per-automation connector selection;
- the guide distinguishes the Cockpit lifecycle from the default
  **Automations & Loops** session group, which can inspect automations/runs and
  prepare a proposal but cannot save, enable, run, pause, edit, or delete one;
- `fleet-and-delegation` distinguishes independent worker jobs created through
  **Fleet Management** from optional subagents created inside one parent job;
- the Fleet path covers tool enablement, manual fallback, connector inheritance,
  job/project/repository inspection, approval, feedback/resume, safe-point
  pause, cancellation, parallel-capacity limits, and durable monitoring in
  Jobs/Inbox;
- the overview, sessions, and jobs references no longer promise that every
  session can create jobs or continuously watch them without the actual Fleet
  tools;
- both focused references carry content-type, capability-ID, and journey-ID
  metadata and are routed one hop below `SKILL.md`; and
- `tests/test_app_guide_content.py` pins their critical safety/actionability
  claims and requires every tool in the selectable live Fleet Management and
  Automations & Loops groups to have an explicit guide-topic decision.

Like M1b, these are static build-level claims. The guide tells the model to
check its actual visible tools, but the Phase 2 capability plane is still
needed to explain deployment, user, and current-session state reliably.

### Shipped M1d — Canvas/browser and permission/workspace truthfulness

This slice repairs the two most failure-prone “can I do this here?” journeys
and closes the automation prerequisite discovered during the audit:

- `canvas-and-browser` distinguishes Canvas as a shared stage, direct browser
  tools, web research, and the default-off shared-browser view instead of
  treating them as one generic browser feature;
- it documents the current file renderer allowlist, editable-source refresh
  contract, close-versus-clear lifecycle, strict-versus-schema-advertised
  interactive HTML boundary, deployment-gated Live Preview, shared-browser
  baton/cookie handoff, native dialog gaps, persistent-session-only host
  surface, reason codes, and the currently proven shared-browser
  Container—not VM—path;
- `permissions-and-availability` documents the runtime's shipped permission
  modes rather than the proposed tier design, the restrict-only grant
  hierarchy and full current grant catalog, all four workspace tiers, live
  settings limitations, upgrade-only workspace changes, and a layered
  diagnosis path that refuses to infer current state from static prose;
- the sessions, overview, and file/integration references no longer promise
  git, shell, browser, IDE, or workspace files on tiers that do not provide
  them;
- the automation editor now filters to worker experts and persists
  database-backed selections with `expert_id` while keeping bundled configs in
  `expert`; fire-time resolution preserves pinned IDs, resolves an unpinned
  `worker_base` default intentionally, and fails loud instead of silently
  changing an explicit selection;
- migration `0069_automation_expert_id.sql` backfills the old UUID-in-name
  editor bug and adds the foreign-key/check/index contract, while create,
  update, delete-blocker, portable-bundle, Cockpit, and fire-time paths share
  the new representation; and
- focused tests pin the two guide metadata contracts, Canvas/direct-browser
  tool and renderer inventories, every shared-browser reason code, every
  current capability-grant key, current approval and workspace boundaries,
  independent managed-reader retrieval, and the automation selection
  regression.

These remain static, reviewed build-level claims. They can interpret an
observed control reason or visible tool, but the guide cannot yet query the
complete effective deployment/user/session state; that remains Phase 2.

### Shipped M1e — unified loops/campaigns and Protected Cloud

This slice repairs the two remaining post-v1 feature journeys whose shipped
behavior had outgrown the original broad references:

- `project-loops` separates continuous work from general project organization
  and documents the unified stage/barrier engine's Standard and Campaign
  modes, preset/custom cycles, analysis-only fan-out, campaign plan and
  disposition flow, budget/failure guardrails, monitoring, and graceful
  pause/resume/stop semantics;
- it corrects two especially consequential false impressions: Definition of
  Done is a steering quality bar rather than an automatic stop condition, and
  `max_iterations` is charged per completed stage/turn, so a parallel fan-out
  can create more jobs than the iteration budget;
- the Loop Cockpit copy now reports jobs started separately from the remaining
  iteration budget and states those two semantics in the start form;
- `protected-cloud` distinguishes an ordinary live mount, read-only project
  access, project-job diff review, and the protected persistent-session
  overlay; it covers the deployment flag, non-default Nextcloud and Container
  requirements, creation-time immutability, first-eligible-mount rule,
  turn-end staging, whole-diff text/binary review, owner-only apply/reject,
  fail-closed engage, epoch/external-change gates, partial writes, quota, and
  the accepted dead-pod resume edge case;
- the same audit closes a mixed-project selector gap by excluding
  `project_default` user-home rows in the shared server-side protected-mount
  selector, rather than relying only on the Cockpit checkbox gate;
- projects, sessions, overview, files/cloud, and workspace references now route
  to those focused topics without duplicating their detailed workflows;
- both references carry content-type, capability-ID, and journey-ID metadata
  and remain one hop below the compact `SKILL.md` router; and
- focused tests pin loop actionability and safety claims against the current
  analysis-role and campaign-default constants, pin Protected Cloud's
  eligibility/review/fail-closed claims and first-Nextcloud-mount selector,
  preserve workflow-tool coverage, and prove independent managed-reader
  retrieval.

These are still static build-level instructions. The Protected Cloud guide can
interpret an observed checkbox, badge, error, or workspace, but it cannot query
the feature flag, engage health, staging freshness, or current user's effective
state itself; that remains Phase 2.

### Implemented M1f work package 1 — core-reference closure

The final original-reference audit is implemented:

- `jobs` now matches the current create form, worker/session eligibility,
  autonomy boundaries, internal review/wait/pause states, all four workspace
  tiers, optional Critic/Scholar behavior, conditional Workspace/IDE/diff/cloud
  actions, and Fleet's actual tool dependency. It no longer promises git,
  files, automatic pause recovery, or critic review for every job.
- `experts` now covers the complete bundled roster including General Worker
  and Product QA Tester, worker versus session type compatibility, the
  explicit → project → personal → application default chain, managed seed
  defaults, and current create/duplicate/import/export/edit/delete constraints.
- `memory-and-knowledge` now separates live conversation context and
  compaction, optional automatic RecallStore memory, writable native project
  knowledge, and external read-only OKF knowledge. It documents scope and
  degraded/required behavior without promising that a particular fact will be
  extracted or recalled.
- The overview, sessions, projects, and files references no longer imply a
  fixed Assistant default, supervised platform default, default Critic,
  universal project-memory inheritance, one git repository per job, or a
  filesystem on every workspace tier.
- Every touched reference now declares content type, capability IDs, and
  journey IDs, while remaining one hop below the compact router.
- Focused contracts enumerate the bundled expert roster from current YAML,
  pin consequential Jobs/Experts/Memory boundaries, and prove that the managed
  reader loads those topics independently.

The App Guide-specific tests pass. The repository's full combined content
command currently also observes an unrelated concurrent Canvas/Office coverage
failure (`CanvasRenderer` includes `office` while the Canvas guide-coverage
test has not yet classified it); that external drift remains visible and is
not counted as a pass for the M1 exit gate.

### Remaining gaps after M1f work package 1

The guide is now delivered reliably, but it is not yet self-maintaining or
runtime-aware:

- The connector overview now routes Email and OKF to focused how-to topics and
  covers MCP, credential-file, repository, WebDAV, and managed database
  connectors. The new catalog catches type-list drift, but it does not verify
  every workflow sentence against implementation.
- The reference-index, connector-catalog, grant-catalog, and selected
  tool/loop/protected-cloud contract tests prove structure, coverage decisions,
  selected consumer parity, and critical safety boundaries—not end-to-end
  factual freshness.
- Static references can describe possible features but cannot inspect flags,
  user grants, service configuration, or the session's attached resources.
- `get_session_context`, `list_experts`, `list_skills`, workflow tools, and
  `/api/users/me/capabilities` expose useful fragments but there is no bounded,
  product-facing effective-capability view. The current grants endpoint also
  scopes across all visible projects rather than one owned active thread.
- `get_session_context` observes useful live agent state, but persistent
  sessions do not currently record the exact resolved tool-name set in
  `ToolContext`; the orchestrator alone cannot infer all post-upgrade runtime
  state.
- The current `BUILD_SHA` is a short, production-agent-only value, absent in
  local development by design. It is not an orchestrator, Cockpit, guide, or
  release identity, and SRW supports independently tagged component images.
- Cockpit still repeats connector IDs in TypeScript/UI source, but the canonical
  Python inventory now drives backend acceptance and CI asserts parity.
  Canvas/direct-browser and grant coverage consume current Python
  inventories, but other user-visible inventories and Cockpit route/actions
  still lack canonical seams.
- The proposed `/datasources?new=email` target is not implemented today. The
  current route is `/datasources`, and creation is opened by local component
  state rather than a query parameter.
- Skill files and import/export are UTF-8 text-only. PNG, WebP, and video assets
  cannot yet travel as portable skill files; the bundled scanner also skips
  non-UTF-8 files.
- There is no structured Cockpit help-card or coach-mark protocol.
- The current app-guide tests cover managed delivery and structure, but not
  model trigger behavior, grounded answer quality, factual coverage, or the
  complete fresh/resumed deployment matrix.

### Planned M1f — close the reliable text-guide milestone

M1f is the closure slice for Phase 1, not an expansion into the runtime
capability plane. Its executable plan is
`docs/superpowers/plans/2026-07-25-app-guide-m1f.md`.

The work is deliberately ordered:

1. finish the original Jobs, Experts, and Memory/Knowledge reference audit
   (implemented 2026-07-25);
2. add the operator-only `APP_GUIDE_BREAK_GLASS_DISABLED` escape hatch and a
   bounded persistent-agent health signal;
3. add a held-out routing/answer corpus plus compaction and honest-gap
   evaluation;
4. run the fresh/resumed `none`, `virtual`, and Container k3d matrix with DB
   skills and experts disabled; and
5. record evidence and close the Phase 1 boxes only after the chart-default
   and pre-upgrade-resume exit gate passes.

M1f keeps detailed cases outside the runtime skill, does not infer Phase 2
availability state, and treats a skipped live-model or k3d check as incomplete
rather than successful.

### Validated repository implementation seams

These are the leading implementation seams as of 2026-07-25. They record why
the roadmap is sequenced this way; symbols may move during implementation.

| Concern | Existing seam/evidence | Status / intended change |
|---|---|---|
| Managed system-skill floor | `src/core/skill_resolution.py` — `add_persistent_system_skills()` plus reserved-name filtering | M1a now replaces any same-name catalog payload with the running digest-stamped guide and keeps it out of worker catalogs |
| Persistent delivery/freshness | `src/api/persistent_session.py` — post-load skill scoping | M1a now refreshes the managed bundle on every tool setup/rebind and does not materialize it into the workspace |
| Guide retrieval | `src/tools/product_help.py` and `src/tools/registry.py` | M1a adds bounded `read_product_guide(topic_id)` independent of workspace tier while preserving ordinary workspace skills |
| Capability service | Existing grants route in `orchestrator/main.py`; `orchestrator/services/grants_service.py` | Put product definitions/evaluation in a dedicated service and small router rather than enlarging the grants endpoint |
| Thread ownership/scope | `orchestrator/routers/sessions.py` fetch-then-owner checks | Reuse the owned-thread pattern and resolve only the active thread/project scope |
| Live session overlay | `src/tools/context.py`, `src/api/persistent_session.py`, and `src/tools/orchestrator/jobs.py` | Record final loaded tool names and overlay actual backend/datasource/knowledge/cloud observations in the agent tool |
| Datasource inventory | `src/core/datasource_catalog.py`, validation in `orchestrator/main.py`, agent mapping in `src/core/datasource_setup.py`, and Cockpit types/filters | M1b makes the Python catalog authoritative for backend acceptance and asserts guide/Cockpit/agent parity; runtime availability remains Phase 2 |
| Session work control groups | `src/core/session_tool_overrides.py`, `src/tools/orchestrator/jobs.py`, and `src/tools/orchestrator/workflows.py` | M1c gives every currently selectable Fleet/Workflow tool a focused guide-topic decision; route/action and runtime-state registries remain later work |
| Canvas/browser inventories | `src/tools/canvas`, `src/tools/research/browser_direct.py`, `src/core/session_tool_overrides.py`, `orchestrator/services/canvas.py`, and `orchestrator/services/shared_browser_canvas.py` | M1d gives every current Canvas/direct-browser tool, file renderer, and shared-browser reason code a focused-topic decision; feature/runtime state remains Phase 2 |
| Permissions/workspaces | `src/core/capability_grants.py`, `src/api/persistent_app.py`, session workspace constants in `orchestrator/main.py`, and Cockpit live settings | M1d covers every current grant key and shipped approval/workspace behavior; a complete effective-state query remains Phase 2 |
| Automation expert selection | `orchestrator/services/automations.py`, `orchestrator/routers/automations.py`, migration `0069`, and the Cockpit editor | M1d adds explicit DB `expert_id`, worker-only validation, pinned/default fire semantics, delete blockers, and backend/Cockpit regressions |
| Project loops/campaigns | `orchestrator/routers/project_loops.py`, `orchestrator/services/project_loops.py`, the unified advance path in `orchestrator/main.py`, and `cockpit/.../project-loop.component.ts` | M1e documents Standard versus Campaign scheduling, stage-barrier budgeting, campaign guardrails, controls, and the inspection-only live workflow tools; complete runtime state remains Phase 2 |
| Protected Cloud review | `orchestrator/services/cloud_staging/`, `orchestrator/services/diff_source.py`, protected mount/endpoint wiring in `orchestrator/main.py`, and the session-create/chat/diff-review Cockpit surfaces | M1e documents the shipped flag/backend/workspace gates, staging and whole-diff decision contract, fail-closed posture, conflicts, and recovery boundaries; live evaluation remains Phase 2 |
| Core Jobs/Experts/Memory references | Current Cockpit create/list/project surfaces, `orchestrator/services/default_experts.py`, bundled expert YAML, `src/services/recall_store.py`, the configured memory pipeline, and native/OKF knowledge paths | M1f work package 1 corrects the original references and their broad overlaps, adds metadata, and pins consequential scope/action/degradation claims plus the complete bundled roster |
| M1 break-glass/health | `src/core/skill_resolution.py`, `src/tools/product_help.py`, the persistent-agent health route in `src/api/persistent_app.py`, and the shared Helm ConfigMap | M1f adds one default-off negative operator escape hatch, withholds both guide and reader when active, and reports a bounded degraded reason without reviving mutable fallback content |
| M1 evaluation/acceptance | `eval/app_guide/`, persistent graph/session tests, and `docs/tests/app_guide_m1_verification.md` | M1f separates trigger trajectory, topic routing, grounded facts, near-miss negatives, honest gaps, compaction recovery, and the fresh/resumed live matrix |
| Deep-link actions | `cockpit/src/app/app.routes.ts` and datasource page/list components | Add an explicit action manifest and handler; do not assume `/datasources?new=email` already works |
| Help presentation | `cockpit/src/app/core/models/tool-card.model.ts` currently knows `open_canvas`; strict Canvas HTML is inert while schema-advertised interactive HTML is an isolated, untrusted artifact | Define a separately validated help-card/App contract; do not treat arbitrary interactive Canvas HTML as trusted product UI |
| Provenance | Agent-only `BUILD_SHA` in `docker/Dockerfile.agent` and short CI values; independently tagged images in Helm values | Stamp and surface full revision/digest metadata for each relevant component |
| Tests | Bundled, resolution, product-help tool, content-contract, CRUD, backend-tier, and persistent-session suites | M1a covers managed delivery/current bytes; M1b–M1e add focused content plus datasource, Fleet/Workflow, Canvas/browser, grant-catalog, loop/campaign, and Protected Cloud boundaries; add model triggers, grounded answers, compaction, and the k3d resume matrix |

## Architecture

The request path is:

```text
user product question
        |
        v
managed app-guide trigger -> immutable guide reader -> focused reference
        |                                               |
        v                                               v
product-capability query                    workflow / explanation
        |
        +-> orchestrator evaluation -> live-session overlay
        |              |                       |
        +------ component provenance ----------+
                               |
                               v
                 truthful answer + safe next action
                               |
                   optional help card / visual
                               |
             isolated pinned-source fallback if needed
```

### 1. Managed `app-guide` system skill

`app-guide` is a product artifact, not an ordinary replaceable workspace
skill. Its delivery contract is:

- inject its compact catalog entry for every user-facing persistent expert,
  independently of DB-authored skills and expert-resolution flags;
- reserve its system identity so an owner/project/global skill cannot silently
  shadow it; user extensions use a different name and remain untrusted;
- read the authoritative body and references from the running guide bundle,
  not arbitrary mutable workspace bytes;
- work on `none` and other no-filesystem workspace tiers; and
- reconcile by content digest/version when an existing session resumes after a
  product upgrade.

Phase 1 may satisfy this by extending `use_skill` with a system-bundle reader or
by adding a bounded `read_product_guide(topic_id)` tool. The mechanism is open;
the immutable, backend-independent behavior is not. Workspace materialization
may remain for portability, but it cannot be the authority.

Keep the skill small and progressively disclosed:

1. **Layer 1 — metadata:** a trigger description broad enough to catch product
   capability, usage, troubleshooting, and onboarding questions.
2. **Layer 2 — `SKILL.md`:** the answer procedure and compact topic router.
3. **Layer 3 — references:** user-facing concepts and workflows loaded only as
   needed.

Keep `SKILL.md` below 500 lines and roughly 5,000 tokens, keep reference links
one level deep, and preserve an activated guide's procedure across context
compaction. The Layer-1 description remains the only unconditional model-context
cost.

Detailed feature inventory should not be duplicated in the skill body. Dynamic
inventory comes from the capability tool; references explain stable concepts,
safe defaults, trade-offs, and step-by-step journeys.

Guide records carry enough metadata for routing and verification without
turning prose into a second registry:

```yaml
guide_id: datasources.email.connect
content_type: how_to # tutorial | how_to | reference | explanation | troubleshooting
capability_ids: [datasources.email]
journey_ids: [datasources.email.create]
```

`last_verified_revision` is generated from reviewed CI/journey evidence, not
advanced merely because an AI drafted new copy. Explanation, task guidance,
reference inventory, and troubleshooting remain separate content units so the
agent loads only the user's actual information need.

As the guide grows, keep references one level below `SKILL.md` and split broad
topics when doing so materially reduces irrelevant context. Likely additions
to the v1 reference set are:

| Reference | Purpose |
|---|---|
| `automations.md` (shipped M1c) | Scheduled jobs, enable/disable semantics, runs, and safe authoring |
| `canvas-and-browser.md` (shipped M1d) | File/app/browser presentation, shared browsing, and availability requirements |
| `datasources-email.md` (shipped M1b) | Provider support, app passwords, access tiers, folder/recipient allowlists, attachment |
| `datasources-okf.md` (shipped M1b) | OKF knowledge bases, repositories, indexing readiness, and read-only behavior |
| `fleet-and-delegation.md` (shipped M1c) | What the session can delegate, inspect, approve, resume, pause, and cancel |
| `permissions-and-availability.md` (shipped M1d) | Grants, feature flags, workspace tiers, and how to interpret disabled controls |
| `project-loops.md` (shipped M1e) | Standard/parallel and Campaign scheduling, iteration semantics, controls, and agent actionability |
| `protected-cloud.md` (shipped M1e) | Eligible protected sessions, staged review/apply/reject, safety gates, and troubleshooting |

The exact split is an implementation-time content decision. `SKILL.md` remains
the authoritative routing index; the capability registry remains the
authoritative inventory.

### 2. Product capability registry

Introduce a versioned registry of user-visible capabilities. A capability has a
stable dotted ID such as:

- `jobs.create`
- `jobs.review`
- `automations.manage`
- `projects.loops`
- `datasources.email`
- `datasources.email.send`
- `datasources.okf`
- `canvas.files`
- `canvas.browser`
- `sessions.permission-mode`
- `workspaces.select`
- `sessions.delegate`

Each definition carries descriptive metadata, not secret configuration:

```yaml
id: datasources.email
components:
  authority: orchestrator
  execution: agent
  presentation: cockpit
  guidance: guide
visibility: public # public | conditional | hidden
title_key: datasources.form.optEmail
summary: Attach an IMAP/SMTP mailbox with scoped agent access.
help_topic_id: datasources.email
open_action_id: datasources.email.create # desired contract; not implemented yet
guide_ref: references/datasources-email.md
visual_id: datasources.email.create
introduced_in: <logical-release-or-component-revisions>
```

Runtime resolver code evaluates the definition for the caller and session.
The definition must not duplicate credentials or raw configuration values. The
leading implementation location is an orchestrator service (for example,
`orchestrator/services/product_capabilities.py`) because the orchestrator is
already the authority for feature flags, users, grants, jobs, and deployment
configuration. Whether the definitions live in Python or a checked YAML file
is an open implementation detail; resolution stays server-side. Machine-
readable inventories should become canonical seams where practical; a coverage
test must not choose one of several duplicated datasource/UI lists and call it
the product registry.

Visibility is separate from permission. `public` capabilities may be explained
even when admin-controlled; `conditional` capabilities are returned only when
safe discovery conditions hold; `hidden` capabilities are omitted for callers
who cannot discover them. Absence from a partial result never proves that a
capability is hidden or unsupported.

A capability may span components. Layer results name the component that made
the observation, while the question type selects the relevant provenance: UI
steps use `presentation`, execution claims use `execution`, policy claims use
`authority`, and reviewed workflow wording uses `guidance`.

### 3. Effective product-capability API and tool

Add an authenticated, user-scoped endpoint, provisionally:

```text
GET /api/users/me/product-capabilities?thread_id=<owned-thread>&topic=<topic>&limit=<n>
```

Add a persistent-session tool, provisionally:

```text
get_product_capabilities(topic?: str, capability_ids?: list[str])
```

The API validates thread ownership before using a thread and resolves grants
against that thread's active project scope. Without `thread_id`, session state
is `not_applicable` or `unknown`; it is never guessed. The model does not supply
the authenticated user, project IDs, repository, or component revision.

Resolution has two explicit halves:

1. The orchestrator service resolves registry/build, deployment, authenticated
   user, and active-project facts.
2. The persistent-session tool overlays live agent facts after tools
   instantiate: exact loaded tool names, workspace/backend features, effective
   datasource type/tier, knowledge/cloud state, and current attachments.

The help/capability tool is an always-available product-introspection surface,
not part of the user-disableable Fleet Management group. It accepts only
bounded topic/ID filters, applies hard result/byte limits, and returns validated
structured content plus a concise text fallback. It may compose existing
services internally instead of making the model join many broad tool results.

An effective result has an explicit state per layer:

```json
{
  "schema_version": "1.0",
  "registry_revision": "<opaque-content-revision>",
  "evaluated_at": "<RFC-3339-timestamp>",
  "completeness": "complete",
  "product": {
    "name": "Superhuman Remote Worker",
    "release_version": "<optional-logical-release>",
    "mixed_build": false,
    "components": {
      "orchestrator": {
        "artifact_digest": "sha256:<digest>",
        "source_url": "<configured-repository>",
        "source_revision": "<full-immutable-commit>",
        "provenance_status": "declared"
      },
      "agent": {},
      "cockpit": {},
      "guide": {}
    }
  },
  "capabilities": [
    {
      "id": "datasources.email",
      "visibility": "public",
      "build": {
        "state": "supported",
        "reason_code": "included_in_build",
        "source_component": "orchestrator",
        "freshness": "fresh"
      },
      "deployment": {
        "state": "enabled",
        "reason_code": "feature_enabled",
        "freshness": "fresh"
      },
      "user": {
        "state": "allowed",
        "reason_code": "grant_present",
        "freshness": "fresh"
      },
      "session": {
        "state": "needs_attachment",
        "reason_code": "datasource_not_attached",
        "source_component": "agent",
        "freshness": "fresh"
      },
      "agent_action": "can_guide",
      "open_action_id": "datasources.email.create",
      "guide_ref": "references/datasources-email.md",
      "visual_id": "datasources.email.create"
    }
  ],
  "evaluation_errors": []
}
```

Recommended bounded enums:

| Field | Values |
|---|---|
| `completeness` | `complete`, `partial` |
| `visibility` | `public`, `conditional`, `hidden` |
| `build.state` | `supported`, `unsupported`, `unknown` |
| `deployment.state` | `enabled`, `disabled`, `degraded`, `unknown` |
| `user.state` | `allowed`, `denied`, `no_opinion`, `not_applicable`, `unknown` |
| `session.state` | `ready`, `needs_attachment`, `needs_upgrade`, `not_applicable`, `unknown` |
| `agent_action` | `can_execute`, `can_propose`, `can_guide`, `unavailable` |
| `freshness` | `fresh`, `stale`, `unknown` |
| `provenance_status` | `declared`, `verified`, `unavailable` |

Use per-layer stable `reason_code` values for model reasoning and localized UI
copy. A single top-level reason loses simultaneous limitations such as
"provider not configured" plus "mailbox not attached". Preserve provider
detail internally, map it to safe public codes, and put only bounded layer/code
pairs in `evaluation_errors`; do not embed sensitive administrator details in
free-form reasons.

`registry_revision` changes when definitions/help mappings change.
`evaluated_at`, freshness, and completeness describe the dynamic observation.
HTTP `ETag`/conditional GET may cache identical bounded responses, but clients
must refresh after grant, flag, attachment, workspace-tier, or tool-catalog
changes. A stale cached snapshot cannot authorize an operation.

The endpoint should include build/source metadata only when configured and
safe. It must not expose:

- secrets or credential presence beyond a safe capability boolean;
- private service addresses;
- hidden global features to unauthorized callers;
- grants belonging to another user or project; or
- admin-only configuration values.

### 4. Component and guide provenance

SRW is a multi-component deployment, so source identity is recorded per
component rather than flattened into one `BUILD_SHA`:

- Cockpit revision for UI labels, routes, anchors, and screenshots;
- orchestrator revision for capability/grant evaluation and product APIs;
- live agent/workspace revision for actual execution behavior; and
- guide bundle revision/content digest for the reviewed instructions loaded.

Every first-party image should carry the standard OCI `source`, `revision`,
`version`, and `documentation` annotations plus an immutable artifact digest.
Full commit IDs are used for lookup; short SHAs are display-only. OCI metadata
is declared provenance, not cryptographic proof. Verified SLSA provenance may
later upgrade `provenance_status` to `verified` by binding the artifact digest
to a trusted build and resolved source commit; it is not on the M1/M2 critical
path.

If relevant component or guide revisions differ, return `mixed_build: true`
and explain the limitation. Local/dev builds may report provenance as
`unavailable`; they must not silently point to upstream `main`.

### 5. App-guide answer contract

For a product question, the skill directs the agent to:

1. Identify the user's actual goal and route to the relevant reference.
2. Read the reference before stating product facts.
3. Query effective capabilities when the answer depends on availability,
   permissions, current tools, workspace tier, or attached resources.
4. State the layers separately when useful: "SRW supports it", "it is enabled
   here", "you may use it", and "this session is/isn't ready".
5. Give the shortest safe workflow in the user's vocabulary.
6. Offer only actions exposed by the agent's actual tools. Otherwise provide a
   validated Cockpit Open action or guide-only response.
7. If the guide and capability service do not cover the question, say what is
   unknown. Use the pinned source fallback only when allowed and relevant.
8. Never turn a proposed design found in the repository into a claim about the
   running product.
9. Treat capability output as an observation, not permission. If the user asks
   the agent to act, call the real operation so it performs current policy and
   precondition checks; do not infer success from `can_execute`.

Preferred answer shape:

```text
Short answer / current availability.

Shortest steps to the user's goal.

One relevant safety or scoping tip.

Action: offer to do it, prepare it, open the page, or show the visual only if
that action is genuinely available.
```

#### Email example

For "How can I share my email with you?", the agent should:

1. Resolve `datasources.email` and the current session's datasource state.
2. Read the email workflow reference.
3. Explain **Datasources -> New -> Email (IMAP/SMTP)**, provider/app-password
   setup, access tier, Test/Create, and attaching it to a session or project.
4. Recommend a dedicated `AI` folder allowlist when the user wants selective
   sharing, and recommend `read` or `draft` before broader access.
5. Accurately state unsupported providers or deployment/grant restrictions.
6. Offer the current `/datasources` page until the desired
   `datasources.email.create` deep-link/action contract is implemented, then
   offer the version-matched action and visual help. Do not claim that the
   agent can create or attach the datasource unless its current tools support
   that action and the operation itself re-authorizes successfully.

### 6. Public source repository fallback

The repository is useful for implementation questions and guide gaps, but only
under a strict contract:

- The model supplies only a logical topic/component and, at most, a normalized
  allowlisted path. The server selects repository and full immutable revision
  from that component's provenance; the model never chooses either value.
- Require an exact commit object and configured repository identity, including
  fork identity. Reject branches, tags, short display SHAs, missing provenance,
  and any fallback to `main` or `develop`.
- Use a dedicated read-only retriever with normalized path/extension allowlists;
  reject traversal, unsafe symlinks, and submodule escape. Enforce file-count,
  byte, MIME/content-type, archive, timeout, and response limits.
- Restrict HTTPS hosts/repositories and disallow URL credentials, IP literals,
  arbitrary ports, and redirects by default. If DNS or redirects are supported,
  revalidate every destination and block loopback, link-local, private, and
  cloud-metadata addresses.
- Return repository identity, component, full revision, normalized path,
  content digest, and origin with every excerpt. Cache only by immutable
  repository/revision/path/content identity.
- Deliver fetched text as clearly delimited untrusted data, never as system or
  skill instructions. The retriever has no mutation tools, secret access, or
  general outbound-communication authority, and source text cannot authorize a
  follow-on action.
- Prefer shipped user docs and source code over feature proposals. A file under
  `docs/features/` is evidence of design intent, not evidence that the feature
  shipped.
- If trusted component provenance is absent/unreachable, report that the source
  fallback is unavailable and continue with bundled/runtime knowledge.
- Clearly label any conclusion inferred from implementation rather than stated
  in user documentation.

This fallback is deliberately later than the bundled guide and capability
service. It must not become a shortcut around maintaining user-facing workflow
references.

### 7. Visual guidance

Visual help should progress from durable and cheap to richer and more fragile:

1. **Deep link** — take the user to the correct Cockpit page after they click.
2. **Selective generated screenshot** — only when the target is subtle, hidden,
   ambiguous among competing controls, or materially hard to explain in text.
3. **Structured help card** — render steps, a same-origin visual, and an Open
   action in the conversation.
4. **Coach marks on the real UI** — after the user opts in, navigate and
   highlight stable `data-help-id` anchors.
5. **Short video/animation** — an exceptional last resort only when motion
   communicates something text, a screenshot, and a live coach mark cannot.

Do not make an animated mockup the default. It can drift independently of the
real UI and duplicates localization and accessibility work. Strict Canvas HTML
intentionally strips scripts and animations; the explicit interactive renderer
can run a self-contained mockup, but it remains an isolated, untrusted artifact
without a trusted product-action contract. Coach marks on the actual UI are the
more reliable interactive end state.

#### Help UX and accessibility contract

- Contextual help states the benefit, never blocks the primary task, is
  dismissible, and can be reopened. Do not launch a proactive multi-step tour
  merely because the user entered a page.
- Keep availability, prerequisites, permission limits, and safety/scoping
  advice visible. Collapse only supplemental explanation or troubleshooting.
- Only one coach mark may be active. Prefer one step and cap a journey at four;
  show its outcome and step count before the user starts, with Skip/Close on
  every step.
- Rendering a help card never navigates. A real link or clearly labelled button
  such as "Open Datasources and start guide" performs user-initiated navigation
  without mutation, credential prefilling, or form submission.
- On arrival, focus moves predictably to the destination heading or requested
  anchor and the new context is announced. Interactive coach marks use
  non-modal dialog/popover semantics, not tooltips; they have an accessible
  name, visible Close, Escape dismissal, and sensible focus restoration.
- Do not trap focus when the user must operate the highlighted control. The
  active/focused control must not be obscured. If the anchor is missing,
  disabled, obscured, or cannot be positioned at the current zoom/viewport,
  stop the live journey and fall back to the text card.
- Support a zero-motion `prefers-reduced-motion` mode. Do not autoplay pointer,
  pulse, scrolling, or attention animations.

#### Screenshot inclusion and generation contract

Every screenshot remains optional to comprehension: complete localized text
steps, meaningful localized alt text, and captions are always available. Use a
tight crop with enough surrounding context, synthetic data, and no cursor,
hover-only state, credentials, private names, mailbox content, or deployment
addresses. Put numbers/simple outlines in the bitmap and render explanatory
callouts as localized HTML rather than baking prose into pixels.

Generation runs in a pinned container with fixed OS, browser/Playwright
version, fonts, locale, timezone, viewport, device-pixel ratio, and canonical
theme. Seed data, mock external dependencies, disable animations/carets, and
mask timestamps and other volatile regions. Screenshot artifacts and visual-
regression baselines are separate reviewed outputs; an AI drift job never
accepts new baselines automatically. Generate extra locale/theme/viewport
variants only when visible layout or workflow materially changes.

#### Media storage

The current skill format stores UTF-8 text files only. The recommended first
implementation therefore serves version-matched help media from the Cockpit
build under stable logical IDs, while the skill references those IDs through
the capability registry.

A later binary-skill-assets slice may extend import/export, DB storage,
resolved-config delivery, workspace materialization, MIME validation, size
limits, and Canvas presentation. Do not base initial visual help on that larger
storage migration.

#### Structured presentation

A future tool such as `show_product_help(topic_id, step_id?)` should use a
formal input/output schema and let the model choose only an allowlisted logical
topic. The server resolves localized text, action IDs, and media. Cockpit
renders a safe card with buttons; the model does not invent HTML, asset URLs,
selectors, or navigation targets. Return structured content plus text and
standard image/resource fallbacks.

Before locking a bespoke tool/event protocol, evaluate MCP Apps. A native
Cockpit renderer may still be simplest, but the structured payload should map
cleanly to a predeclared `ui://` help resource for compatible hosts. UI-
initiated operations still pass through host consent and server authorization;
ordinary text/deep-link help remains the universal fallback.

Navigation and coach marks require a user click. Screenshots use synthetic
fixtures and must never capture real credentials, mailbox contents, private
project names, or deployment addresses.

## Freshness and maintenance model

Automation should cover facts that code can prove. Human/AI-reviewed guidance
should cover intent, advice, and workflow quality.

### Deterministic checks

CI should eventually verify:

- every capability definition has a valid ID, component, visibility policy,
  guide reference, action ID, and optional visual ID;
- every `guide_ref` exists and is indexed by `app-guide`;
- the managed guide is present with DB/expert resolution off, cannot be
  shadowed, works on `none`, and refreshes in pre-existing resumed sessions;
- canonical user-visible inventories (starting with datasource types, bundled
  experts, workflow families, top-level destinations, and major feature flags)
  agree with their Cockpit/agent consumers and have capability coverage or an
  explicit exclusion;
- every action ID resolves through an explicit same-origin help-route manifest;
  do not infer supported query parameters by parsing Angular source;
- translation keys and stable `data-help-id` anchors referenced by help
  metadata exist;
- each documented Playwright journey reaches the expected page and asserts one
  visible target with the expected role, localized accessible name, enabled
  state, and route;
- component provenance is present or explicitly unavailable, uses full
  immutable revisions for lookup, and detects mixed deployments;
- generated screenshots come from the expected Cockpit revision, pinned
  environment, and synthetic fixtures;
- no guide/media artifact contains obvious credentials or private fixture
  values; and
- the skill still meets description/body budgets and parses through the
  production parser.

Not every route or tool should become a capability. The coverage registry must
support explicit non-user-facing exclusions so the test measures product
surface completeness rather than raw source counts.

### AI-assisted drift review

An AI documentation check can inspect a pull request or commit diff when it
touches likely product surfaces, including:

- Cockpit routes, navigation, forms, and translation keys;
- datasource enums, validators, or tool maps;
- expert and skill configs;
- session, job, automation, loop, Canvas, browser, cloud, and permission tools;
- feature flags and Helm values; or
- capability definitions.

The check produces a proposed patch or issue with source evidence. It does not
silently rewrite or merge product documentation, update
`last_verified_revision`, or accept visual baselines. Product QA may run the
same audit on a schedule and file drift findings against the released build.

### Feature definition of done

Once the capability registry exists, a user-visible feature is not complete
until its change set either:

- adds or updates its capability definition and guide journey; or
- records why no user-facing help change is required.

This replaces a vague "remember to update the docs" convention with a bounded
release check.

## Discovery UX

The guide only helps after users know they can ask. Add lightweight discovery
surfaces:

- a globally consistent Help action in the Cockpit shell that can pass the
  current route/topic into a session and remains available again after use;
- starter prompts in a new session, such as "What can I do here?" and "Help me
  connect my data";
- context-sensitive empty-state prompts on Jobs, Projects, Datasources,
  Automations, Experts, and Skills pages;
- page-level Help actions that open or reuse a session with the relevant
  `topic_id` rather than a generic blank chat;
- a "Was this accurate?" action on structured help cards; and
- optional demo content showing one completed project/job journey.

These affordances route into the same guide/capability system. They are not a
second documentation source. Proactive prompts are contextual, dismissible,
non-blocking, and reopenable; SRW does not force a first-run tour.

## Security, privacy, and trust boundaries

- Resolve capability state for the authenticated caller and active project;
  never return another user's grants or datasource details.
- Treat capability output as an advisory observation. Every mutating or
  privileged operation re-authorizes and revalidates at execution time.
- Return safe booleans/enums and reason codes, not secrets, hostnames, key
  names, raw administrator configuration, datasource names, folder names, or
  connection URLs. Return aggregate type/tier/readiness only.
- Keep the managed guide non-shadowable. Treat all user-authored extensions as
  untrusted prompt content with origin and collision diagnostics.
- Treat public source content as untrusted data and enforce the isolated
  retriever contract above. Fencing/sanitization supplements, but does not
  replace, path/network/tool boundaries.
- Only render same-origin, manifest-listed help media. Do not let model output
  select arbitrary remote images or HTML.
- Generate visuals with synthetic data and scan artifacts before shipping.
- Require a user gesture for route navigation, coach marks, credential forms,
  and any mutation.
- Respect deployment/fork differences. An upstream public repository must not
  override the running deployment's effective capability result.
- Do not reveal the existence of admin-only features when the caller lacks
  permission to discover them, unless the product intentionally documents the
  feature as generally available but admin-controlled.

## Failure and degradation behavior

| Failure | Required behavior |
|---|---|
| Skill reference missing | State that the guide has no reviewed workflow; do not improvise UI steps |
| Capability endpoint unavailable | Answer only stable conceptual material and qualify current availability as unknown |
| Capability result partial or resolver errored | Preserve `partial`/`unknown` and safe evaluation errors; absence does not mean unsupported or denied |
| Feature disabled or grant denied | Explain the safe user-facing reason and next owner/admin action when discoverable |
| Session lacks attachment/tool | Explain that the product supports it but this session is not ready; show how to attach/upgrade |
| Capability snapshot changes before action | The operation re-authorizes/revalidates and reports its current result; the earlier snapshot grants nothing |
| Component provenance missing or mixed | Name the affected uncertainty; skip source-derived claims for components without an immutable revision |
| Source URL/full revision missing | Skip repository fallback; bundled/runtime knowledge still works |
| Pinned source revision unreachable | Report fallback failure without switching to the default branch |
| Screenshot/help card unavailable | Fall back to text steps and a validated deep link |
| Help anchor missing/obscured/disabled | Stop the coach-mark journey and show the text card; never guess a selector or continue blindly |
| Guide conflicts with effective state | Current runtime observation wins for explanation; flag the guide as potentially stale, while execution still rechecks |

## Testing and evaluation

### Unit and contract tests

- Capability definition parsing, stable IDs, bounded enums, and duplicate
  rejection.
- Per-layer reason/source/freshness mapping, completeness propagation, registry
  revision, schema compatibility, and bounded evaluation errors.
- Resolver matrices for flag on/off, provider configured/unconfigured, grant
  allowed/denied/no-opinion, datasource attached/unattached, degraded/stale
  providers, and workspace ready/upgrade required.
- Auth, owned-thread, active-project, visibility, and cross-user scoping for the
  product-capability endpoint.
- Execution-time authorization tests proving a prior `can_execute` result does
  not bypass a changed/revoked policy.
- Response redaction: no secrets, private addresses, or unrelated user data.
- `guide_ref`, action/route manifest, translation key, and visual ID integrity.
- `app-guide` parsing, budget, reference-index agreement, managed delivery,
  non-shadowing, DB/expert flags off, `none` backend, and resumed-session
  upgrade behavior.

### Agent behavior evaluations

Keep the evaluation corpus outside the runtime skill (provisionally under
`tests/fixtures/app_guide_evals/`) so expected answers and judge criteria cannot
leak into model context. Use a fixed held-out validation split, fresh sessions,
several runs for stochastic triggers, and roughly balanced positive versus
near-miss negative prompts. Compare the new guide to no skill and the previous
guide version; record trigger rate, quality, latency, and token cost.

At minimum cover:

- broad orientation;
- sessions versus jobs versus projects;
- background delegation and job review;
- automations and project loops;
- choosing an expert;
- connecting read-only PostgreSQL;
- connecting email with a folder allowlist;
- unsupported/disabled feature questions;
- a supported feature missing from the current session;
- a feature the agent can guide but not execute;
- a deliberately undocumented question; and
- a repository proposal that must not be described as shipped;
- a near-miss project-onboarding question that must not trigger product help;
- partial/stale capability results and service failure;
- a malicious instruction in a guide/source excerpt; and
- a capability snapshot revoked before the requested action.

Cross those intents with enabled, disabled, denied, unattached, ready,
degraded, and unknown states; guide-only versus executable sessions; and
English/German where UI copy is exposed.

Evaluate distinct layers rather than hiding failures in one aggregate score:

| Layer | Deterministic assertions | Reviewed/model-judged qualities |
|---|---|---|
| Routing | Correct skill/content type/reference; correct bounded capability query | Retrieved material is relevant |
| Runtime truth | Fixture state, layer reasons, completeness, and tool arguments match | None where the answer is exact |
| Grounding | Every route, action, state, and restriction has reviewed evidence | Material claims are faithful and non-contradictory |
| Response | Required layer distinctions, abstention, and no invented action | Relevance, clarity, helpfulness, concise workflow |
| Trajectory | Correct tools/arguments; no unrequested navigation, mutation, or source escalation | Overall path is efficient and appropriate |
| Visual help | Valid topic/action/visual/anchor/locale; user activation required | Visual is understandable and useful |

Deterministic assertions lead. Version any judge model/prompt, calibrate it
against a small human-labelled set, shuffle pairwise ordering, and manually
audit disagreements. A false positive that says a dangerous/unavailable action
is usable is a zero-tolerance release failure; aggregate helpfulness cannot
hide it.

### End-to-end and visual tests

- Fresh and pre-existing resumed persistent sessions invoke the managed guide
  with DB/expert resolution off and on `none`, returning the correct mental
  model from current immutable bytes.
- The email question reports the right state under enabled, disabled, denied,
  unattached, ready, degraded, partial, and unknown fixtures.
- Deep-link/action IDs reach the expected Cockpit page only after a user click.
- Playwright journeys use role/label locators for user semantics and
  `data-help-id` only for the coach-mark anchor contract. They assert role,
  accessible name, visibility, focus, and non-obscuration.
- Help cards/coach marks get targeted ARIA snapshots and axe scans; the first
  journey also passes a manual keyboard and screen-reader checklist.
- Screenshot generation is reproducible in the pinned capture environment;
  baseline changes require review. Add mobile/zoom and reduced-motion projects.
- Help cards render safely on desktop/mobile and in English/German, with
  complete text alternatives.
- Pinned source fallback never follows the default branch when the revision is
  missing and ignores adversarial instructions in Markdown, code comments,
  commit metadata, SVG/image text, and fake tool-result content.

## Observability

Collect low-cardinality product metrics without storing private conversation
content:

- `app-guide` invocations by topic ID;
- effective-capability query outcomes and unknown reason codes;
- partial/stale evaluations, component mismatches, and snapshot-to-action
  revalidation failures;
- guide misses and source-fallback attempts/failures;
- help-card impressions, Open clicks, journey completion/abandonment, and
  accuracy feedback;
- CI capability-coverage percentage;
- reviewed guide age/last-verified component revision per journey; and
- bounded help latency/token overhead by topic.

Unknown topics and negative accuracy feedback feed the documentation backlog.
Do not log raw user questions by default.

## Principal risks and mitigations

| Risk | Mitigation |
|---|---|
| Stale or user-replaced product truth | Managed non-shadowable guide, immutable bundle reader, content-digest reconciliation on resume |
| Partial evaluation mistaken for denial | Per-layer `unknown`, completeness/freshness/errors, no inference from absence |
| Capability snapshot used as authorization | Explicit advisory contract and execution-time re-authorization tests |
| Mixed component versions teach the wrong UI/behavior | Per-component provenance and question-to-component mapping |
| New features silently outrun the guide | Canonical inventories, coverage/exclusion gates, reviewed verification metadata |
| Screenshot/localization matrix becomes a second maintenance project | Screenshot inclusion gate, HTML callouts, canonical capture profile, variants only when layout changes |
| Repository fallback causes prompt injection or SSRF | Immutable server-selected provenance, isolated read-only retriever, strict network/path/size controls, adversarial tests |
| Evaluations reward prose while missing unsafe behavior | Trace/tool assertions, state matrix, held-out A/B runs, zero-tolerance critical false positives |
| Help obstructs the task or excludes assistive technology | Optional/reopenable UX, four-step cap, predictable focus, reduced motion, semantic and manual accessibility checks |

## Rollout and compatibility

- Ship the managed text guide as a product floor, not as an ordinary optional
  DB feature. Keep an operator-only break-glass disable for rollback, with a
  visible degraded-health signal; it is not a routine deployment toggle.
- Dark-launch the Phase 2 registry/endpoint before the guide relies on it.
  Compare resolver output against known fixtures and existing grants/session
  observations without logging private payloads, then enable the read-only tool
  for internal sessions before general rollout.
- Version the capability schema with major/minor semantics. Additive fields and
  reason codes are backward compatible; removing/renaming fields or changing
  state meaning requires a major version. Unknown fields/codes are ignored and
  represented safely by older clients.
- Negotiate structured help as an optional presentation capability. Older
  Cockpit/hosts receive the same concise text and resource/deep-link fallback;
  they never need the rich renderer to answer a product question.
- Roll back guide content with its owning artifact and preserve the guide
  content digest in session observations. A mixed rollback is reported as a
  component mismatch rather than masked.
- Keep source fallback disabled by default until Phase 6 security and
  adversarial gates pass. Offline deployments remain fully supported.
- Set concrete metadata, payload, latency, and token budgets from the Phase 1/2
  baseline before general rollout; exceeding a budget degrades to a smaller
  bounded result rather than dumping the catalog into context.

## Roadmap and TODO list

### Already shipped — v1 foundation

- [x] Create bundled `config/skills/app-guide/SKILL.md`.
- [x] Add progressive-disclosure references for the original eight topics.
- [x] Add retrieve-before-answer and no-invented-UI grounding rules.
- [x] Add a focused bundled-skill structure/index test.
- [x] Materialize optional skill references through the existing skills
  runtime when model-invoked skills are enabled.

### Phase 1 — repair the guide and make it dependable

**Outcome:** every user-facing persistent expert has a current text guide on
fresh and resumed sessions, including `none` workspaces, without requiring the
DB-authored-skills or DB-experts features.

- [x] Audit user-visible changes since the v1 guide shipped, including email,
  OKF knowledge bases, datasource readiness/scope, automations/workflows,
  fleet/job management, unified loops/campaigns, Canvas, live/shared browser,
  protected cloud review, workspace tiers, and relevant settings. Email and
  OKF were audited in M1b; Automations and Fleet/job management were audited in
  M1c; Canvas/browser, permission modes, grants, workspace tiers, and related
  session settings were audited in M1d; unified loops/campaigns and Protected
  Cloud review were audited in M1e.
- [x] Run a small delivery spike and record the choice: extend `use_skill` with
  an immutable system-bundle reader or add bounded `read_product_guide`; prove
  that authoritative bytes do not depend on mutable workspace files. M1a chose
  the bounded dedicated reader.
- [x] Generalize the existing Canvas exception into a managed system-skill
  floor for all user-facing persistent experts, independent of DB skill/expert
  flags; keep it out of autonomous worker catalogs by default.
- [ ] Add an operator-only break-glass disable and visible degraded-health
  signal without turning the guide back into a normal feature-flag dependency.
- [x] Reserve `app-guide` from owner/project/global replacement at resolution
  and create/import/update boundaries; emit a clear collision diagnostic and
  allow extensions only under distinct names.
- [x] Add digest-owned guide upgrade/withdraw reconciliation for existing
  resumed sessions. M1a refreshes the in-memory bundle on every tool rebind and
  deliberately has no authoritative workspace copy to reconcile.
- [x] Add the high-value split references listed in this design, including
  content type, capability IDs, and journey IDs. Email and OKF shipped in M1b,
  Automations and Fleet/delegation in M1c, and Canvas/browser plus
  permissions/availability in M1d; project loops/campaigns and Protected Cloud
  shipped in M1e.
- [x] Complete the remaining original-reference audit. M1d corrected the
  broad session, overview, and file/workspace claims touched by the new focused
  guides; M1e corrected project organization, loop, session/cloud, and
  workspace overlaps. M1f work package 1 corrected Jobs, Experts,
  Memory/Knowledge, and the remaining broad reference conflicts, added
  metadata, and pinned their consequential contracts.
- [x] Repair automation worker-expert selection uncovered by the guide audit:
  filter the Cockpit picker, persist DB experts by UUID, define pinned versus
  effective-default fire semantics, backfill legacy rows, and cover
  create/edit/fire/delete/bundle paths.
- [x] Update `SKILL.md` triggers and logical-topic routing for the managed
  reader without bloating Layer 1/2 context.
- [ ] Verify the activated guide procedure remains available after session
  context compaction, while unneeded references remain unloadable/on demand.
- [x] Remove the current unsupported suggestion that the default session can
  attach a datasource; offer only observed tools or validated guide actions.
- [x] Create the first minimal canonical/coverage seam for datasource types so
  the repaired email guide immediately gains a drift check.
- [ ] Add balanced trigger evaluations for broad/specific product questions and
  near-miss negatives such as project/codebase onboarding.
- [ ] Verify fresh and pre-existing resumed k3d sessions with DB skills and DB
  experts disabled, including `none`, `virtual`, and normal shell workspaces.
- [ ] Verify an off-doc question produces an honest gap instead of a guess.

**Exit gate:** the chart-default deployment and a session created before an
upgrade answer the core evaluation set from the same current immutable guide,
including the email folder-allowlist journey, with no shadowing or workspace
dependency.

### Phase 2 — runtime capability plane

**Outcome:** answers distinguish installed, enabled, allowed, ready, and
agent-actionable state.

- [ ] Define stable capability IDs, enums, reason codes, and schema versioning.
- [ ] Define per-layer evaluation objects, visibility, completeness,
  freshness, bounded errors, registry revision, and advisory/non-authorization
  semantics.
- [ ] Choose the definition format (Python registry or checked YAML) and add
  schema validation.
- [ ] Implement definitions and pure server-side resolvers in a dedicated
  orchestrator service/router for the first capability set: sessions,
  jobs, experts, projects/loops, automations, datasources, Canvas/browser,
  cloud, knowledge, and workspace tiers.
- [ ] Compose existing grants, flags, catalogs, service readiness, and session
  context rather than duplicating their authority; scope grants to the active
  owned thread/project rather than all visible projects.
- [ ] Stamp orchestrator, agent, Cockpit, guide, and other relevant first-party
  artifacts with full source revision, source URL, version/documentation OCI
  metadata, and artifact digest; report declared/unavailable provenance and
  mixed builds.
- [ ] Add the authenticated endpoint with owned `thread_id`, topic/ID filters,
  result/byte limits, and `not_applicable` session state when no thread is
  supplied.
- [ ] Record the final loaded tool-name set in persistent `ToolContext` after
  tool instantiation.
- [ ] Add `get_product_capabilities` in a dedicated always-on product-help tool
  group and overlay exact loaded tools, backend features, datasource type/tier,
  knowledge/cloud state, and attachments onto server facts.
- [ ] Publish and validate the tool's formal structured-output schema with a
  concise text fallback for clients that cannot consume structured content.
- [ ] Update `app-guide` to require the tool for availability/permission/current
  context questions.
- [ ] Define snapshot refresh/cache behavior for grant, flag, attachment,
  workspace, and tool-catalog changes; use `ETag` only as an optimization.
- [ ] Dark-launch the resolver/endpoint, compare privacy-safe outcomes to known
  fixtures/current seams, then canary the read-only agent tool.
- [ ] Add resolver matrix, owned-thread/auth-scope, visibility, redaction,
  payload-bound, partial/stale/error, mixed-build, and graceful-degradation
  tests.
- [ ] Add a revocation-between-snapshot-and-action test proving each real
  operation authorizes again.
- [ ] Add English and German reason-code presentation where exposed in UI.

**Exit gate:** the email evaluation correctly distinguishes at least supported
but disabled, enabled but denied, allowed but unattached, ready, degraded,
partial/unknown, and revoked-before-action states without leaking private
configuration.

### Phase 3 — deterministic freshness gates

**Outcome:** common product drift breaks CI or produces an explicit coverage
decision instead of silently aging the guide.

- [x] Create the canonical machine-readable datasource-type inventory and
  assert backend, Cockpit, agent-grouping, and guide parity.
- [ ] Create canonical Cockpit route/action metadata before using duplicated
  route or control lists as coverage authorities.
- [ ] Map each initial capability to its relevant components, visibility policy, guide
  reference, action ID, and optional visual ID.
- [x] Add explicit guide-coverage decisions for canonical datasource types.
- [ ] Add explicit coverage/exclusion checks for bundled disk experts, workflow
  families, top-level destinations, and major feature flags; dynamic user
  content is not a release-coverage target. M1c covers the selectable live
  Fleet and Workflow tool groups; M1d covers the Canvas and direct-browser tool
  inventories plus every current capability-grant key; M1e pins the loop
  analysis-role/campaign-default constants and the Protected Cloud mount
  selection boundary. Disk experts, top-level destinations, flags, and broader
  workflow inventories remain open.
- [ ] Add an explicit same-origin help-route/action manifest and validate guide
  paths, translation keys, and actions against it.
- [ ] Add stable `data-help-id` anchors to high-value workflow controls.
- [ ] For every anchor, also assert role, localized accessible name,
  visibility/enabled state, route, uniqueness, and non-obscuration.
- [ ] Put the held-out response/routing/trajectory corpus outside the runtime
  skill and add A/B, paraphrase, negative-trigger, state-matrix, latency, and
  token measurements.
- [ ] Build Playwright guide journeys with synthetic fixtures only after route
  and anchor contracts are stable.
- [ ] Add a release-readable capability coverage report.
- [ ] Add an AI drift-review job that drafts patches/findings from relevant
  diffs with file-level evidence; never auto-merge, advance verification
  metadata, or accept visual baselines.
- [ ] Add a scheduled Product QA audit for release drift and guide misses.
- [ ] Update feature/PR guidance so user-visible changes must update or exempt
  capability/help coverage.

**Exit gate:** adding a new datasource type or changing a documented workflow
without a coverage decision fails an appropriate deterministic check.

### Phase 4 — screenshots, deep links, and help cards

**Outcome:** the agent can show a safe, accessible, version-matched visual path
in addition to complete text guidance.

- [ ] Define same-origin `visual_id` metadata and asset lookup.
- [ ] Implement the explicit action/deep-link handler for the first journey;
  `/datasources?new=email` remains a desired contract until this lands.
- [ ] Apply the screenshot inclusion gate to candidate journeys and create only
  those where a visual materially beats text; start with email creation if it
  qualifies.
- [ ] Build the pinned capture profile with synthetic fixtures, mocked external
  calls, masked volatility, fixed rendering inputs, and reviewed baselines.
- [ ] Render numbered/text-light bitmap callouts with localized HTML
  explanations, alt text, and complete standalone steps.
- [ ] Evaluate native Cockpit rendering against MCP Apps, then define a formal
  `show_product_help(topic_id, step_id?)` structured schema with text/resource
  fallbacks.
- [ ] Add presentation-capability negotiation so older clients use the same
  text/resource fallback and ignore unsupported rich fields safely.
- [ ] Render a Cockpit help card with localized steps, screenshot, Open action,
  visible prerequisites/safety, fallback copy, and accuracy feedback.
- [ ] Add a globally consistent, reopenable Cockpit Help action that passes the
  current topic/route into a session.
- [ ] Verify mobile/zoom, canonical theme, reduced motion, English/German,
  focus, ARIA, axe, and text-only fallback behavior.
- [ ] Decide retention/versioning for generated screenshots across releases.

**Exit gate:** asking how to share email can show the exact current form using
synthetic data and an explicit user-clicked Open action.

### Phase 5 — live coach marks

**Outcome:** users can opt into a walkthrough on the real Cockpit UI.

- [ ] Define the coach-mark journey schema over stable `data-help-id` anchors,
  allowing one active journey, preferring one step, and hard-capping at four.
- [ ] Show the benefit/outcome and step count before starting; require a user
  gesture before navigation or highlighting and provide Skip/Close every step.
- [ ] Use accessible non-modal dialog/popover semantics with an accessible
  name, Escape, visible Close, predictable focus movement/restoration, and no
  focus trap around the highlighted page control.
- [ ] Handle missing, disabled, hidden, or obscured controls and narrow/zoomed
  layouts by stopping and returning to the text card.
- [ ] Resume or restart a journey after route changes without losing the user's
  place or launching automatically.
- [ ] Meet keyboard, screen-reader, zero-motion, bidirectional-layout, and
  localization needs.
- [ ] Add Playwright role/anchor/focus/mobile/reduced-motion coverage for every
  journey plus a manual keyboard/screen-reader checklist for the first one.
- [ ] Measure completion and abandon rates before creating additional tours.

**Exit gate:** at least one high-value journey works against the actual UI and
fails safely when the capability is unavailable.

### Phase 6 — pinned public-source fallback

**Outcome:** implementation questions and genuine guide gaps can use the open
repository without confusing planned or newer code with the installed build.

- [ ] Consume the per-component repository/full-revision/digest provenance from
  Phase 2; the model never supplies repository or ref.
- [ ] Implement a dedicated isolated, read-only source retriever rather than
  granting ordinary help questions general research/browsing authority.
- [ ] Add exact-commit enforcement; normalized path/extension allowlists;
  traversal, unsafe-symlink, and submodule defenses; and byte/file/type/time
  limits.
- [ ] Add strict HTTPS host/repository configuration, redirect policy, DNS/IP
  revalidation, and blocking for credentials, arbitrary ports, loopback,
  link-local, private, and metadata addresses.
- [ ] Return delimited untrusted excerpts with repository, component, full
  revision, path, content digest, and origin; cache only immutable identities.
- [ ] Define precedence between fork source, upstream source, bundled guide,
  runtime observations, and execution-time checks.
- [ ] Add source attribution and "implementation inference" wording.
- [ ] Add tests for missing/full revision, mixed components, unreachable
  commit, fork identity, redirects/DNS rebinding, attempted default-branch
  fallback, and proposal-versus-shipped confusion.
- [ ] Add adversarial content tests for Markdown, code comments, commit
  metadata, SVG/image text, and fake tool results; assert no tool escalation,
  secret disclosure, memory persistence, or ref/path override.
- [ ] Keep this feature optional and non-blocking for offline deployments.

**Exit gate:** every excerpt is bound to the relevant component's full running
revision and digest; no retrieved instruction can alter lookup scope or trigger
an action; runtime observation and execution-time checks always win.

### Phase 7 — optional binary skill assets and rich media

**Outcome:** portable skills may carry audited media when that is demonstrably
better than Cockpit-owned versioned assets.

- [ ] Decide whether binary portability is needed after help-card usage data.
- [ ] Before accepting binary files, add archive byte/file/decompression limits
  missing from the current text-only ZIP import path.
- [ ] If needed, design binary-safe ZIP import/export and DB storage (`BYTEA` or
  object storage), with MIME allowlists, size limits, hashes, and quotas.
- [ ] Extend resolved-config/workspace delivery without embedding large assets
  in model context.
- [ ] Add Canvas/media rendering and security tests.
- [ ] Evaluate short WebM/animation only with evidence that motion communicates
  something text, screenshots, and coach marks cannot.
- [ ] If video ships, prohibit autoplay and provide keyboard controls, captions,
  transcript/equivalent description, reduced-motion behavior, and localized
  alternatives; the full task remains possible from text alone.

**Exit gate:** binary/rich media is versioned, portable, bounded, accessible,
and does not weaken existing content or rendering boundaries.

### Phase 8 — discovery and continuous improvement

**Outcome:** users discover help naturally and guide quality improves from
evidence.

- [ ] Add new-session starter prompts.
- [ ] Add context-specific help prompts to major empty states.
- [ ] Add page-to-session topic handoff from the consistent shell Help action
  and page-level actions without starting a forced tour.
- [ ] Add privacy-preserving topic, miss, click, and feedback metrics.
- [ ] Review unknown topics and negative feedback as a recurring backlog.
- [ ] After repeated inaccurate/unsuccessful help, offer a safe report/admin
  escalation rather than repeating the same answer.
- [ ] Consider a dedicated public Product Guide expert only after the in-session
  experience is proven.
- [ ] Add or refresh demo content for the most important end-to-end journey.

## Milestones and dependency order

| Milestone | Phases | User-visible result | Depends on |
|---|---|---|---|
| M1: Reliable text guide | 1 | Fresh/resumed sessions on every persistent tier accurately explain the current core product | Managed system-guide delivery |
| M2: Truthful availability | 2 | The agent explains supported/enabled/allowed/ready observations with component provenance, without pre-authorizing actions | M1 |
| M3: Drift-resistant docs | 3 | Feature changes require a help coverage decision | M2 capability IDs |
| M4: Visual help | 4 | Consistent Help, validated actions, selective screenshots, and accessible help cards | M2 IDs/provenance, M3 anchors/journeys |
| M5: Guided UI | 5 | Opt-in coach marks on the real interface | M4 help protocol |
| M6: Source fallback | 6 | Pinned implementation lookup for gaps | Component provenance from M2 |
| M7: Rich/portable media | 7 | Optional binary assets/video if justified | M4 usage evidence |
| M8: Continuous onboarding | 8 | Discoverability and feedback-driven improvement | M1-M4 |

M1 and M2 are the product-critical path. M3 prevents recurrence. M4 is the
first visual milestone. M5-M8 are incremental and should not delay accurate
text answers.

## Overall acceptance criteria

The feature is successful when:

1. A chart-default fresh deployment and a pre-upgrade resumed session expose
   the managed guide to every user-facing persistent expert, including `none`,
   with DB skill/expert resolution disabled.
2. Authoritative guide content is immutable, digest-reconciled, and cannot be
   silently replaced by owner/project/global skills or mutable workspace bytes.
3. The agent answers the held-out core evaluation set from reviewed,
   version-matched references rather than model priors.
4. Availability answers explicitly distinguish build, deployment, user,
   session, and agent actionability where relevant.
5. Capability payloads are owned-thread/project scoped, bounded, explicit about
   completeness/freshness/errors, and contain no secrets or private
   infrastructure/datasource details.
6. Capability snapshots are never used as authorization; revocation or state
   change before execution is enforced by the actual operation.
7. The email folder-allowlist example works end to end and changes correctly
   under disabled, denied, unattached, ready, degraded, partial, and unknown
   fixtures.
8. Relevant orchestrator/agent/Cockpit/guide identities use full component
   revisions and artifact digests, and mixed/unavailable provenance is visible.
9. Adding or changing a registered user-visible capability requires a guide
   coverage decision in CI.
10. Visual help uses complete text alternatives, current synthetic fixtures,
    same-origin assets, semantically validated targets, accessible focus/motion
    behavior, and explicit user gestures.
11. Repository fallback is component-revision-pinned, optional, isolated,
    size/network/path bounded, attributed, and never overrides runtime or
    execution-time truth.
12. Missing, partial, mixed, or stale knowledge produces an honest limitation,
    not an invented workflow or denial.
13. Critical false-positive capability claims have a zero-tolerance release
    gate, with routing, grounding, response, and trajectory measured separately.
14. A consistent Help action is available and contextual help remains optional,
    dismissible, and reopenable.
15. The guide remains progressively disclosed: unrelated session turns pay only
    the bounded catalog metadata cost.

## Locked decisions

1. `app-guide` remains the primary product-help mechanism; a dedicated expert
   is optional and secondary.
2. The managed guide is available to all user-facing persistent experts but
   not autonomous workers by default.
3. Reviewed guide content is a reserved, non-shadowable product artifact read
   from immutable running-product bytes; mutable workspace copies are not an
   authority.
4. Product support, deployment enablement, user permission, session readiness,
   and agent actionability remain separate per-layer evaluations with
   completeness, freshness, provenance, and safe reason codes.
5. Capability state is authoritative for explaining the observation at
   `evaluated_at`, but is advisory for planning/UI and never authorizes an
   operation. Execution always rechecks.
6. Provenance is component-specific. Full immutable revisions and artifact
   digests drive lookup; short/global SHAs are insufficient, and mixed builds
   are represented explicitly.
7. The public repository is an isolated, server-selected, component-revision-
   pinned fallback, never an unpinned primary source or general browsing tool.
8. Deterministic generation/checks cover inventory and exact claims; reviewed
   prose covers mental models, safe defaults, and workflow advice.
9. AI may draft documentation changes and findings but does not auto-merge
   product truth, advance verification metadata, or approve screenshot
   baselines.
10. Text precedes deep links and selective screenshots; opt-in real-UI coach
    marks precede video/animated mockups. A journey has at most four steps.
11. Structured help resolves allowlisted topic/action IDs server-side; the
    model does not invent routes, selectors, HTML, assets, repositories, or
    revisions. MCP Apps is evaluated before fixing a bespoke protocol.
12. Navigation and coach marks require an explicit user gesture, preserve
    predictable focus/reduced-motion behavior, and fall back to complete text.
13. All help surfaces work without public-network access and degrade to honest
    text/unknown state.
14. Critical false-positive capability claims are release blockers; one
    aggregate helpfulness score cannot hide them.
15. M1 uses a dedicated bounded `read_product_guide(topic_id)` tool; ordinary
    `use_skill` and mutable workspace files are not authoritative for the
    managed guide.

## Open implementation decisions

- Capability definitions in Python versus checked YAML/JSON.
- Exact Phase 2 capability endpoint/tool names, payload/result limits, cache
  TTL, and refresh/notification mechanism.
- Mapping an optional logical release version over independently deployable
  component revisions.
- The initial list of explicit capability coverage/exclusion registries.
- Native Cockpit structured rendering versus MCP Apps where host support
  exists.
- Cockpit static assets versus a dedicated versioned help-media endpoint.
- Which journeys need per-locale/theme/viewport screenshots after applying the
  inclusion gate.
- Direct allowlisted source-host fetching versus an administrator-controlled
  immutable source mirror; either uses the same isolated server-side contract.
- Whether binary skill assets ever justify their storage/runtime complexity.

Resolve these during the corresponding phase design, not before M1/M2 work
requires them.
