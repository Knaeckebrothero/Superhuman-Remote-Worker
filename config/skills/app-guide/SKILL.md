---
name: app-guide
description: Use when the user asks what SRW can do, how to use it, or what something in it means — "what can I do here?", "how do jobs work?", "can you run this in the background?", "how do I schedule it?", "how do loops or campaigns work?", "how do I share my email?", "can I review cloud changes first?", "can you show this on Canvas?", or "why is this tool, permission mode, or workspace unavailable?". Covers sessions, jobs, fleet/delegation, automations, experts, projects, loops/campaigns, connectors, Protected Cloud, Canvas/browser, grants, workspace tiers, memory, files, and integrations. Load the current managed guide and its focused bundled references/ with read_product_guide; answer from them, never from priors or mutable workspace copies. For an exact combined workflow not explicitly covered by the index, load index only and report the guide gap instead of composing related features. For explaining the app to its user, not for orienting yourself in project content (that's project-onboarding).
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
Documented components are not proof that SRW supports combining them. If the
user requests an exact end-to-end outcome that no index row covers, call
`index` only, state an explicit guide gap, and stop: do not load the nearest
topic to manufacture a setup. Questions that explicitly ask about several
individually documented topics may still need those focused topics, but never
claim the combination works unless a reference says it does.

**2. Read before you answer.** Call
`read_product_guide(topic_id="<topic-id>")` for the matching topic — even when
you think you know. If routing is uncertain, call it with `topic_id="index"`
first. A topic response includes this procedure and the focused reference.
Product facts come from that response, your own currently visible tools, and
what the user just showed you — nowhere else. Never read
`skills/app-guide/` from the workspace; any such copy is not authoritative.

**3. Answer like a guide, not a manual.** Lead with the shortest path to the
user's actual goal, in their vocabulary. A new user gets the mental model
first; a specific how-do-I gets the steps. Don't dump a doc when a paragraph
answers the question.

**4. Offer only actions you can actually take.** If the user's goal maps to a
tool currently visible to you, offer that after explaining. Otherwise give the
reviewed Cockpit path from the reference. Do not imply that explaining a
feature means this session can configure or operate it.

**5. If the docs don't cover the exact outcome, say so.** Use direct language
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
| Permission modes, capability grants, workspace tiers, live tool settings, why a feature is unavailable | `permissions-and-availability` | `references/permissions-and-availability.md` |
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
- **Lecture past the question** — answer what was asked; offer the next-most-useful thing as a follow-up, not a wall of text.
- **Confuse this with project-onboarding** — that skill orients *you* in the user's project content; this one explains *the app* to the user.
