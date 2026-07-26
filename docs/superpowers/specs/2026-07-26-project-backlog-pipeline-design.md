# Project Backlog — Design (the idea pipeline)

- **Date:** 2026-07-26
- **Status:** Approved (design); v1 is **sequential** — no worker pool, no concurrency
- **Author:** Brainstorm pairing
- **Roadmap effect:** cuts Phase 2 (cycles) of `docs/features/loop_unified_engine.md`; becomes the next slice ahead of Phase 3 (handover briefs); Phase 4 (overlap) stays superseded by the eventual worker pool.

## Problem

**The project loop's backlog is a fiction.** Every loop kickoff instructs the agent:

> "BEFORE you act: restate the goal in one line, then check the KB for (a) what's already done, (b) what's been TRIED AND REJECTED (do not re-propose it), and (c) **the current open backlog**."
> — `orchestrator/services/project_loops.py:709-711`

There is no backlog. No table, no note type, no list. Each agent re-derives one by similarity-searching a note store, every job, from scratch. The consequences are the ones you would predict: good ideas evaporate between iterations, rejected approaches get re-proposed, and nothing accumulates into a queue anyone can see.

This is the same defect class as the campaign disposition hole fixed on 2026-07-25 (commit `25719cf8`): **control state that lives only in prose the engine cannot read.** There the critic's verdict went into a KB note while the campaign sat parked in `review`. Here the work queue is narrated rather than stored. One level up, same shape.

**Steering is equally crude.** The only user input channel into a running loop is `project_loops.user_prompt` — one free-text string appended to every kickoff. It cannot express "do the deployment story next, then go back to what you were doing", it has no lifecycle, and it says the same thing to every agent forever.

## Goal

A machine you feed at the top and harvest at the bottom. Ideas and requirements go in — from the user, or from scholars foraging freely. They become tickets in a visible pool. The critic acts as **overseer**: it reads the queue, picks what is worth doing, and runs it. Developers build, QA tests and files what it finds back into the pool. Features come out. The user watches two buckets and reorders them when they care.

## Grounding — what already exists (verified 2026-07-26)

**The note format is already right.**

- OKF notes are markdown with YAML frontmatter, dual-written to `knowledge/<slug>.md` in the project's jobs repo **and** indexed into `knowledge_index` (pgvector). Renderer: `src/tools/knowledge/knowledge_tools.py:388` (`_render_note_md`). Frontmatter carries `id`, `type`, `description`, `tags`, `keywords`, `confidence`, `status`, provenance (`author`/`job`/`branch`), `created`/`modified`, `superseded_by`. Standard markdown links, never wikilinks. Required keys are `id`, `type`, `description` (`src/tools/knowledge/gardener.py:31`).
- **Status vocabulary already suffices for tickets:** `CHECK (status IN ('active','resolved','superseded','archived'))` — `orchestrator/database/vector_schema.sql:495`.
- **`note_type` is CHECK-constrained** to `goal, plan, decision, learning, code, source, question, state, retrospective, datasource` (`vector_schema.sql:491`). There is an in-file precedent block for extending it — that is how `datasource` was added (`vector_schema.sql:498-507`).
- **TTL is per note-type and unknown types never expire.** `KB_TTL_BY_NOTE_TYPE` (`src/services/knowledge_store.py:55`) with `KB_TTL_DEFAULT = None` and the comment *"Unknown types default to None (conservative: never auto-expire something we don't recognise)"*. New ticket types inherit immortality by default — the backlog cannot silently rot.
- **The hot query is already indexed:** `idx_knowledge_project_type`, `idx_knowledge_project_status`, and a GIN index on `tags` (`vector_schema.sql:509-512`).

**The agent tooling is already there.** No new tools are needed, which matters — extending an existing tool beats adding one, because every tool line costs context in every job.

- `kb_write(title, type, content, description, tags, keywords, confidence, links, …)` — `knowledge_tools.py:895`
- `kb_update(note_id, content|append, status, add_tags)` — `knowledge_tools.py:1116`; already performs status transitions
- `kb_list(type, tag, status, job_id, kb)` — `knowledge_tools.py:1259`
- `NoteTypeValue` literal at `knowledge_tools.py:60`; `NoteStatusValue` at `:72`

**A campaign is already a one-slot "in progress" bucket.** `project_loops.campaign` (JSONB) holds `initiative_note_id` — *a pointer to a KB note* — plus `title`, `stages`, `acceptance`, `cursor`, `stages_done`, `status` (`active`/`review`/`aborted`), `extensions_used`. Closure appends to `campaign_history`. Dispose-only filing (close without opening a successor) shipped 2026-07-25.

**What does not exist:**

- No `priority` anywhere — not in frontmatter, not in `knowledge_index`.
- **No cloud sync of `knowledge/`.** The notes live only in the jobs repo. Obsidian editing is *format-compatible* but not *plumbed*.
- **No KB view in cockpit at all.** `cockpit/src/app/views/project-detail/` contains only `project-detail.component.ts` and `project-loop.component.ts`.
- Budget stays job-counted; the cycles rename (unified-engine Phase 2) is cut.

## Design

### 1. A ticket is an OKF note with a work type

| Concern | Decision |
| --- | --- |
| Type | New `note_type` values `feature`, `issue`, `idea` |
| In the pool | `status = 'active'` |
| Done | `status = 'resolved'` |
| Dropped / rejected | `status = 'archived'` |
| Folded into another ticket | `status = 'superseded'` (+ existing `superseded_by`) |
| Priority | New frontmatter property `priority: high\|normal\|low`, default `normal` |
| TTL | None — tickets never auto-expire |

**Priority is a label, not a contract.** Nothing in the engine enforces it. It orders the list agents are shown; the overseer may pick anything it likes and owes no justification. If the user wants something done *now*, they schedule a job directly — that path already exists and is unambiguous.

Priority is a **property rather than a tag** for two reasons: `kb_update` can add tags but not remove them, so a tag-based priority would accumulate contradictory labels across a ticket's life; and Obsidian sorts and filters on properties, so the eventual editor workflow works without a translation layer.

Storage: the human-facing frontmatter value is the word; `knowledge_index` carries an indexed `priority SMALLINT NOT NULL DEFAULT 1` rank (`0=high, 1=normal, 2=low`) so ordering is a plain index scan rather than a `CASE`. The word↔rank mapping lives in exactly one constant.

### 2. Two buckets

- **Pool** — `note_type IN ('feature','issue','idea') AND status = 'active'`, ordered by `priority` then `created`. A deterministic, indexed SQL listing. Explicitly **not** a vector search: the point of the feature is that the queue stops being something each agent guesses at.
- **In progress** — the loop's existing `campaign`, whose `initiative_note_id` is the ticket. In sequential v1 there is at most one.

**A ticket being worked on keeps `status: active`** — "in progress" is derived from the campaign, never written to the note (writing it would make the note authoritative, which §5 forbids). So the pool query **excludes the current campaign's `initiative_note_id`**, and the rendered list shows that ticket separately under an `IN PROGRESS` heading. One SQL predicate, no note write, and the queue never offers the overseer something already underway.

**The campaign engine does not change.** A ticket is simply a better-typed initiative note. Everything hardened over the past week — barrier, heal, disposition, history — carries over untouched.

### 3. Injection, not search

The orchestrator runs the pool query and **injects the rendered list into the kickoff**: id, type, priority, title; capped at the top 20 by priority with a count of the remainder. The fictional "check the KB for … the current open backlog" line is deleted.

This mirrors the handover-brief principle: the platform hands over state, the agent never re-derives it. Every loop role receives the list (a developer benefits from the context); only the critic carries selection duty.

### 4. Lifecycle

1. **Created** — by the user (cockpit) or an agent (`kb_write` with the new type). A freely foraging scholar files `idea` notes; QA files `issue` notes on failures. That is the funnel intake, and it closes the loop back on itself.
2. **Selected** — the overseer critic files a campaign whose `initiative.kb_note_id` is the ticket.
3. **Worked** — campaign stages run exactly as today.
4. **Closed** — the campaign's disposition decides the ticket's fate: `ship` → `resolved`; `kill` → `archived`, reason recorded in the disposition notes; **`extend` → no change**, the ticket stays `active` and out of the pool because the campaign continuing on the same initiative still owns it. This reuses the dispose-only filing shipped on 2026-07-25.
5. **Reprioritized** — the user edits the property; reindex picks it up; nothing in flight is disturbed.

### 5. Authority and drift

The **database is authoritative** for work state (`campaign` + `campaign_history`). The note's `status` is a **mirror**, written best-effort when a campaign closes.

The rule that keeps this out of the un-healable-state trap: **the engine never reads a note to decide what to do next.** Selection reads the pool (a query), execution reads the campaign (a row). If a mirror write fails it is logged, history still holds the truth, and the next disposition repairs the note. Work state that must survive a torn advance stays in DB rows where compare-and-swap works — a markdown file cannot do CAS, and re-litigating that was the entire cost of the torn-advance incident.

### 6. Surfaces

**v1 — cockpit.** A backlog panel on the project page: open tickets sorted by priority, create, change priority, close, and a clear marker on the one currently in progress. Note endpoints largely exist already (the MCP knowledge tools consume them); this needs a filtered listing endpoint plus the component.

**Fast-follow — Obsidian.** Requires a decision: either the user clones the jobs repo into a vault, or `knowledge/` syncs into the project's OpenCloud space. Out of v1.

### 7. Roles

- **Scholar** — default is unscoped foraging, filing findings as `idea` tickets. When the overseer wants something specific researched, that is a campaign stage carrying an instruction; no new mechanism.
- **Critic / overseer** — reads the injected pool, selects, files the campaign, disposes it.
- **Developer / QA** — work campaign stages; QA files `issue` tickets, feeding the funnel.

## Data model & code surface

- **Migration `vector/0013`** — extend the `valid_note_type` CHECK with `feature`, `issue`, `idea` (mirroring the drop-and-re-add block at `vector_schema.sql:498-507`, which is how `datasource` was added); add `priority SMALLINT NOT NULL DEFAULT 1`; add a partial index on `(project_id, status, priority)` restricted to the three ticket types. Regenerate `vector_schema_current.sql`.
- `src/tools/knowledge/knowledge_tools.py` — extend `NoteTypeValue`; add `priority` to `kb_write` and `kb_update`; render it in `_render_note_md`; surface it in `kb_list` output.
- `src/services/knowledge_store.py` — ingest/persist `priority`; add the three types to `KB_TTL_BY_NOTE_TYPE` as explicit `None` (documentation value, since the default already covers it).
- `orchestrator/services/project_loops.py` — pool-query helper; `BACKLOG` part in `build_loop_kickoff`; delete the fictional line at `:709-711`; role-block wording for scholar/critic/QA.
- `orchestrator/main.py` — mirror write on campaign disposition inside `_advance_planner_campaign`; filtered backlog listing endpoint for cockpit.
- `cockpit/` — `api.model.ts` ticket types; backlog panel component under `views/project-detail/`; i18n keys in both locales.

## Failure modes

| Situation | Behavior |
| --- | --- |
| Empty pool | Critic falls back to today's self-directed selection from the loop goal, and is instructed to file tickets. No wedge. |
| Mirror write fails | Logged; DB authoritative; repaired at the next disposition. |
| Ticket deleted mid-campaign | Campaign carries its own `title` and `acceptance`, so it continues; the mirror write no-ops. |
| Very large pool | Bounded injection (top 20 + remainder count) keeps kickoff size sane. |
| Duplicate ideas | v1 relies on the overseer seeing the whole list at once — already strictly better than similarity-guessing. Automatic dedup is out of scope. |
| Priority absent (legacy note) | Defaults to `normal` rank; no migration backfill needed beyond the column default. |

## Testing

- **Unit:** pool ordering (priority then age); priority round-trip through render → ingest → list; kickoff rendering for populated / empty / over-cap pools; disposition → mirror write, including the failure path; the migration's CHECK accepting new types and rejecting junk.
- **Cockpit:** component tests for the backlog panel (bare-`Injector.create` pattern — `TestBed.createComponent` cannot render these components).
- **Live (dev):** one loop run where a user-added `high` ticket is picked up at the next checkpoint, worked, and closed with the note flipping to `resolved`.

## Out of scope for v1

Resource pool and concurrent workers; multiple loops per project; the dispatcher scheduler mode; binding priority; subloops; ticket dependencies or epics; Obsidian/cloud sync; automatic dedup. None of these are blocked by this slice.

## Open questions

1. **Ticket acceptance criteria** — authored by the user on the ticket, or left to the critic when it files the campaign? v1 assumes the critic authors them (campaigns already carry `acceptance`); revisit if user-authored criteria turn out to matter.
2. **Do tickets belong in similarity search?** They are indexed like any note, so they will surface in `kb_search`. Assumed useful; revisit if it pollutes retrieval.
3. **Obsidian delivery mechanism** — jobs-repo checkout versus `knowledge/` cloud sync. Deferred with the fast-follow.

## Related

- `docs/features/loop_unified_engine.md` — Phase 2 cut, Phase 4 superseded by this direction
- `docs/features/loop_campaign_scheduling.md` — the execution mechanism a ticket is run through
- `docs/features/okf_knowledge_base.md` — note format and conventions
- `docs/features/kb_convergence_ttl_reverification.md` — the TTL machinery tickets opt out of
- `docs/features/project_self_improvement_loop.md` — the loop this feeds
