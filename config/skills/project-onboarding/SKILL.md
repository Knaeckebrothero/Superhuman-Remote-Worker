---
name: project-onboarding
description: Use when you're dropped into an unfamiliar project and need to get your bearings before acting — a code repo, a document corpus or Obsidian vault, a shared folder of decks and protocols, a database, or a mix. Inventory the datasources, find and confirm the real source of truth, learn the structure, conventions, and vocabulary, reuse what prior work already mapped, and build a map you can act from — then stop. For orienting in an existing body of work before you start, not for checking your own finished work (that's verify-before-done) or investigating a research question to produce findings (that's research-guide).
display_name: Project Onboarding
icon: explore
color: "#89dceb"
tags:
  - onboarding
  - orientation
  - planning
---

# Project Onboarding

You've been dropped into an unfamiliar project and you don't yet know how it's
put together. A project here is a *bundle*: cloud files and shared folders (an
Obsidian vault, decks, meeting protocols, specs), databases, maybe a code repo —
and whatever prior work already learned about it. Code, if it's here at all, is
one part next to the rest.

Orientation fails in two opposite ways. You charge in on a structure you never
actually checked — and confidently do the wrong thing (this is the single largest
failure mode for autonomous agents). Or you read *everything*, map the whole
project, and never start. The job is to build a map you can act from, scoped to
what the task needs, and then **stop** — when you can write the map and a located
plan, you're done, not when you've read it all.

## The orientation

**1. Read what's already known — first.** The project knowledge base is the
accumulated onboarding of every prior job; so are the prior `notes/`,
`datasources.md`, and the memories injected each call. Search it
(`search_knowledge` / `get_knowledge_summary`) before re-deriving anything —
re-discovering what's already recorded is wasted budget. Reuse before rediscover.
*(But trust it only as far as step 3's freshness check allows.)*

**2. Inventory the datasources.** Find out what's actually attached, and what kind
of project that makes this — a document corpus? a database? a repo? a mix? Don't
assume it's a repo. Start from the datasource index, not a guess.

**3. Find each source's source of truth and conventions — and confirm it's the
real, current one.** The move differs by type:
- **Files / vault** — the index / README / MOC; the naming and folder conventions.
- **Database** — the schema and table relationships (`get_table_schema`,
  `list_tables`); the data dictionary is the README of a DB. Sample a few rows to
  confirm columns mean what they claim.
- **Repo** — the README, the entry point, the module map; **run the tests** to
  prove your model of it is right.
- **Provenance / freshness (every type)** — a vendored copy or a snapshot in a
  `documents/` folder can *differ* from the live source. Confirm which one you
  actually have before building on it; relevance is blind to staleness, and when a
  copy and the live source disagree, the live source wins.

**4. Learn the vocabulary and what "done" looks like.** The domain terms and
acronyms, the key entities, the stakeholders, and the actual target of *this*
task. Obscure terms rarely make sense until you've handled the material — so
capture them as you go.

**5. Don't assert what you didn't open.** Every claim about where something lives
names a path or handle you actually read this session, cross-checked against the
project's own index. A label is a hypothesis until you open the container. Mark
each map entry **confirmed** (you opened it) or **assumed** (you inferred it).

**6. Write the map back, then stop.** Record the map (`kb_write` or a `notes/`
file) so the next job inherits it — closing the loop with step 1. Orientation is
**strategic** work: its output feeds your first todo list. You're done when you
can write the map *and* a concrete, located plan — not when you've read
everything. Orient to the task, not the whole project.

## The project map

Fill this in as you go and write it back — it's a real artifact and it resets the
loop-detection counter:

```
# Project map — <project>
- Datasources:        <what's attached, by type>
- Source of truth:    <the canonical, current source per area — confirmed>
- Structure:          <the units + how they reference each other>
- Conventions:        <naming, layout, house style, where decisions live>
- Vocabulary:         <domain terms / acronyms / key entities>
- Open questions:     <what's still unknown>
- Confirmed vs assumed: <which entries you opened vs. inferred>
```

**You've oriented enough when:** the last source you read didn't change the map,
you can state the project in a few sentences, and you can write a located plan for
the task. That's the signal to stop and start.

## Don't

- **Assume it's a repo** — inventory first; code may be one part next to a vault and a database.
- **Act on a structure you never opened** — verify by opening; cite the path, cross-check the index.
- **Trust a snapshot as the source of truth** — check provenance and freshness; the live source wins a disagreement.
- **Re-derive what the knowledge base already holds** — read prior notes first; reuse before rediscover.
- **Orient forever** — stop when you can write the map and a located plan; read to task-sufficiency, not completeness.
- **Map the whole project for a corner-sized task** — scope orientation to what the task actually touches.
