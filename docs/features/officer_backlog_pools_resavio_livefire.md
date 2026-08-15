---
tags:
  - feature
  - officers
  - backlog
  - rsi-loop
  - live-fire
status: in-progress
created: 2026-08-15
related:
  - "[[officer_backlog_pools]]"
  - "[[officer_post]]"
  - "[[officer_control_plane_post_implementation_audit]]"
---

# Resavio live-fire scorecard — officer backlog pools (O6 release)

The acceptance run for the officer/loop unification (B1–B6 + the audit
tranche), on the arena it was designed around: Better Resavio, project
`68137e29`, dev cluster. O6 — releasing the held Centurion through
`POST /api/projects/{id}/officer/release` — was reserved as the live-fire
for the whole operating-surface stack and is executed here.

**The design of this run:** the July 2026-07-09 audit of the Resavio loop
found six systemic failures. This feature is substantially a response to
four of them, which makes the run falsifiable: each finding maps to a
concrete check, and the officer's picks are predicted *in this document,
committed before his release*, so hindsight cannot soften the comparison.

## Setup

| | |
|---|---|
| Officer | thread `d67ee261-334a-4315-ab7f-b1e0e7ba8765`, held since 2026-08-01 15:10Z |
| Officer brain | `gpt-5.6-sol` (codex-proxy, 400k ctx) — unchanged from July |
| Workers | `MiniMax-M3` (endpoint, 524k ctx) on every pool |
| Roster (new) | `research 1× (researcher)` · `test 1× (tester)` · `build 1× (executor)`, all `backend: sandbox` |
| auto_pull | **OFF** — every dispatch is an explicit officer act we can compare against our own judgment; turning it on removes the observable |
| Dev tip | pushed tranche incl. migrations app 0161 + vector 0020, leader-gated backlog tick confirmed started 15:53:43Z |

Deviation from July: slot backend was `vm`; this run uses `sandbox`. The
user's "as last time" constraint was the models, which are kept exactly.
VM provisioning carries its own open hardfail issues (headscale latch,
golden cold-import, SSH-ready clone) that would confound the feature under
test. The old `line`/`heavy` roster is replaced wholesale (`slots: null`
then the new map) because slot removal by omission is a silent no-op
(BP-11).

Known confounds, accepted going in:

- The OC-03 read-surface fix (`e8d5aea6`) is committed locally but NOT on
  dev. Nothing in this run ends the officer thread outside the endpoints.
- The officer session will eventually compact; the tool-pairing 400 on
  compaction is an open issue unrelated to pools. A wedge after a long run
  is suspect #1 there, not here.
- A project loop ran iterations "1–12" on 08-06→08-08 while he was held.
  22 wake events are queued for him; coalescing should fold these into one
  or two sitreps, not 22 turns. (That is itself a check — see M-7.)

## Baseline: the four July findings under test

| # | July finding | What the feature claims | Falsifiable check |
|---|---|---|---|
| F1 | **Phantom steering** — session KB writes never reached the loop's project KB; the Legate believed steering was injected; the loop never saw it | Officer's KB binding is structural: exactly one writable project KB, enforced at attach | His tickets exist in project `68137e29` via `search_knowledge` against THAT id, carrying `ready`/`category:` tags — verified before any dispatch |
| F2 | **Self-inferred DoD** — the loop's past CLI bias became its own acceptance criteria | Category contracts travel WITH the ticket; the charter + Legate demo answer (web UI required) are pinned, not re-derived | Executor ticket's deliverable shape names the web-UI demo DoD, not a CLI surface |
| F3 | **Iteration-counter collision** — counter reset on restart; duplicate iter-N artifacts; critic burned effort disambiguating | One-shot ticket claims are keyed by note id + `ready_at` generation, not by counter | No double-dispatch on any ticket (partial unique index `uq_jobs_active_ticket_claim` holds); bonus: does he NOTICE the fresh 08-06 loop reused iter-1..12 over July's? |
| F4 | **QA starvation** — freshest qa-findings were 10 iterations stale; critic triaged on a stale pool | Pools have floors; `ready_depth`/`below_floor` are rendered on the card and in his sitrep | Tester pool shows a real depth; a tester ticket exists and dispatches; the card's `below_floor` matches reality |

Findings F5 (hallucinated org constraints as gates) and F6 (critic rubric
ceremony) are NOT addressed by this feature; if they recur, they are
recorded, not scored.

## Predictions — written 2026-08-15 ~18:40Z, before release

What we expect him to do with his first two or three turns, recorded so
the comparison is honest:

1. **P1 — his first substantive act is `bbce4bed`.** His own dispatched
   developer job (Hotel Rheinland receptionist acceptance) froze into
   `pending_review` on 08-02, one day into his hold. It is his review
   duty, it is in his queued events twice (`paused`, `pending_review`),
   and judging it was written into the O6 acceptance a week ago.
2. **P2 — the executor ticket points at web-UI demo readiness** (the
   Legate's delivered DoD: 2–3 real front-desk flows over kurort_engine,
   one-command demo start), not at a new CLI subcommand. This is the F2
   check with teeth: his last pre-hold act was asking exactly this
   question, and the answer is in his KB.
3. **P3 — the tester ticket exists and targets verification of the
   unattended 08-06→08-08 loop iterations' claims** ("completed" is a
   claim, not a fact — 12 jobs completed with nobody judging them).
4. **P4 — he notices the counter collision** (the fresh loop's iter-1..12
   over July's iter-1..38) in his sitrep or a KB note. Low confidence —
   scored as a bonus, not a failure.
5. **P5 — pool discipline holds mechanically**: one job per ticket, slots
   stamped, no dispatch while a pool is full, and no dispatch of any kind
   before the Legate's go (he is instructed to write tickets first, not
   dispatch).

## Procedure

1. PATCH the roster (clear, then category slots) while held — mirrors to
   the live thread; notices, not wakes.
2. `POST /api/projects/68137e29…/officer/release` as the Legate.
3. Wait out the drain (~20s tick): queued timer + 22 events coalesce into
   sitrep wake(s). Watch his pod take the turn.
4. Legate directive via his input lane: orient, SITREP, judge what needs
   judging, then file 2–3 backlog tickets — one researcher, one tester,
   one executor — tagged ready where genuinely dispatchable. **No
   dispatch yet.**
5. F1 check: `search_knowledge`/`list_knowledge_notes` against project
   `68137e29` for the tickets and tags. Only on pass: tell him to
   dispatch one ticket per pool.
6. Observe the three jobs end-to-end (MiniMax-M3, sandbox pods).

## Mechanical checks (fill live)

| # | Check | Result |
|---|---|---|
| M-1 | Release endpoint clears hold, drain fires, he wakes without respawn (pod is 15d old and Running) | |
| M-2 | Sitrep renders pool section: per-pool ready depth + below_floor | |
| M-3 | Tickets in project KB with `ready` + `category:` tags; officer-only tags survive only via officer provenance | |
| M-4 | Dispatch stamps `context.work_category`, `context.officer_slot`, `context.ticket_note_id`; kickoff carries the category block | |
| M-5 | One-shot claim: second dispatch attempt on a claimed ticket refuses (409/committed claim visible in `uq_jobs_active_ticket_claim`) | |
| M-6 | Ready consumed on dispatch; re-arm requires explicit officer act | |
| M-7 | 22 queued events coalesce into ≤3 turns, not 22 | |
| M-8 | Officer card (`GET /api/projects/{id}/officer`) shows kit with `ready_depth`/`below_floor` per pool and lineage-aware in_flight | |
| M-9 | Worker jobs run on MiniMax-M3 in sandbox pods; category contract visible in the worker's kickoff | |
| M-10 | No admission while pool full; capacity 409 names the roster | |

## Results

**Run log (UTC):**

- 16:24:57 PATCH `{"slots": null}` → 200, `applied_to_thread: true`. The
  wholesale clear worked; no `line`/`heavy` ghosts in the returned config.
- 16:25:05 PATCH category roster → 200. Thread projection verified
  byte-identical to the row.
- pre-release card: all three pools render `ready_depth: 0`,
  `below_floor: true`, `auto_pull: false` — computed through the tick's
  own eligibility path against the live vector DB (M-8 pre-check ✅).
- 16:25:46 release → 200, hold cleared (was `kind=maintenance` since
  08-01). **`notified: false`** — see finding LF-1.
- 16:25:46+ drain kick: 20 of 22 queued events flipped `sent` in one
  claim; sitrep turn (iter 417) began on his 15-day-old pod without a
  respawn (M-1 ✅ — durable path; live-notice half failed, LF-1).
- 16:27:05 Legate directive accepted mid-turn (`turn_id: 416`,
  `queue_depth: 1`). Directive deliberately does not name `bbce4bed`.
- 16:27:19 **P1 confirms unprompted**: his first substantive tool reads
  are `GET /api/jobs/bbce4bed…`, its repo `output/` listing, and its last
  commit — straight to his own `pending_review` job, before any ticket
  work.

**Findings:**

- **LF-1 — agents row stuck `offline` while the pod heartbeats 200.**
  `agents.status` for `ea8dd2ee` reads `offline` with `last_heartbeat`
  19–25 s old, continuously. The heartbeat handler writes the agent's
  self-reported status, so either the persistent agent self-reports a
  status that maps to offline, or an effective-status override keeps it
  sticky. Consequence: `_resolve_live_agent` refuses on
  `status='offline'` before probing, so **every live officer notice is
  silently dropped** (`release` returned `notified: false`), and the wake
  drain routed the sitrep through its DURABLE branch
  (`save_thread_message` + finish) rather than live injection — confirmed
  by the code path and the `sent` flips with no inject log. Two knock-on
  effects while the row is stale: sitreps reach a LIVE officer only at
  his next turn trigger (input or the ~2h agent-local backstop) instead
  of immediately, and the sitrep watermark deliberately does not advance
  on durable delivery, so fingerprints re-diff until a live delivery
  lands. Degraded-but-functional; the run continued through it. Needs
  its own issue.
- Codex streaming quirk observed twice in iter 417: "Streaming produced
  tool calls with empty args — retrying with ainvoke", retry succeeded
  (second instance logged empty content with reasoning_content present).
  Known shape, non-fatal, no action.
- **LF-2 — a deploy leaves a live officer on pre-deploy agent code
  indefinitely.** His dedicated pod (`persistent-d67ee261-334`, 15 d old)
  is not deployment-managed, so the 15:52 rollout recycled every pool
  agent but not him. Consequences observed in one turn: (a) he runs the
  pre-B5 persona, which never taught the `category:` machine-tag grammar
  — his tickets carry bare `tester`/`researcher`/`executor` tags that
  `classify_ticket` cannot see, so the card honestly reports
  `ready_depth: 0` against a ready ticket he honestly filed; (b) he runs
  pre-B2 kb tools, which cannot stamp `ready_at` — his `ready` tag
  persisted with `ready_at` NULL, which fails closed exactly as designed
  (`eligible_tickets`: "ready tag with no ready_at"). The officer and the
  machinery disagree about the backlog and BOTH are being honest. My
  release directive also said "tagged with its category" loosely, which
  compounded (a) — but the persona was supposed to be the teacher, and
  his pod never got it. Mid-run remedy: pod recycle → watchdog respawn
  onto the current image (the ops-realistic fix, and a live test of the
  respawn-continuity design). Filed as its own issue.
- His turn-1 quality, for the record: cancelled his own `pending_review`
  job rather than rubber-stamping it, preserved the candidate by commit
  (`0140f70`) and branch, and rebuilt its acceptance as an INDEPENDENT
  tester verification — with the executor promotion gated on that test
  going green, research parallel, and only the genuinely dispatchable
  ticket marked ready. The dependency chain is exactly what the category
  model wants. "Legatus" spelling in his sleep reason — his charter
  predates the rename; historical artifacts, as documented.

## Verdict

*(fill at the end: which of F1–F4 the feature demonstrably fixed on its
home arena, which predictions held, what broke)*
