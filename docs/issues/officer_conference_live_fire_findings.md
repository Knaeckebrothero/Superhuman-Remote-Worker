---
tags:
  - issue
  - officer
  - conference
  - knowledge-base
  - notifications
related:
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[dual_app_persistent_app_redundancy]]"
  - "[[session_job_management_toolset_rework]]"
  - "[[centurion_implementation_notes]]"
---

# Conference live fire — eight findings from the first real officer conference

**Status:** 🔴 **OPEN** — filed 2026-07-30 for design discussion; F7/F8
and the F4 mail-reply addendum added 07-31 after the first supervised
night. Nothing below is fixed; operational mitigations that already
happened are marked inline (the original interim deadlock resolved — see
*Night-2 addendum*).

**Context.** First production use of the conference machinery (centurion.md
§2/S9) on the Better Resavio century, evening of 2026-07-30. The mechanical
layer worked exactly as designed: one open conference per project, officer
hold stamped and politely announced, brief wake enqueued on conclude,
hold released. Everything in this document is what stood between a Legatus
dictating a two-paragraph order at 17:28 and the century acting on it —
**as of filing, ~2.5 h later, the officer still has not received the
order.** Each finding alone is survivable; chained, they turned a working
handoff mechanism into a deadlock.

## Timeline (evidence)

| UTC | What happened |
|---|---|
| 17:13 | Conference opened via cockpit button → thread `773b10fe`, `config_name=centurion`, override only `{officer:{conference:true}}` → **no model inherited**, boots on platform default (gemma-4); `<\|channel>thought` think-tags leak into replies (F2) |
| 17:15 | Embodiment sweep: `list_worker_jobs` returns 20 pending_review jobs across **≥3 projects plus ownerless daily automations**; proposes bulk-cancelling foreign jobs "to clean the view" (F1) |
| 17:28 | Model switched to `gpt-5.6-sol` in live session settings (manual workaround for F2); directive dictated |
| 17:29:11 | `kb_write` → `legatus-directive-2026-07-30-truth-gate-then-ui` (verbatim, correct) |
| 17:29:51 | Session ended → hold released, brief wake enqueued: *"direction agreed there is now in force. Re-read the charter posture and any KB/backlog notes…"* |
| 17:30–17:36 | Officer wake: **15+ searches, all against index stamped `29f03b66` (stale)** — directive unfindable; 10-action wake budget guard trips; "Conference handoff gap" appended to century-state; digest item queued (F3, F5) |
| 18:06 | Next wake: still nothing → **page 2/3**: "the promised durable direction still is not present…" (delivered — the page→email channel works; F4) |
| 17:37–19:03 | Operator reindex → runs **full**: 3012 notes re-embedded, **86 minutes**, finishes **partial** (12 varchar errors + 17 broken frontmatter + 4 dup-id pairs); watermark finally advances past `29f03b66` (F5) |
| 18:37, 19:23 | Officer wakes **no longer search at all** — after repeated failure he rationally stops polling and waits for the Legatus. The healed index is invisible to him (F3) |

## Findings

**F1 — Officer command surface has user-wide authority but project-wide
duty.** The centurion tool family (`list/get/create/approve/resume/cancel/
pause/steer_worker_job`, `get_stuck_jobs`) calls the global endpoints
(`GET /api/jobs`, `PUT /api/jobs/{id}/cancel`, …) via a client that acts
as the *session user* (`src/tools/orchestrator/jobs.py:742`). For an admin
Legatus that is the whole cluster; for any user it is all their projects
plus ownerless MCP jobs. The sitrep generator is correctly
project-scoped — his *senses* are scoped, his *hands* are not. Live
consequence: the embodiment proposed cancelling another project's test
fixture and the ownerless automations. Same gap applies to the background
officer, who has listed foreign test jobs since night 1.

**F2 — Conference does not inherit the officer's brain.** The cockpit
button posts `config_name: centurion` + `{officer:{conference:true}}` only
(`project-officer.component.ts:498-507`); the officer's
`config_override.llm` (his actual brain) is not copied, and the centurion
config deliberately pins no model. Result: the embodiment that will write
binding direction into the project stores boots on the platform-default
small model, with family-template think-leak as a bonus symptom. Manual
mid-session model switch works but shouldn't be a required step.

**F3 — The brief is a pointer through a search index; content must ride
the brief instead.** `_conclude_conference_if_any` (main.py:20824) sends
"re-read the stores" and leaves *retrieval* to KB search — a pull through
an index that is always somewhat stale (here: hours, and the note was 40 s
old). Compounding behavior: an officer who has searched hard and failed
*stops searching* (correct triage from his side), so even a healed index
never reaches him. Pull cannot converge; the direction must be **pushed**:
the conclude hook can deterministically enumerate the notes written during
the conference (query by conference thread id — index-independent, proven
live) and inject **handles plus the inline body of small notes** directly
into the wake payload. An aux-LLM transcript summary covers direction that
was discussed but never written (weak/lazy embodiment); it must never
block the hold-release, degrade to the manifest on aux failure, and be
marked machine-generated (a Legatus note always outranks it). Wake copy
should stop implying search is the retrieval path.

**F4 — notify_user reaches the Legatus unevenly, and success reporting
can lie.** `digest` urgency appends to a metadata ring surfaced **only**
on the cockpit officer card ("Digest — what he queued for you") — no
badge, no push, no email; three items sat there invisible all evening
(digest email delivery is a known open item). `page` urgency does deliver
(email via SMTP; the 18:06 page arrived), but `_dispatch_officer_page`
returns `not results.get("error")` while the email leg can fail with only
`results["email"]=False` — a failed send can still report "Paged the
Legatus". The 07:48 page was likely delivered but unnoticed; there is no
in-app record of sent pages beyond the budget counter.
**Addendum (07-31): the page email cannot be answered.** The cockpit
"Answer" button in the notification center dead-ends — officer pages are
queued with `job_id = thread_id` (no job behind a page), so the center
cannot resolve the message and shows nothing. There is no reply route
from the email itself either (the IMAP reply loop routes to *job* message
threads). The only working reply channel is typing into his session —
which nothing in the email or the notification center points to.

**F5 — KB reindex: partial forever, expensive when manual, and stuck-file
churn.** The operator reindex ran full (3012 embeddings, 86 min). ~30
loop-era notes fail **every** pass and will forever: 12 × `value too long
for type character varying(100)` (kb_reindex.py:827), 17 × invalid
frontmatter (shell markers, diff hunks, broken block scalars in YAML —
kb_reindex.py:749), 4 duplicate-id pairs (kb_reindex.py:354; already
tracked from the vault-corruption incident). Result: the index never
reaches "clean", the same errors re-log every sweep, and "partial" is the
permanent steady state. Wanted: truncate-don't-fail for length caps, a
quarantine list for known-bad files (skip without retry churn, surface the
count), dup-pair cleanup, and index-state surfacing a human can see.

**F6 — Two search planes disagree, which defeats operator verification.**
The agent's `kb_search` queries the chunk index and stamps its watermark
(`index @ …`); the MCP `search_knowledge` tool answered from a
different/fallback path with **no index stamp** and found the note while
the agent-visible index was still stale — producing a false "it's findable
now" all-clear during the incident. Both planes should query the same
index or at minimum both must stamp what they searched.

**F7 — Every push drains the whole worker fleet (content-blind drift +
retag-on-anything).** Found on night 2 (07-30→31): CI re-tags **all**
images on every develop push — including docs-only commits (the agent tag
literally pointed at a docs HEAD, `sha-26a734d`) — and the lifecycle
reconciler's drift predicate compares tag SHAs, not image content. Result:
each push re-declares every running agent pod stale and starts a rolling
drain. Overnight, several pushes produced drain waves at 21:51, 23:14,
23:23 ×2, 01:04 and 02:21 (`category=drained, reason=stale_image,
exit_code=0` in the reap captures); the officer's two contracted jobs were
Continue-as-New'd across ≥3 agent pods each. The drain itself is graceful
(boundary freeze, checkpoint resume, VM workspace untouched) but not free:
pause → re-dispatch → boot → compacted restore → re-orientation, paid
repeatedly, plus at least one degradation to the orphan/missed-heartbeat
crash path (cousin of [[version_upgrade_drain_livelock]]), plus the
officer's pause/cancel calls failing 5× against the orchestrator's own
concurrent rollouts. A meaningful slice of the night's "900 steps, only
RED tests" was this churn, not worker output.

**F8 — Drain and deploy causes are invisible to the officer.** All he saw
was `fleet: agents_offline` and `orphans_recovered (agent offline)` —
indistinguishable from crashes. He attributed platform churn to worker
failure and escalated (two destructive re-plans) against restarts the
platform itself was causing; the platform's state-*preserving* restarts
then interleaved with his state-*resetting* re-plans, each wiping what the
other kept. The original postmortem's "sitreps should tag deploy windows"
(P2-B) now has a full night of receipts.

## Fix directions (for discussion, roughly ordered)

- **P-A Scoping** (F1): (1) agent-side default: officer job tools query
  `/api/projects/{id}/jobs` for the session's projects; mutating tools
  pre-check the target's project. (2) The real boundary is
  orchestrator-side: session-originated internal calls carry the thread's
  project scope and `/api/jobs*` filters/denies beyond it — design
  belongs with [[session_job_management_toolset_rework]].
- **P-B Brain inheritance** (F2): conference create copies the officer
  thread's `llm` override server-side (client stays dumb).
- **P-C Brief payload** (F3): deterministic note manifest + inline small
  bodies in the conclude wake; aux-LLM minutes as second layer; copy
  change. This is the "streamline" core: direction arrives *inside* the
  officer's next wake, unconditionally.
- **P-D Notification truth & visibility** (F4): unread-digest badge (and
  eventually digest email flush); page result reflects the email leg
  honestly; page history visible on the officer card.
- **P-E Reindex hygiene** (F5): truncate-don't-fail, quarantine list,
  dup cleanup, "index behind by N commits / M quarantined" surfaced in
  the KB panel.
- **P-F Search-plane honesty** (F6): one index for both tools, or a
  mandatory "searched index @ X" stamp everywhere.
- **P-G Stop the false drains** (F7, F8 — likely first to build): (1) CI
  only re-tags components whose build inputs changed; (2) drift predicate
  compares image digests (or build-content hash), not tag strings;
  (3) sitreps tag active deploy/drain windows so restart churn is
  attributable; (4) officer doctrine until then: clustered
  `agents_offline`/orphan events ⇒ suspect deploys before workers
  (issued as standing order 4, 07-31). Endgame per the postmortem annex:
  rainbow deploys — in-flight jobs finish on the old version.
- **P-H Mail reply routing** (F4 addendum): page emails and the
  notification center must link to the officer's session (the only
  working reply channel), or grow a real reply route.

## Night-2 addendum (2026-07-31) — how the handoff resolved

The directive was delivered by session message via the API front door
(`POST /api/persistent/threads/{tid}/input` with the user's MCP
credential) after the pointer-brief failed — the officer read the note by
handle on first try and dispatched both contracted jobs within two
minutes. Overnight he supervised them well (2.5 h of "no steering
needed", artifact-based contract checks, guidance-lane steers, evidence
preserved, gradual escalation, page with an A/B/C decision) while F7's
deploy churn rolled his workers underneath him. Both jobs were cancelled
near 02:00 with zero contract deliverables (part churn, part genuine
worker process-drift: spec ceremony + RED tests, no implementation).
Morning after: standing orders issued (act within authority instead of
idling the century; guidance over re-plans with a one-re-plan cap; cancel
only provably stalled work; suspect deploys on clustered offline events)
and recovery option A authorized. The officer's own slot roster put the
heavy job's workspace on a VM automatically — the one part of the stack
that held all night.

## What already worked (keep)

Single-writer conference, hold + polite stand-by notice, brief wake with
event coalescing, hold-release on end, page→email delivery, live
mid-session model switch, and — throughout — the officer's discipline:
he never invented orders, never burned slots repeating a failed pattern,
recorded the gap durably, and escalated on budget. The platform failed
him; he did not fail the platform.
