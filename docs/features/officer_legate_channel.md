---
tags:
  - feature
  - architecture
  - officers
  - communication
  - mcp
  - tooling
status: implemented-live-gate-owed
created: 2026-08-17
aliases:
  - legate channel
  - officer note
  - legate note
  - officer mcp surface
  - list_officers
  - send_officer_note
related:
  - "[[centurion]]"
  - "[[officer_post]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
  - "[[officer_knowledge_plane]]"
  - "[[unified_orchestrator_tool_surface]]"
  - "[[session_wake_on_job_completion]]"
---

# The Legate channel — seeing and directing an officer from outside the cockpit

> [[officer_supervision_surface]] gave the officer trustworthy eyes on his workers.
> [[officer_message_routing]] gave a blocked worker a way into his chain of command. This
> is the third edge and the one that was missing: the **Legate's** own view of his
> officers, and a way to give one direction from a client that is not the cockpit. Three
> MCP tools — a roster, a post read, and a one-way note — plus a tail read for long logs.
> No ask-and-wait, no lifecycle control: the cockpit keeps commissioning.

## Status

**IMPLEMENTED 2026-08-17** on develop. Backend: `POST /api/projects/{id}/officer/note`
and `GET /api/officers` in `orchestrator/main.py` (beside the other officer endpoints —
FastAPI 0.139 no longer flattens `include_router` and capability introspection would drop
a router), `deliver_officer_note` in `services/session_wake.py`, the verbatim Legate
section in `services/sitrep.py`, `list_project_officer_posts` in `database/postgres.py`.
Surface: three tools in `orchestrator/mcp/server.py` with their contracts in
`capabilities.py`, schema revision **10 → 11**, client + formatters in
`src/shared/orch_surface/`. **Live gate on dev is owed** — see [Acceptance](#acceptance).

## 1. Why

An officer is only reachable through the cockpit: a conference wearing his identity, or
his log. Everything an assistant working alongside the Legate would need is missing from
the MCP, and the gaps compound.

- `list_persistent_threads` does not say a thread is an officer, whose post he holds, or
  whether he is held. Across ~50 projects, finding him is guesswork.
- `get_persistent_thread` truncates his config and exposes no officer state — no next
  wake, no kit utilization, no pages, no digest.
- `get_persistent_thread_messages` pages from the OLDEST row. Reaching turn 113 of a
  590-message log meant computing `offset=576`.
- There is **no user→session REST path at all**. Cockpit chat is WebSocket-only and
  `POST /api/agents/threads/{id}/messages` is the agent writing its own history. An MCP
  caller could not say one word to a running officer.

Two facts kept this small. `GET /api/projects/{id}/officer` already computes the whole
post card, and the messages endpoint already has a `before=<ts>` backfill cursor. The read
half needed no new backend.

## 2. The note and how it lands

A note is direction from the Legate, delivered one way. `deliver_officer_note` reuses the
live→durable split the job-completion wake already uses, and **returns which of the three
happened** — a note reported as sent when it only reached a table is worse than an error.

| Officer state | Path | Result |
|---|---|---|
| Live pod, no conference hold | `_inject_live` → `/api/input` as `role='event'` | `live` |
| No live loop (503 / no pod) | durable `legate` outbox row + drain kick | `queued` |
| Conference hold | outbox row only, no live attempt | `held` |
| Maintenance hold, live pod | `_inject_live` (see below) | `live` |
| Maintenance hold, no pod | outbox row; the claim SQL skips held threads | `held` |

**Holds are not alike, and the row shows which is which.** A conference hold carries the
conference `thread_id`; the meeting is the single writer ([[centurion]] §2), so a note
queues and arrives with the brief wake. A maintenance hold carries no `thread_id`, and its
own notice promises "Legate messages still reach you" — the same rule
`_inject_officer_notice` states — so the live path stays open. `held` is deliberately not
called `queued`: the drain will not deliver it until the hold lifts, and saying "queued"
would promise a wake that is not coming.

Each note carries a fresh `dedup_key`, so two directives a minute apart are two
directives. `OFFICER_DEBOUNCE_BY_SOURCE["legate"] = 0` is explicit even though unlisted
sources already default to 0 — a future default must not silently swallow one.

**The sitrep renders notes verbatim, first, before the wake reasons.** The generic reason
renderer truncates a payload summary to 160 chars; routing a directive through it would
amputate it on the queued path. `_legate_lines` caps at 4000 chars only to stop a
pathological paste from crowding out the briefing, and says so when it trims.

**Provenance is part of the message.** The endpoint stamps
`[Legate note — <name> via MCP, <ts>]` ahead of the body. He treats a Legate directive as
top authority, so a note composed by an assistant holding the Legate's credentials must
not read as words the Legate typed himself.

**Owner-gated.** `require_project_owner`, the same bar as hold/release: a note carries
command authority. A vacant post is a **409** with the reason, never a silent success.

## 3. The reads

`GET /api/officers` starts from `project_officers`, so a **vacant** post is still a row —
"does this project have an officer" is part of the roster, not an absence to infer. It is
scoped by `user_visible_project_ids` (admin `"all"` sentinel, MCP `project:<uuid>` scope
narrowing) and is deliberately a plain read: the per-project card runs
`get_or_create_project_officer`, and fanning that across a user's projects would commission
posts as a side effect of looking. Per-slot kit utilization stays on the card, which
computes it lineage-aware; the roster carries one project-level in-flight count using the
admission funnel's own terminal-status list.

`get_project_officer` renders the card and, when the post is filled, pulls the tail of his
log so the answer shows what he has been doing rather than only how he is configured. A
failed tail read costs the log section, not the whole answer.

`newest_first` on `get_persistent_thread_messages` switches the existing endpoint to its
`before` cursor and drops the offset-paging footer, which would otherwise send a reader
walking forward from turn one.

## 4. Surface

| Tool | REST | Auth |
|---|---|---|
| `list_officers()` | `GET /api/officers` | approved user, visibility-scoped |
| `get_project_officer(project_id, recent=10)` | card + thread-message tail | project viewer |
| `send_officer_note(project_id, message)` | `POST .../officer/note` | project **owner** |
| `get_persistent_thread_messages(..., newest_first)` | existing route, `before` cursor | thread owner |

Out of scope by decision: ask-and-wait reply correlation (his answer surfaces in his log,
digest, or a page); commission/decommission/hold/release/patch over MCP; any change to the
officer's own 28-tool surface.

## 5. Tests

- `tests/test_officer_legate_note.py` — delivery for every officer state, hold-kind split,
  non-coalescing keys, owner gate, vacant 409, message validation, provenance, routes wired.
- `tests/test_officer_roster.py` — roster view logic, scoping, admin sentinel, and that the
  read never creates a post.
- `tests/test_officer_roster_real_postgres.py` — the join against a real server: vacant post
  survives the LEFT JOIN, wake counts do not leak across threads, in-flight follows the
  project, `last_agent_activity` ignores orchestrator-written event rows.
- `tests/test_officer_sitrep.py::TestLegateNotes` — verbatim, leading, not duplicated as a
  reason.
- `tests/test_officer_mcp_surface.py` / `tests/test_officer_mcp_tools.py` — client request
  contracts, honest rendering, tail wiring, and that a broken tail read never costs the post.

## 6. Acceptance

Live gate on dev, against the Better Resavio post (project `a572e4a0…`, officer
`6ce5bc4c…`), after confirming the **running pods** carry the new tag for both the
orchestrator and the MCP image (`server.py` ships in its own):

1. `list_officers()` shows the post, its next wake, and jobs in flight.
2. `get_project_officer(...)` shows kit, pages, digest, and a recent-log block.
3. `send_officer_note(...)` returns `live` or `queued` with `next_wake_at`.
4. `get_persistent_thread_messages(..., newest_first=true)` shows the note as an event row
   carrying its provenance label, and his next turn acts on it.
5. Negatives: vacant project → 409; non-owned project → 403; two notes in quick succession
   both delivered.

Record the outcome here when it runs.
