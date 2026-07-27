---
name: app-guide
description: >-
  Do not use for repository orientation, application/code questions, or
  generic advice merely because they mention a worker, job, canvas, loop,
  memory, SQL, or email. Use when the user asks what SRW can do, how to use an
  SRW feature, or why an SRW tool, permission, connector, or workspace is
  unavailable. Covers sessions, jobs, Fleet/delegation, automations, experts,
  projects, loops/campaigns, connectors, Protected Cloud, Canvas/browser,
  workspace tiers, memory, files, and integrations. Load the current managed
  guide and focused reference with read_product_guide; for a here-and-now
  availability claim, then check get_product_capabilities when that tool is
  available. Answer from those sources, never priors or mutable workspace
  copies. For a combined workflow, read the index: load a topic when its row
  covers the workflow or its limitation; otherwise report a guide gap without
  composing adjacent features.
display_name: App Guide
icon: help
color: "#f9e2af"
tags:
  - onboarding
  - help
  - product
---

# App Guide

The user is asking about the product you are running inside — Superhuman
Remote Worker (SRW) — not about their task. You know your own tools, but the
product around you (the Cockpit UI, worker jobs, experts, projects, loops,
connectors) is not fully described by your visible tools, and guessing about it
is how users get taught features that don't exist. This managed skill ships with
the running product; `read_product_guide` supplies its current procedure and one
focused reference without relying on mutable workspace files. Your job is to
retrieve, then explain — in the user's terms, at the user's level.

The mental model that anchors everything: **sessions** are interactive — a
conversation with an agent like this one; **jobs** are autonomous — an agent
works through a goal on its own and comes back with results; **projects** tie
them together with shared knowledge, connectors, and (optionally) a
self-improving loop. Everything else hangs off that triangle.

## The answer

**1. Make a coverage decision, then route.** Match the user's requested
**outcome**, not just nouns that resemble topic names. "What can I do here?"
and anything vague routes to `overview`. A focused topic matches only when its
index row explicitly covers the requested action, state, or workflow.
Documented components are not proof that SRW supports combining them. For an
exact end-to-end outcome, call `index` first. If an index row explicitly covers
that workflow **or a limitation that decides it**—for example, Automations'
current connector limits—load that focused topic before answering. If no row
covers the outcome, call `index` only, state an explicit guide gap, and stop;
do not load the nearest topic to manufacture a setup. Questions that explicitly
ask about several individually documented topics may still need those focused
topics, but never claim the combination works unless a reference says it does.
A scheduled or recurring outcome that reads from or writes through a connector
is covered by the `automations` limitation row: load `automations` and explain
the connector boundary rather than treating the outcome as an unknown guide
gap.
`permissions-and-availability` explains a known SRW session feature's current
gates; it is not a catch-all place to search for a feature absent from the
index.
Enterprise identity administration terms such as SSO, SCIM, SAML, Okta,
directory sync, and identity or group mapping are not project-group or
datasource workflows. The current index has no built-in Cockpit setup for
them: after reading `index`, state the guide gap and stop unless the index
gains an explicit identity-administration row.
When the requested outcome is future recall or where to record a durable
project fact, route to `memory-and-knowledge` even if the question also
mentions a session or `/compact`.

**2. Read before you answer.** Call
`read_product_guide(topic_id="<topic-id>")` for the matching topic — even when
you think you know. If routing is uncertain, call it with `topic_id="index"`
first. The index is a router, not a substitute for a matching focused topic.
A topic response includes this procedure and the focused reference.
Stable product facts come from the current guide. Current capability state
comes from the same-turn `get_product_capabilities` response when Step 3
requires it. Currently visible operation tools and evidence the user just
showed you may confirm narrower facts; use no other source. Never read
`skills/app-guide/` from the workspace; any such copy is not authoritative.

**3. Check live state only when the answer needs it.** After reading the
focused guide, call `get_product_capabilities` in the same turn when the user
asks about this deployment, their permission, this session's workspace,
connector attachment, loaded tools or readiness, why something is unavailable,
or whether you can act now. Do not call it for stable concepts, reviewed
Cockpit steps, safety advice, or how-to questions that the guide already
answers.

For a focused live-state question, copy only the exact relevant
`capability_ids` listed in the focused reference. Product-guide topic IDs and
capability-tool topic IDs are different namespaces: never pass a guide
`topic_id` as the capability tool's `topic`. For a broad “what can this session
do right now?” inventory, call the capability tool without filters. If the
focused reference lists no capability ID for the requested live fact, or none
of its IDs cover that fact, say the live registry does not cover it and treat
it as unknown; do not query an adjacent capability. If the capability tool is
not visible or returns `unavailable`, keep stable guide instructions available
but say you cannot inspect current availability. A visible operation may still
be offered as an attempt, but it does not confirm readiness before its own
current checks run.

Treat the tool's top-level `status` as authoritative and read the whole
response, not only `summary`. State the build, deployment, user, and session
layers separately when they decide the answer, followed by `agent_action`.
Preserve `unknown`, `no_opinion`, `degraded`, `needs_attachment`,
`needs_upgrade`, and `not_ready`; never rewrite them as disabled, denied, or
unsupported. A `partial` result, partial `completeness`, or `truncated=true`
means affected or omitted facts remain unknown. If `product.mixed_build=true`,
say components report different revisions before making version-sensitive
claims; `null` means build uniformity could not be determined.
`product.mixed_build=false` means only that known observed revisions agree; it
does not prove that every component was observed.

A capability result is an advisory snapshot at `evaluated_at`, never
authorization or a promise of success. `can_execute` means the capability
family appeared ready and a matching tool was loaded at observation time; it
does not prove every operation in that family is callable. `can_guide` means
explain only. To act, call the exact operation tool currently visible and
report its result; treat that result, not the earlier snapshot, as the action
outcome. Operations enforce their own action-time checks, which vary by tool.
For the current email send path, those checks cover the active shared
connector binding, effective tier, and unattended-send setting; do not claim
that an already-bound call re-fetched an out-of-band grant or other upstream
policy unless the operation reports that check. A next-turn rebind can reflect
newly resolved policy, but it is not proof that every prior closure did. Never
pass the capability result to an operation as authority.

**4. Answer like a guide, not a manual.** Lead with the shortest path to the
user's actual goal, in their vocabulary. A new user gets the mental model
first; a specific how-do-I gets the steps. Include every prerequisite or limit
that decides whether the requested path will actually work; a partial recipe
is not a short recipe. For a shared-browser workflow, state before the control
steps that the currently proven path requires a **Container workspace** and is
deployment-dependent; Virtual and None cannot host it, and VM support must not
be promised. Don't dump a doc when a paragraph answers the question.

**5. Offer only actions you can actually take.** If the user's goal maps to a
tool currently visible to you, offer that after explaining. Otherwise give the
reviewed Cockpit path from the reference. Do not imply that explaining a
feature means this session can configure or operate it.

**6. If the docs don't cover the exact outcome, say so.** Use direct language
such as "the guide does not document this exact workflow" or "I cannot confirm
an exact Cockpit setup from the guide." Name what you looked in and give your
best verified pointer (a Cockpit page, an admin, the project README), but do not
turn adjacent documented features into a recipe. Related primitives may be
mentioned only as clearly labelled possibilities that still need verification,
not as steps, supported composition, or current UI. Features can also be
deployment-dependent (admin-configured or flag-gated); when a doc marks
something that way, say "your deployment may not have this enabled" instead of
asserting it exists.

## The index

| Question is about... | Topic ID | Bundled reference |
|---|---|---|
| First orientation, "what is this / what can I do here?", how the pieces fit | `overview` | `references/overview.md` |
| This chat: what you can do here, files, models, skills, interrupting, lifecycle | `sessions` | `references/sessions.md` |
| Autonomous work: creating jobs, autonomy levels, statuses, approving/resuming, watching progress, results | `jobs` | `references/jobs.md` |
| Sending background work from a session, Fleet Management, parallel jobs, worker subagent delegation | `fleet-and-delegation` | `references/fleet-and-delegation.md` |
| Scheduled jobs: creating, testing, pausing, catchup, safety limits, current trigger and connector limits | `automations` | `references/automations.md` |
| Canvas files, editable previews, direct browser tools, shared browser, taking/releasing control | `canvas-and-browser` | `references/canvas-and-browser.md` |
| The agent roster: which expert for which task, custom experts | `experts` | `references/experts.md` |
| Projects, members, shared context, settings | `projects-and-loops` | `references/projects-and-loops.md` |
| Project loops: Standard/parallel stages, Campaign scheduling, budgets, pause/resume/stop | `project-loops` | `references/project-loops.md` |
| Connector overview: supported types, access, attachment, databases, WebDAV, repositories, MCP, credential files | `datasources` | `references/datasources.md` |
| Connecting Email: providers, app passwords, access tiers, folder and recipient limits, attaching a mailbox | `datasources-email` | `references/datasources-email.md` |
| Connecting an external OKF Knowledge Base: Git source, indexing, readiness, reindexing, read-only behavior | `datasources-okf` | `references/datasources-okf.md` |
| Protected Cloud sessions: eligibility, staging, whole-diff review/apply/reject, troubleshooting | `protected-cloud` | `references/protected-cloud.md` |
| Permission modes, capability grants, workspace tiers, live tool settings, why a known session feature is unavailable (not unknown product features) | `permissions-and-availability` | `references/permissions-and-availability.md` |
| What agents remember, the knowledge base, browsing/searching it | `memory-and-knowledge` | `references/memory-and-knowledge.md` |
| Files, deliverables, cloud storage, git, citations | `files-and-integrations` | `references/files-and-integrations.md` |

## Don't

- **Answer product questions from priors** — read the reference first, every time; the product moves fast and your priors are stale.
- **Load this skill with `use_skill` or `read_file`** — only
  `read_product_guide` supplies the current managed bytes.
- **Invent UI labels, buttons, or features** — if you didn't read it in a reference or see it in your own tools, it doesn't exist.
- **Compose undocumented workflows** — a schedule, connector, prompt, expert,
  or permission documented separately does not prove they work together.
- **Assert deployment-dependent features** — flag-gated or admin-configured items are "may be available", not "is".
- **Treat a capability snapshot as permission** — current operations still
  enforce their own state and policy.
- **Lecture past the question** — answer what was asked; offer the next-most-useful thing as a follow-up, not a wall of text.
- **Confuse this with project-onboarding** — that skill orients *you* in the user's project content; this one explains *the app* to the user.
