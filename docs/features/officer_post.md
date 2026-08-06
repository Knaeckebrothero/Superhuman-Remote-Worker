---
tags:
  - feature
  - architecture
  - orchestration
  - sessions
  - database
status: proposed
created: 2026-08-06
aliases:
  - officer post
  - project_officers
  - commission
  - decommission
related:
  - "[[centurion]]"
  - "[[centurion_implementation_notes]]"
  - "[[db_migration]]"
  - "[[loop_unified_engine]]"
---

# The officer's post — `project_officers` and the commission lifecycle

> Every century has a **post**, whether or not an officer currently holds it. This doc
> moves the officer's durable identity out of thread metadata and into a
> `project_officers` row — one per project, always present, `thread_id IS NULL` when the
> post is vacant. **Commission** raises an officer onto the post; **decommission** ends
> his thread but keeps everything he had — kit, budgets, brain, digest, page counter,
> sitrep fingerprints; **recommission** brings him back with all of it via a continuity
> brief. The thread stays the *runtime projection*; the row becomes the *durable record*.
> Along the way this ships the two things the Legate has asked for since the first live
> command: seeing how much of his kit is actually in use, and adjusting it while he is
> on duty.

## 1. Motivation — four defects with one root

A month of Centurion v1 (centurion.md §11) plus the first live command surfaced four
gaps that are all the same gap — *the officer's existence is an inference, not a record*:

`get_officer_thread_for_project` (`postgres.py:6186`) defines an officer as
`status != 'ended' AND metadata.config_override.officer.enabled = 'true'`, newest first.
Everything durable about him — slot roster, sleep bounds, page budget, token ceiling,
brain override, and `metadata.officer_state` (digest ring, page counter, **sitrep
fingerprints**) — hangs off that one thread row. Consequences, each observed live on
the Resavio command (`d67ee261`):

1. **Retirement strands his state.** The DELETE path flips `enabled=false` and soft-ends
   the thread (`main.py:26785`). Nothing is destroyed, but nothing reads it back either:
   no endpoint or UI shows a retired officer's kit, and there is **no recommission
   path** — `resume_thread` restores metadata but the create funnel is the only writer
   of `enabled=true`. Bringing him back means a new thread, a re-typed kit, an empty
   digest, and — worst — **zero sitrep fingerprints**, so his first watch re-reports
   every job in the project as news (Resavio: 200 fingerprints, 242 jobs).
2. **No live adjustment.** The slot-roster builder exists only in the provision branch of
   the Centurion tab. Changing a live officer's kit today = retire + re-provision
   (defect 1), or hand-editing JSONB in Postgres (done twice on dev: the re-brain, the
   maintenance hold).
3. **Allocation is visible, utilization is not.** The card shows `line ×2` (what he
   *may* run), never `line 1/2` (what he *is* running) — even though the number is
   already computed twice, at dispatch admission (`main.py:9852`) and per wake in
   `sitrep._capacity_section` (`sitrep.py:390`). The officer sees his own capacity every
   wake; the Legate never does.
4. **Ambiguity by construction.** "Newest non-ended enabled thread" answers *which of
   these ended officer threads was the real one* with `ORDER BY created_at DESC` and
   hope. Wake events, loops (`scheduling='officer'`), conference holds and the watchdog
   all key off this inference.

The 2026-08-01 maintenance hold on `d67ee261` (see §6) is the live bridge: he is paused,
state intact, waiting for this migration so he can be the backfill's first adoption.

## 2. The model

```
project_officers (one row per project, created with the project)
    │
    ├── thread_id IS NULL      → post VACANT (the default for every project)
    │     row holds: kit, budgets, brain, harvested state, incarnation history
    │
    └── thread_id → threads.id → post COMMISSIONED
          the thread carries the runtime projection (config_override.officer)
          and the LIVE officer_state; the row is stamped at transitions
          │
          └── officer.hold set  → HELD (commissioned, standing down)
```

Division of authority, one line each:

- **The row is the durable record.** Kit, budgets, brain, sleep bounds, harvested
  `officer_state`, incarnation log. Survives everything short of project deletion.
- **The thread stays the runtime projection.** `officer.enabled` is load-bearing in ~10
  places — the wake-claim and watchdog JSONB predicates (`postgres.py`), the drain, the
  dispatch admission, and the agent-side `config.officer` (`loader.py`) — **none of
  which change**. Commission stamps the row into thread metadata exactly the way the
  create path does today; the hot paths keep reading what they read.
- **Writers per direction, transitions only.** Row → thread at commission and on a
  PATCH (§7). Thread → row at decommission (state harvest). While commissioned, live
  `officer_state` writes (`merge_thread_officer_state`, `postgres.py:6301`) keep
  targeting the thread; the row is not double-written.

The metaphor earns its keep in the UI: the Centurion tab stops flip-flopping between a
provision form and a read-only card. There is one card — the post — with one state.

## 3. Schema — migration `0087_project_officers.sql`

New table, so the squawk three-file split does not apply; one transactional migration
(backfill included), then **regenerate `schema_current.sql`** (`scripts/schema-snapshot.sh`).

```sql
CREATE TABLE project_officers (
    -- 1:1 by construction — the project IS the key. No surrogate id.
    project_id      UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    -- Current incarnation; NULL = vacant. Deliberately NOT unique: the legion
    -- (centurion.md §9 — one Primus Pilus commanding several centuries) becomes
    -- "several rows share a thread_id" instead of a schema change.
    thread_id       UUID REFERENCES threads(id) ON DELETE SET NULL,
    -- The provision fragment, verbatim as the create funnel receives it today:
    -- {officer: {slots, sleep_*, max_*, daily_token_ceiling}, llm: {model,
    -- reasoning_level}, workspace: {backend}, interactive: {permission_mode}}.
    -- Runtime keys (officer.hold, officer.last_respawn_at) are stripped on write.
    config_override JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Harvested officer_state (digest ring, pages, sitrep fingerprints) from the
    -- last decommission, plus the while-vacant ledger (§5). Empty while
    -- commissioned — the live copy is on the thread.
    state           JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Append-only: [{thread_id, commissioned_at, decommissioned_at, reason}].
    -- Old threads stay readable as ended sessions; this is the index into them.
    incarnations    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT project_officer_config_is_object CHECK (jsonb_typeof(config_override) = 'object'),
    CONSTRAINT project_officer_state_is_object  CHECK (jsonb_typeof(state) = 'object'),
    CONSTRAINT project_officer_incarnations_is_array CHECK (jsonb_typeof(incarnations) = 'array')
);
```

**Backfill (same migration, pure SQL — no Python-side JSONB parsing, which dodges the
asyncpg string-vs-dict trap that litters `main.py`):**

1. `INSERT INTO project_officers (project_id) SELECT id FROM projects` — every existing
   project gets its post, vacant.
2. **Adopt live officers.** For each project's newest non-ended thread with
   `officer.enabled='true'` (`DISTINCT ON`): set `thread_id`, harvest
   `config_override` minus `officer.hold` / `officer.last_respawn_at` (`#-` path
   deletes). The thread itself is untouched — **`d67ee261` is adopted mid-hold, and the
   hold stays on the thread where it lives**. Zero wakes fire; he is held.
3. **Fold retired officers into history.** For projects with only *ended* officer
   threads (k3d smokes, past retirements): harvest config + `officer_state` into the
   row, append an incarnation entry, leave `thread_id` NULL. This is what makes the
   provision form seed from the last real kit instead of the hardcoded
   `line ×2 · sandbox` draft.

`create_project` (`postgres.py:13345`) gains the same INSERT, and the row helpers use a
get-or-create read so a project minted by any bypassing path self-heals its post.

## 4. Read paths — what flips, what deliberately does not

**Flips to the row** (these need "*the* officer of project X"):

| Site | Today | After |
|---|---|---|
| `get_officer_thread_for_project` (`postgres.py:6186`) | JSONB predicate + `ORDER BY … LIMIT 1` | join through `project_officers.thread_id`; NULL = no officer, no ambiguity |
| `GET /api/projects/{id}/officer` (`main.py:22924`) | 404-shaped `officer: null` when uninferable | always returns the post (§8 response shape) |
| `routers/project_loops.py` officer checks | "enabled centurion exists" | "post is commissioned" (`thread_id IS NOT NULL`) |
| Conference hold stamping (`_hold_officer_for_conference`) | via the old lookup | via the row (transitively — it calls the flipped function) |

**Stays on thread metadata** (runtime projection, per §2): the wake-claim query's
enabled/hold predicates, `list_officer_threads` (watchdog + fleet sweeps iterate *live
threads*, which is exactly what they mean), the dispatch-admission hold fence, the
agent-side `OfficerConfig`. Blast radius of the flip is four call sites, not fourteen.

**Consistency by construction:** the create-thread funnel is today the only writer of
`officer.enabled=true` (`_validated_session_officer_override`, `main.py:4057`). It
gains one step: registering the new thread on the project's post — and returning 409
when the post is already commissioned. The commission endpoint (§5) goes *through* this
funnel, so there is exactly one path that raises an officer, and the row can never
disagree with the threads table about who holds the post. Our own dev provisioning
recipe (in-pod curl `POST /persistent/threads`) keeps working and lands registered.

**Capacity across incarnations** — a real edge the row fixes: admission and
`_capacity_section` count in-flight jobs by `created_by_thread_id = <current thread>`.
A job left running across a decommission→recommission (the design *encourages* leaving
them) belongs to the *prior* thread and would silently stop counting against the kit.
Both queries switch to the post's thread lineage — `created_by_thread_id = ANY(current
+ incarnations[].thread_id)` — which the row makes available for the first time. Job
rows and their `context.officer_slot` stamps are untouched.

## 5. Lifecycle transitions

### Commission — `POST /api/projects/{id}/officer/commission`

Body: optional partial config (same fields as §7's PATCH; validated, merged into the
row first). Then:

1. If `thread_id` points at an *ended* thread (crash-ended incarnation under the v1
   page-and-wait policy), fold it first: harvest state, append the incarnation entry,
   unlink. Commission always starts from a clean link.
2. Create the thread through the existing funnel with the row's `config_override` and
   title `Centurion — <project>`; registration (§4) links it. Boot follows the normal
   attach path — nothing new.
3. Enqueue the **continuity brief** as his first wake:
   `enqueue_session_wake_event(thread, source='commission', dedup_key=<thread_id>)` —
   the source column is deliberately unconstrained (`0074` migration comment), and the
   dedup key makes retries coalesce. Payload: vacant-since/until, a pointer at his last
   state note and the charter (which he re-reads anyway — identity lives in the project
   stores, centurion.md §5), and the **while-vacant ledger** below.

**Recommission is a new incarnation, not `resume_thread` — a decided design point.**
Resuming the old thread replays a landmine: pending `session_wake_events` are excluded
only by `t.status != 'ended'`, so every event queued during the vacancy would fire into
his first turn — completions from weeks ago, fleet notices from another era. It also
fights the 2026-07-28 decision that identity lives in charter + KB + RecallStore rather
than the context window. He comes back with everything that *matters* — kit, budgets,
digest, fingerprints, the brief — and none of the stale queue. The old log stays one
click away via `incarnations`.

### Decommission — `POST /api/projects/{id}/officer/decommission`

Replaces the bare thread-DELETE with actual hygiene, in order:

1. **Warn on in-flight jobs** (unless `force`): the response lists them; the default is
   *leave them running* — a pause must not kill his work. Their completions reach the
   post regardless: `notify_officer` resolves the officer via the project, so a later
   incarnation gets the wake, and a vacant post records it in the ledger.
2. **Harvest** `metadata.officer_state` → `row.state`, whole (digest, pages,
   fingerprints — the fingerprints are the point).
3. **Fold + clear the queue.** Pending wake events for the thread: job-shaped ones
   append to `row.state.while_vacant` (ring, cap 20, drop-oldest with a dropped-count);
   the rest (timer, fleet) are deleted — a sleep timer for an absent officer is
   meaningless.
4. End the thread via the existing `end_thread` flow (enabled-flip, workspace snapshot,
   pod teardown all unchanged). The officer branch there (`main.py:26785`) *becomes*
   this step, so a direct DELETE on an officer thread routes through decommission —
   again one funnel.
5. Unlink `thread_id`, append the incarnation entry `{thread_id, commissioned_at,
   decommissioned_at, reason}`.

While vacant, the `maybe_wake_session` officer leg appends terminal-status entries for
the project's jobs to the same ledger instead of dropping them — the commission brief
then opens with "while the post was vacant: …".

### Hold / release — `POST .../officer/hold`, `POST .../officer/release`

Productizes the manual 2026-08-01 pause, verified live for five days on `d67ee261`
(memory: `reference_officer_maintenance_hold_pause`). Hold stamps
`config_override.officer.hold = {kind: 'maintenance', since, note}` — **no
`thread_id`**, which is what keeps the watchdog's stale-hold self-heal
(`main.py:28663`) from ever releasing it. One key, four effects, all pre-wired by the
conference machinery: drain skips him, dispatches 409, watchdog stands down, nothing
self-heals. Both endpoints inject a best-effort one-line notice via the agent's
`/api/input` (which bypasses the hold *by design* — Legate input always reaches him).
Release costs nothing standing: queued events drain within one ~20s tick.

Hold is thread-scoped runtime state and never enters the row; a vacant post cannot be
held.

## 6. Migration of the live command — the standing fixture

`d67ee261` (Better Resavio, project `68137e29`) has been under a manual maintenance
hold since **2026-08-01 15:10Z** precisely so this migration can adopt him in place. As
of 2026-08-06: thread `active`, hold intact, 7 events queued durably (1 timer, 3 fleet,
3 job transitions), zero dispatches since the hold — and his job `bbce4bed` finished
into **`pending_review`** on 08-05, its completion wake sitting in the queue. The
backfill adopts him commissioned-and-held; the acceptance run (§10) releases him
*through the new endpoint* and expects him to coalesce the backlog and judge that job.
No state transfer step exists or is needed: adoption reads the same JSONB it leaves in
place.

## 7. Editing the post — `PATCH /api/projects/{id}/officer`

The missing form. Accepts a partial of: `slots` (validated by `validate_slots_spec` —
the same hard validation provision gets; a typo'd kit 400s), `max_concurrent_workers`,
`max_pages_per_day`, `max_actions_per_wake`, `daily_token_ceiling`,
`sleep_min_minutes`/`sleep_max_minutes` (min ≤ max), `brain` (`{model,
reasoning_level}`, vocabulary-checked like the create bridge). Writes the row; when
commissioned, deep-merges the fragment into thread metadata
(`merge_thread_config_override`) and injects a one-line notice. **Deliberately not a
wake** — burning a turn on "you have one fewer line slot" is the bureaucracy the P1
wave removed; the next sitrep's capacity line carries the truth.

Why not the existing thread-config PATCH: `_apply_thread_config_update` validates only
tools + datasources (a kit would bypass `validate_slots_spec` entirely), 409s whenever
an agent is bound — a live officer's permanent state — and its auth is thread-owner,
while the kit belongs to the century (project-admin, §11 open Q1).

Per-field honesty, shown in the UI:

| Fields | Effect |
|---|---|
| `slots`, `max_concurrent_workers` | next dispatch (admission re-reads the thread row per create) |
| `daily_token_ceiling`, `max_pages_per_day` | next delivery / next page (drain + notify read fresh) |
| `sleep_min/max_minutes` | next sleep filing (server-side clamp) + watchdog immediately |
| `max_actions_per_wake`, `brain` | **next respawn** — baked into `config.officer` at attach (`loader.py`); labeled so, not pretended live |

**Shrinking below in-flight is drain semantics, decided:** `admit()` computes
`free = count − in_flight`; dropping `line` 2→1 with 2 running makes free −1 — new
dispatches on the slot are refused with the existing actionable 409, running jobs are
untouched, capacity converges as they land. Rejecting the edit would be backwards (you
shrink *because* you want the bleeding stopped); cancelling running jobs from a form
save would be indefensible. The card shows "2 in flight — drains to 1".

## 8. The card — one component, one state

`GET .../officer` always returns the post:

```jsonc
{
  "commissioned": true,            // thread_id IS NOT NULL
  "held": {"kind": "maintenance", "since": "…", "note": "…"} | null,
  "officer": { /* current shape when commissioned: thread_id, status, model,
                 sleep bounds, next_wake_at, pending_events, pages_today,
                 token_ceiling, digest, conference */ },
  "kit": { "line": {"count": 2, "model": "MiniMax-M3", "backend": "vm",
                     "in_flight": 1 } },        // ← utilization, lineage-aware (§4)
  "spend_today": {"tokens": 1200000, "ceiling": 5000000},  // usage_ledger.query_usage,
                                                 // the ceiling brake's own call, reused
  "incarnations": [ {"thread_id": "…", "commissioned_at": "…",
                     "decommissioned_at": "…", "reason": "…"} ]
}
```

`project-officer.component.ts` collapses its two disjoint branches into one card:

- **Vacant:** the kit editor (today's provision form), seeded from the row (last real
  kit, else the starter draft) — plus the while-vacant ledger and past-incarnation
  links. Button: **Commission**.
- **Commissioned:** the *same* editor, populated live, with per-slot `1/2` utilization
  chips and today's spend against the ceiling; digest; Open log / Conference /
  **Hold** / Decommission (armed, warning on in-flight jobs).
- **Held:** badge with `hold.kind` + note — fixing the current hardcoded
  "held — conference in progress" label, which is wrong for maintenance holds.

## 9. Explicitly out of scope

- **Officer notes in the project repo — decided against (2026-08-01).** The jobs repo
  clones into every worker workspace (`repos/<slug>/`; the `.gitignore` floor governs
  *commits*, not *clones*), and a markdown KB folder re-opens the substrate decision
  the KB just settled toward Postgres — for the one actor who most needs write-time
  integrity, typed supersede semantics and retrieval (Resavio alone: 3,254 notes). What
  the repo *should* eventually carry is the `retros/` analogue: an orchestrator-written,
  merge-outcome-backed **per-watch decision record** — append-only history, fine for
  workers to read. Deferred, orthogonal to this table.
- Digest email sender, charter UI, suspended-conference re-hold (v1 leftovers,
  unchanged).
- A "respawn now" affordance for brain edits (the live re-brain recipe — stamp + pod
  delete → watchdog respawn — exists but stays manual; §11 Q3).
- The legion (multi-century command): the schema leaves the door open (no UNIQUE on
  `thread_id`), nothing more.

## 10. Acceptance (dev cluster, in order)

1. **Backfill:** every project has a post; `d67ee261` adopted commissioned + held, hold
   untouched, zero wakes fired by the migration itself; at least one previously retired
   officer's kit visible on its vacant post.
2. **Fresh post:** new project → vacant card; commission with a kit → thread boots,
   first wake carries the commission brief; registration 409s a rival direct create.
3. **Live edit:** shrink a slot below in-flight → 409 on next dispatch names `2/1`,
   running jobs unharmed; ceiling/pages edits visible to the drain without agent
   involvement; brain edit shows "next respawn".
4. **Hold/release from the card:** due timer stays `pending, attempts=0` past `fire_at`
   while held (proven manually 08-01); release drains within a tick.
5. **The standing fixture (§6):** release `d67ee261` through the new endpoint → he
   wakes once with the coalesced backlog and judges `bbce4bed` (`pending_review`).
6. **Decommission/recommission:** decommission with a job in flight (warns; leave it) →
   completion lands in the ledger → recommission → brief lists it, fingerprints
   restored (sitrep reports *deltas*, not 242 jobs of news), zero replayed events.
7. **Regression:** plain sessions, conferences, worker dispatch, officer loops
   (`scheduling='officer'` start/convert guards now read the row) all green.

## 11. Open questions (Legate)

1. **Edit/commission authority** — project `admin`, or thread-owner? Recommendation:
   admin; the kit belongs to the century, not to whoever clicked provision.
2. **Starter kit for never-commissioned posts** — keep the `line ×2 · sandbox` draft,
   or empty flat-cap? Recommendation: keep the draft; history overrides it wherever an
   incarnation ever existed.
3. **Brain-edit respawn** — offer `apply: respawn` on PATCH (pod delete → watchdog,
   ~7 min, dedicated pods only), or leave manual? Recommendation: defer; the honest
   "next respawn" label costs nothing.

## 12. Sequencing

| Slice | Contents | Gate |
|---|---|---|
| O1 | migration 0087 (table + backfill) + row helpers + `create_project` hook + snapshot regen | migration idempotent on re-run; adoption facts (§10.1) via psql |
| O2 | read flips (§4) + create-funnel registration + lineage-aware capacity | pytest on lookup/409/capacity; officer-loop guards green |
| O3 | commission / decommission / hold / release + ledger + brief + `end_thread` rerouting | §10.2/4/6 on k3d |
| O4 | PATCH + validation reuse + live merge + notice | §10.3 on k3d |
| O5 | cockpit card (one state machine, utilization, spend, incarnations, hold-kind label) | vitest + walk the card through all three states |
| O6 | dev acceptance: release the Resavio officer through the front door | §10.5 — he judges `bbce4bed` |

**Risks:** double-authority drift (mitigated by §2's writers-per-direction and
transition-only stamping — nothing double-writes steady-state); backfill JSONB edge
cases (pure-SQL backfill; `#-` on absent paths is a no-op); the standing trap of
editing an applied migration (never — repair forward); `schema_current.sql` regen is
mandatory and part of O1's definition of done.

## 13. Decision log

- **2026-08-01 (Legate):** posts are *always there* — one per project, default
  decommissioned; decommission keeps everything visible; recommission restores it. The
  triggering observation: retirement strands state that is "right there in the JSONB"
  with no reader.
- **2026-08-01 (Legate, on review):** officer knowledge stays in the Postgres KB; no
  officer folder in the jobs repo (clones into every worker; re-opens the substrate
  decision). Repo gets a retros-style decision record later, if anything.
- **2026-08-01 (joint):** pause ≠ retire. The Resavio officer was held live
  (`officer.hold`, kind=maintenance, no `thread_id` → no self-heal) rather than
  retired; his in-flight job deliberately left running. Both halves verified: he read
  the stand-down and refiled sleep; his due timer sat unclaimed across ~15 drain ticks.
- **2026-08-06 (design):** row = durable record, thread = runtime projection, writers
  per direction; recommission = new incarnation + continuity brief (wake-replay
  landmine + identity-outside-the-window); shrink-below-in-flight = drain semantics;
  kit edits don't wake him; capacity counts follow the post's thread lineage.
