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
| M-1 | Release endpoint clears hold, drain fires, he wakes without respawn (pod is 15d old and Running) | ✅ durable path (live half blocked by LF-1; cleared after respawn) |
| M-2 | Sitrep renders pool section: per-pool ready depth + below_floor | ✅ card + his own reports track it |
| M-3 | Tickets in project KB with `ready` + `category:` tags; officer-only tags survive only via officer provenance | ✅ after LF-2 remedy + grammar correction; `ready_at` stamped on both explicit re-adds (16:39:16, 16:52:04) |
| M-4 | Dispatch stamps `context.work_category`, `context.officer_slot`, `context.ticket_note_id`; kickoff carries the category block | ✅ category/slot/kickoff on both jobs; `ticket_note_id` ❌ until LF-3 deploys (operator-repaired both times) |
| M-5 | One-shot claim: second dispatch attempt on a claimed ticket refuses | ◐ index verified present; claim-vs-eligibility proven via ready_depth flip; dup-probe deliberately skipped (admission rewrite in flight in a concurrent session) |
| M-6 | Ready consumed on dispatch; re-arm requires explicit officer act | ✅ claimed tickets left every eligibility view; his explicit re-stamps moved `ready_at` |
| M-7 | 22 queued events coalesce into ≤3 turns, not 22 | ✅ one turn |
| M-8 | Officer card shows kit with `ready_depth`/`below_floor` per pool and lineage-aware in_flight | ✅ live throughout, honest at every state change |
| M-9 | Worker jobs run on MiniMax-M3 in sandbox pods; category contract visible in the worker's kickoff | ✅ tester ("output is ISSUE TICKETS") + researcher ("deliverable is an ANSWER… SPIKE"); officer's 4-field brief became 4 predefined strategic todos |
| M-10 | No admission while pool full; capacity 409 names the roster | not exercised (no pool reached full+1 this session) |

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

- 16:28:58 he **cancelled** `bbce4bed` rather than approving it,
  preserved the candidate by commit+branch, and rebuilt its acceptance as
  an independent tester ticket. P1 ✅ (unprompted). P2 ✅ (executor ticket
  = promote the accepted WEB UI demo; no CLI shape anywhere). P3 partial
  (tester ticket verifies his own preserved candidate, not the 12
  unattended loop iterations). P5 ✅ so far (no dispatch, ready only where
  dispatchable).
- 16:29:13 turn 417 complete — 19 tool calls covering judge + 4 KB writes
  + sleep(30). M-7 ✅: 22 queued events coalesced into ONE turn.
- 16:33:16 stale pod deleted (LF-2 remedy). Durable sleep timer
  (fire_at 16:59) survived deletion — external-timer design ✅.
- 16:36:54 watchdog respawn onto `sha-80cfc57` (the deploy's agent
  image), attach 16:37:03, message restore 1.97 s, officer capability
  ceiling dropped 3 tools on boot (K-plane ✅). 16:37:26 boot turn ends
  in sleep: "Reoriented after restart; category backlog is filed, no
  workers are dispatched, and I am awaiting the Legate's confirmation."
  **Respawn continuity ✅ — ~3.5 min pod-death-to-reoriented, identity
  from charter+KB, not the window.**
- 16:38:51 Legate correction sent: category: grammar + ready re-stamp +
  dispatch the acceptance ticket into the test pool.
- 16:39:16 correction turn (419, 8 tool calls): all three tickets retagged
  with `category:` forms (human labels kept), `ready_at` stamped
  16:39:16.49 by the NEW kb tools on his explicit ready re-add — **B2
  authorization works when the code is present** (M-3 ✅).
- 16:39:33 dispatch: job `660a8eec`, `work_category: tester`,
  `officer_slot: test`, MiniMax-M3, kickoff opens with the tester category
  block ("Your output is ISSUE TICKETS, not file commits…") — M-4 ✅
  except: **the claim did not land** (LF-3, below). His sleep(2) was
  clamped to the roster's 10-minute floor — bounds behave as configured.
- 16:41 operator repair: `ticket_note_id` stamped by hand into
  `660a8eec`'s context (his improvised `backlog_ticket` value was the
  EXACT right slug — only the key name failed him). Card flipped to
  `ready_depth: 0` / `in_flight: 1`: claim semantics hold once the key
  exists (M-5/M-6 ✅ with repair; unrepaired they FAIL — see LF-3).
  Duplicate-dispatch probe deliberately skipped: the admission funnel is
  being rewritten in a concurrent Codex session (the BP-02/03/04 P0), so
  the DB-error-vs-409 shape is mid-change.
- 16:45 worker underway: `660a8eec` processing, assigned, workspace pod
  Running, MiniMax-M3 on sandbox (M-9 ✅ for the tester leg).
- Respawn re-registered him as agent `f71a5549`, status `session`,
  heartbeating — the agents row is healthy again, so LF-1 is BOUNDED:
  it bites only pods that never re-register; recycling clears it.

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
- **LF-3 — the officer's own dispatch tool could not claim a ticket.**
  `JobCreate.ticket` (B3's funnel half: → `context.ticket_note_id`, the
  one key the claim ledger and `uq_jobs_active_ticket_claim` read) was
  never exposed on the `create_job` descriptor his tool schema is
  generated from. He improvised `context.backlog_ticket` with the correct
  slug and reported the ticket claimed; the ledger disagreed and the card
  kept `ready_depth: 1` — with auto_pull on, the tick would have
  dispatched a second worker onto the ticket. The exact failure the claim
  exists to prevent, produced by the claim being unreachable. FIXED in
  `05ef8dac` (descriptor + client forwarding + MockTransport pin);
  requires the next deploy to reach dev. The manual path is proven only
  WITH this fix; until deployed, manual officer dispatches need the
  operator repair above.
- His turn-1 quality, for the record: cancelled his own `pending_review`
  job rather than rubber-stamping it, preserved the candidate by commit
  (`0140f70`) and branch, and rebuilt its acceptance as an INDEPENDENT
  tester verification — with the executor promotion gated on that test
  going green, research parallel, and only the genuinely dispatchable
  ticket marked ready. The dependency chain is exactly what the category
  model wants. "Legatus" spelling in his sleep reason — his charter
  predates the rename; historical artifacts, as documented.

- 16:51:37 Legate release: research may dispatch on his judgment; told him
  honestly that his tool cannot pass the claim and the ledger is being
  repaired server-side until the fix deploys.
- 16:52:04 he stamped the research ticket ready (`ready_at` moved);
  16:52:20 dispatched `a9a468df` — researcher contract landed
  (`work_category: researcher`, `officer_slot: research`, SPIKE kickoff).
  Having been told `backlog_ticket` was dead, he stopped writing it —
  literal, correct instruction-following; claim operator-stamped with the
  slug. Board after: test 1/0, research 1/0, build 0/0, `below_floor`
  everywhere — the true queue-duty signal.

- 17:03:18 his steady-state digest: "Research worker is away: job
  `a9a468df…` is active on ticket `backlog-researcher-…`. The tester
  `660a8eec` is also active; build remains undispatched behind tester
  GREEN." — names the exact ticket slug as instructed. He settles into a
  monitor cadence "for verifier result or researcher questions."

## Verdict (as of 17:00Z — two workers in flight, executor gated by design)

**The feature holds on its home arena once its code actually reaches every
party.** All three structural findings are versions of the same sentence:
the contract machinery works, but a participant was running or reading a
surface that never got the contract (stale persona, stale kb tools, a
descriptor that never exposed the funnel's field). Nothing found wrong
with the design; everything found wrong was reachability.

- **F1 phantom steering: FIXED.** Every officer KB write landed in the
  loop's project KB, first try, verified independently before any
  dispatch.
- **F2 self-inferred DoD: FIXED on this evidence.** The Legate's web-UI
  demo DoD survived two weeks of hold and a pod respawn; both the
  executor ticket and the tester's acceptance criteria are shaped by it.
  No CLI-bias artifact anywhere in his backlog.
- **F3 counter collision: STRUCTURALLY REPLACED.** Tickets are claimed by
  note id + `ready_at` generation; no iteration counter exists to
  collide. The 08-06 loop's reuse of iter-1..12 over July's numbers shows
  the old failure alive in the old mechanism; the officer did NOT flag it
  (P4 missed, as predicted low-confidence).
- **F4 QA starvation: FIXED in mechanism.** The tester pool is a
  first-class floor-bearing pool; the first dispatched worker of the new
  regime IS a tester; `below_floor` is rendered where the officer and the
  Legate both read it.
- **Predictions: P1 ✅ (unprompted, and better than predicted — he
  cancelled rather than approved), P2 ✅, P3 partial (verification of his
  own candidate, not the unattended loop claims), P4 ❌, P5 ✅ with the
  LF-3 caveat (his claim BELIEF was wrong through no fault of his own).**
- **Officer quality throughout:** verify-before-report on every single
  claim he made; precise digests; dependency-gated dispatch; honored
  every hold. gpt-5.6-sol as the brain is earning its keep.

## Addendum 19:00–19:30Z — the chain closed, unattended

Written after the verdict above, and it supersedes the "remaining to
observe" line: **the loop closed on its own while nobody was watching.**

- **18:10** the researcher (`a9a468df`) completed. He judged it and
  dispatched its *named consumer* ticket — the demo-agenda spike the
  report itself nominated — as `05c18dc9`. Report → judgment → next
  dispatch, no human in the path.
- **19:14** the tester (`660a8eec`) completed NOT GREEN. Its four runtime
  gates failed by absence: the candidate commit `0140f70` on branch
  `job/4268052c` was not in its freshly provisioned per-job repo (LF-4,
  now filed).
- **19:16:42** he judged it, and did three distinct things right. He
  **narrowed his own worker's overreach** (the report implied the UI did
  not exist; the evidence only showed it was unreachable from that
  workspace), superseded that note, closed the ticket on the honest
  non-green result — then dispatched an executor,
  `backlog-executor-publish-ui-candidate-0140f70-as-durable-verifier-input`,
  to fix the *infrastructure cause*. He diagnosed LF-4 independently and
  put work against it.
- **19:16:51** — nine seconds later — the Legate's hold landed for the
  change of command.
- Both new tickets carried correct `category:` tags and armed `ready_at`
  **unprompted**: he learned the machine-tag grammar from one correction
  and applied it to work filed an hour later. Both dispatches again
  lacked the claim (LF-3, unchanged until deploy) and were
  operator-repaired.

So the answer to "does the officer/loop unification work" is yes, on
evidence stronger than the planned run would have produced: a worker's
finding became a judgment became a new ticket became a dispatch, twice,
including one aimed at a platform defect the officer found himself.

## Coda — wrong century

The whole run happened on **`68137e29` "Better Resavio (pre-split
archive)"**. The live project has been `a572e4a0` since the 08-13 split;
the two differ only by a parenthetical suffix, and the officer post,
thread and pod were all bound to the archive from July. Nothing was lost —
the artifacts are real and the product line is the same — but the century
was garrisoned in a museum for the entire live-fire.

Fixed the same evening: held → decommissioned with force (both in-flight
workers left running deliberately; their completions land in the archive
post's while-vacant ledger) → recommissioned on `a572e4a0` as thread
`6ce5bc4c` with the identical kit and brain. One incarnation recorded on
the archive post spanning 2026-07-29 → 2026-08-15, `harvested: true` — the
first real exercise of the decommission path, and it held.

Because KB binding is project-scoped, his findings could not follow him;
the ranked demo workflows, the seven acceptance criteria, the open
stakeholder questions and the LF-4 warning were carried across by hand in
his first directive. **The class of mistake is now guarded in code**:
`list_projects` hides archived projects by default and `get_project`
leads with a warning naming what not to do to an archive (`5c0416b3`).

## Post-deploy disposable Officer gate — 2026-08-16

This is a separate follow-up to the committed Resavio chronology above; it does not rewrite
that run or imply that its earlier findings were observed on a different build. The shared
development environment was identified as kubectl context `main`, namespace
`superhuman-remote-worker`, behind `api.srw.works` / `cockpit.srw.works`—not the ambient
local `k3d-srw` context. At the time of the gate the relevant deployed images were
orchestrator `sha-c62b8ea`, agent `sha-5c0416b` and cockpit `sha-505592a`.

Only disposable artifacts were used:

- project `4e5fb287-f0d6-4e8d-9b4b-fe185bbf57ea`,
  `Codex Officer Gate 20260816T072759Z`;
- Officer thread `77ab8ec2-9616-4e4f-9281-0989ff345f5c`;
- ticket `officer-gate-tiny-sandbox-inspection` and sandbox researcher job
  `6a5a3f46-43d2-4013-abf1-2a04a29fed70`; and
- original/restarted exact Officer pod UIDs
  `1cd74f74-d8ff-4493-9b87-4f037db433a4` /
  `25eb4635-0ef2-4dc8-a61a-47f0d6081c9e`.

The supported commission endpoint, with no permission override, produced one live durable
post link to a `centurion`/`autonomous` thread with `auto_pull=false`. The runtime loaded
49 tools: create/list/get plus job lifecycle, routing, inspection and evidence actions were
bound; workspace/object-plane tools were absent. Its wake executed tools, persisted
matching result messages, produced useful output and retained a normal next wake. A manual
ticketed sandbox dispatch completed and carried ticket, incarnation, slot/category and
admission provenance.

A controlled exact-pod restart restored 59 messages and the next turn executed two paired
inspection calls, emitted useful output and filed another normal wake. There was no
permission wait, tool-pairing 400 or zero-tool loop. The kill lost the race to the four
preceding tool-result commits, however, so LF-5's exact orphan window and repeated-error
escalation remain unverified; LF-5 stays open. Every named project/post/thread/job/wake/
message row and both pods were removed after the gate.

This verifies the deployed lifecycle/configuration checkpoint and closes LF-6. It does not
close this wider scorecard: the original M-10/full-capacity observation and LF-5's exact
interruption contract remain outstanding, and `auto_pull` was never enabled. BP-05 is the
subsequent local, uncommitted, not-deployed ledger checkpoint described in
`docs/done/deleting_a_job_releases_its_backlog_ticket_claim.md`; none of its acceptance is
claimed from the deployed system above.
