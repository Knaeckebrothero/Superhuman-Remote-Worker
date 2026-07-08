---
name: app-guide
description: Use when the user asks what this app can do, how to use it, or what something in it means — "what can I do here?", "how do jobs work?", "what's an expert / a project / a loop?", "why is my job paused?", "how do I give the agent access to my database?". Covers the product itself — sessions, jobs, experts, projects and loops, datasources, memory and the knowledge base, files and integrations — via bundled usage docs under references/. Read the matching doc and answer only from it, never from priors. For explaining the app to its user, not for orienting yourself in a project's content (that's project-onboarding).
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
product around you (the cockpit UI, worker jobs, experts, projects, loops,
datasources) is not in your context, and guessing about it is how users get
taught features that don't exist. This skill bundles the product's usage
documentation under `references/`; your job is to retrieve, then explain — in
the user's terms, at the user's level.

The mental model that anchors everything: **sessions** are interactive — a
conversation with an agent like this one; **jobs** are autonomous — an agent
works through a goal on its own and comes back with results; **projects** tie
them together with shared knowledge, datasources, and (optionally) a
self-improving loop. Everything else hangs off that triangle.

## The answer

**1. Route the question.** Find the topic in the index below. "What can I do
here?" and anything vague routes to `overview.md`. Questions spanning topics
(e.g. "how do I set up a research pipeline?") route to each involved doc —
they're short.

**2. Read before you answer.** `read_file` the matching
`skills/app-guide/references/<topic>.md` — even when you think you know.
Product facts come from these docs, your own visible tools, and what the user
just showed you — nowhere else.

**3. Answer like a guide, not a manual.** Lead with the shortest path to the
user's actual goal, in their vocabulary. A new user gets the mental model
first; a specific how-do-I gets the steps. Don't dump a doc when a paragraph
answers the question.

**4. Offer to do it, not just explain it.** If the user's goal maps to an
action you can take with your own tools — create the job, start the loop,
attach the datasource — offer that after explaining. The best tutorial is the
thing happening.

**5. If the docs don't cover it, say so.** Name what you looked in, give your
best pointer (a cockpit page, an admin, the project README) — and don't
improvise an answer. Features can also be deployment-dependent
(admin-configured or flag-gated); when a doc marks something that way, say
"your deployment may not have this enabled" instead of asserting it exists.

## The index

| Question is about... | Read |
|---|---|
| First orientation, "what is this / what can I do here?", how the pieces fit | `references/overview.md` |
| This chat: what you can do here, permission modes, files, models, skills, interrupting | `references/sessions.md` |
| Autonomous work: creating jobs, autonomy levels, statuses, approving/resuming, watching progress, results | `references/jobs.md` |
| The agent roster: which expert for which task, custom experts | `references/experts.md` |
| Projects, members, shared context, the self-improvement loop | `references/projects-and-loops.md` |
| Connecting databases and data: types, what the agent can then do | `references/datasources.md` |
| What agents remember, the knowledge base, browsing/searching it | `references/memory-and-knowledge.md` |
| Files, deliverables, cloud storage, git, citations | `references/files-and-integrations.md` |

## Don't

- **Answer product questions from priors** — read the reference first, every time; the product moves fast and your priors are stale.
- **Invent UI labels, buttons, or features** — if you didn't read it in a reference or see it in your own tools, it doesn't exist.
- **Assert deployment-dependent features** — flag-gated or admin-configured items are "may be available", not "is".
- **Lecture past the question** — answer what was asked; offer the next-most-useful thing as a follow-up, not a wall of text.
- **Confuse this with project-onboarding** — that skill orients *you* in the user's project content; this one explains *the app* to the user.
