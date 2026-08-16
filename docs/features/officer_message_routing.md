---
tags:
  - feature
  - architecture
  - officers
  - communication
  - jobs
  - autonomy
status: implemented-M1-M4-audit-blocked
created: 2026-08-14
aliases:
  - officer first messages
  - worker question routing
  - officer message chain
related:
  - "[[centurion]]"
  - "[[officer_post]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_supervision_surface]]"
  - "[[unified_orchestrator_tool_surface]]"
  - "[[unified_message_store]]"
  - "[[supervisor_control_plane_and_live_talk]]"
  - "[[officer_backlog_pools]]"
  - "[[officer_control_plane_post_implementation_audit]]"
---

# Officer-aware worker messages — triage before interruption

> A worker's question or blocker can enter the officer's chain of command before it
> interrupts the user. The project chooses one of three routing policies:
> `user_direct`, `officer_and_user`, or `officer_first`. The officer may answer the worker,
> escalate the same thread to the user with context, or resolve it and later inform the
> user. Routing is server-side, durable, project-scoped, and unable to strand a blocking
> job when the officer is vacant, held, offline, or silent.

## Status

**M1–M4 IMPLEMENTED 2026-08-14** on develop (`be1d972e`): migration 0159
`job_message_routes`, effective-policy resolver with per-route snapshots, the
one-transaction blocking send (message + route + wake intent + freeze, all-or-nothing),
held/vacant/unreachable fallbacks with hold/decommission drains, the officer inbox
(high-urgency `worker_message` wakes, sitrep section, reply/escalate/acknowledge tools
with post-row actor guards), and the leader-gated SLA/total-timeout reconciler with CAS
exactly-once semantics. Ratified defaults: DB `officer_first` (0163; `user_direct`
through 0162), 15-min officer SLA,
immediate-both for `officer_and_user`. M5 (route badges/thread actions) deferred — the
O5 card already carries the policy selector. Live k3d walk proved reply-resume, SLA
escalation, total-timeout resume, and the fail-closed guard. O6 subsequently released the
Resavio officer successfully with `auto_pull=false`; its committed live-fire is still in
progress, and a real officer LLM turn consuming a routed question remains an outstanding
observation there. Original status for the
record: **PROPOSED (2026-08-14). Nothing implemented.** The existing worker `send_message` tool,
job message endpoint, notification delivery, generic reply route, officer wake outbox, and
message readers provide most primitives. Missing are policy resolution, an officer inbox
route, durable route state, escalation tools, and the timeout that the current config
already promises but does not implement.

**2026-08-15 post-implementation checkpoints:** transactional direct blocking-route
creation, runtime-actor authorization, route-generation CAS, initial message/evidence
sanitization, and truthful retryable delivery outcomes are closed. The subsequent Officer
Post checkpoint also stages hold/decommission fallback durably inside the post transaction,
with external notification only after commit; stale blocking-route creation validates the
same current incarnation under the post lock. The lifecycle follow-up additionally routes
job completions with one post-locked exact-incarnation-or-vacant-ledger decision and folds
an undelivered commission brief back into durable state during decommission. That follow-up
is local and not deployed in the already-running O6 live-fire. The ordered audit remains
[[officer_control_plane_post_implementation_audit]] because lower-priority OC-05/OC-06
residues and unrelated unattended-backlog gates remain open.

Audit markers:

- **[A-send]** `src/tools/communication/messaging.py::send_message` and
  `orchestrator/main.py::send_agent_message` (`POST /api/jobs/{job_id}/messages/send`).
- **[A-reply]** `orchestrator/main.py::reply_to_agent_message`, `_route_inbound_reply`,
  and the existing officer steering wrapper.
- **[A-timeout]** `config/worker_base.yaml::communication.blocking_timeout_hours` and the
  explicit no-reaper comment beside `waiting_for_reply` in
  `orchestrator/database/postgres.py`.

## 1. Current behavior and the liveness hole

`worker_base.yaml` grants `send_message` and advertises
`communication.blocking_timeout_hours: 24` **[A-send]**. The tool writes a workspace copy,
then calls the job-scoped send endpoint. The endpoint resolves a human job owner/project
recipient, sends a notification, logs it, and for `mode=blocking` moves the job to
`waiting_for_reply` with freeze data **[A-send]**. The generic reply endpoint resumes that
job when the matching thread receives an answer **[A-reply]**.

There is no timeout/reaper. The code explicitly notes that nothing reaps
`waiting_for_reply` despite the YAML setting **[A-timeout]**. This becomes more serious with
backlog pools: a waiting job is intentionally non-terminal, so it retains its ticket claim
and capacity. A single unanswered executor question can hold the singleton indefinitely.

The officer already has the opposite direction (`notify_user`) and a durable wake outbox,
but no canonical way to receive, answer, or escalate a worker's user-bound message. Routing
must be added at the existing server endpoint; changing only the worker tool would be
bypassable by any other caller.

## 2. Policy — separate from job autonomy

This is a project governance setting, not another value in the existing job `autonomy`
enum. Job autonomy controls completion/review behavior; message routing controls who is
interrupted. The server resolves the policy from the job's `project_id` and the durable
[[officer_post]] row on every new logical message.

| Policy | Initial delivery | Officer behavior | Liveness fallback |
|---|---|---|---|
| `user_direct` | User | Existing behavior | Existing channels + total blocking timeout |
| `officer_and_user` | Same canonical thread to both | Officer may answer or add context; user may answer/override | First valid answer resolves atomically; later user answer becomes sourced guidance |
| `officer_first` | Officer only initially | Answer worker, or escalate the same thread to user; may separately inform user afterward | Direct user delivery if no commissioned/available officer; SLA escalation if silent |

There is deliberately no strict `officer_only` mode in v1. `officer_first` gives the desired
triage behavior without making a manager outage capable of permanently suppressing a
worker's blocker. A strict never-user policy could be added later only with an explicit
terminal fallback visible to the user.

### 2.1 Defaults and authority

- Posts default to `officer_first` (migration 0163). Vacant posts still *resolve* to
  `user_direct` — the default is what the row stores, not what a question with no officer
  gets.
- The commission UI recommends `officer_first`; the Legate/project admin may choose any
  policy. An explicit choice survives future default flips: 0163 only moved rows that were
  still on `user_direct`.
- Only a project admin/Legate can change the policy. Worker content, job config, the officer,
  or prompt-injected text cannot.
- The effective policy is always `user_direct` when there is no commissioned officer.
- v1 policy applies only to `to="user"`/owner-directed messages. An explicit named project
  member/contact remains direct and visible in audit.
- Jobs without exactly one project remain `user_direct`.

The policy chosen for a message is snapshotted onto its route. Changing the project setting
does not retarget an already waiting question.

## 3. Route state and one canonical thread

Do not create a second conversation log. Keep the original message/replies in the existing
job message path and add a small route ledger (or equivalent normalized columns) for
delivery/control state:

```text
route_id, job_id, project_id, thread_id, originating_message_id
policy_snapshot, state, blocking
officer_thread_id, officer_incarnation, officer_deadline
user_delivery_at, resolved_by_kind, resolved_by_id, resolved_at
total_deadline, created_at, updated_at
```

States:

```text
pending_officer -> resolved_by_officer
                -> escalated_to_user -> resolved_by_user
                -> timed_out
pending_both    -> resolved_by_officer | resolved_by_user
user_direct     -> resolved_by_user
any pre-delivery state -> delivery_failed -> fallback or timed_out
```

`message_log` remains the delivery/audit record; the route row is the logical thread's
control state. Internal officer delivery does not pretend to be an email and does not
consume the user's email rate limit. This is distinct from [[unified_message_store]], which
explores convergence of LLM conversation history (`thread_messages`/`chat_history`). If that
work later creates a canonical message model, the route ledger can reference it; neither
feature blocks the other.

For a blocking send, one database transaction/outbox boundary must:

1. persist the logical message and policy snapshot;
2. create route state and durable delivery/wake intent;
3. move the job to `waiting_for_reply` with matching freeze/thread metadata.

If that unit cannot commit, the job stays runnable and the tool reports failure. Never
freeze first and hope officer delivery succeeds afterward.

## 4. Officer inbox and tools

The existing shared job surface supplies `list_message_threads` and
`get_message_thread`. Add bounded control operations from the same descriptor/client layer:

- `reply_to_job_message(job_id, thread_id, message)` — answer as officer; for a matching
  blocking route, atomically resolve and resume the worker through the existing reply lane.
- `escalate_job_message(job_id, thread_id, context=None)` — deliver the original worker
  message plus clearly separated officer context to the user; retain the same `thread_id`
  and reply/resume path.
- `acknowledge_job_message(job_id, thread_id, note=None)` — close an asynchronous officer
  inbox item without pretending the worker was waiting; optional in the first slice.

Officer actions record actor/thread/incarnation and cannot erase the original message. A
reply is guidance, not authorization: content from either worker or officer cannot
automatically add backlog `ready`, approve a job, waive a deliverable, or mutate a claim.
Those remain explicit tools with their own guards.

The worker-facing `send_message` interface can remain compatible. An optional
`purpose=question|blocker|update` improves presentation and coalescing, but route policy may
not trust that self-declared label; `mode=blocking` remains the mechanical signal that the
job must wait.

## 5. Wake, availability, and timeout rules

### 5.1 Officer delivery

- A blocking `officer_first` message creates a `worker_message` wake with urgency high and
  bypasses normal wake debounce. It may still coalesce with already claimed events into one
  turn, but cannot wait for the next routine timer.
- Asynchronous messages may coalesce into one inbox/SITREP section.
- If the post is vacant/ended, the effective policy is `user_direct` for blocking and
  asynchronous messages. If a commissioned officer is transiently unreachable or wake
  enqueue fails, blocking messages fall back to the user immediately; asynchronous
  officer-first messages remain durably queued for his next healthy wake.
- A held officer (maintenance or conference) is unavailable for the blocking SLA. Route a
  blocking question to the user immediately; asynchronous officer-first messages may queue
  behind the hold. Never release the hold merely to deliver a worker message.
- Entering hold or decommissioning transitions **already pending** officer-first blocking
  routes to durable user-fallback intent in the same post-locked transaction. Notification
  delivery runs after commit and remains retryable while its acceptance stamp is null. A
  later recommission does not adopt an old waiting route merely because it now occupies the
  post.

### 5.2 Two deadlines

1. **Officer response SLA** — short project policy (recommendation: 15 minutes for blocking
   messages). On expiry, a compare-and-set moves `pending_officer` to
   `escalated_to_user`, dispatches the same thread, and records why.
2. **Total blocking timeout** — the existing job config
   `communication.blocking_timeout_hours` (24h default). A leader-gated reconciler finds
   due unresolved routes, marks them `timed_out`, and resumes the job with an explicit
   system reply: no answer arrived; proceed under the existing autonomy policy with a
   documented conservative assumption, or complete honestly as unable to proceed.

The reconciler uses `FOR UPDATE SKIP LOCKED`/compare-and-set semantics so officer and user
answers racing the deadline unblock exactly once. It also repairs a route whose delivery
outbox failed. Resume additionally matches `waiting_for_reply` and the route/freeze
generation; a cancelled or otherwise terminal job remains terminal and receives only the
route audit transition. No polling loop should infer deadlines from prose or workspace
files.

### 5.3 `officer_and_user` races

Both see one logical thread. The first valid answer atomically resolves a blocking route.
If the officer answered first and the user later replies, the later higher-authority user
message is not discarded: while the job is non-terminal it enters the existing guidance
lane as a sourced user steer; after disposition it is recorded and wakes the officer rather
than pretending a finished job can resume. If the two answers conflict before resolution,
user authority wins and the officer receives the result in the next SITREP. Duplicate
notifications never create duplicate job threads.

## 6. User experience

The Centurion/post card gains **Worker questions** next to autonomy controls:

- Direct to me
- Officer and me
- Officer first *(recommended when commissioned)*

Helper copy says that officer-first falls back/escalates to the user if the officer is not
available or does not answer in time. The card shows the blocking SLA separately from the
24-hour total timeout.

Cockpit renders one job message thread with route badges such as `with officer`,
`escalated`, `answered by officer`, `answered by you`, and `timed out`. The user can answer
an officer-held thread at any time; doing so exercises their higher authority and resolves
it through the same CAS path.

Officer-to-user communication remains two distinct operations:

- **Escalate message:** asks the user on behalf of a specific waiting job and preserves the
  reply/resume thread.
- **`notify_user`:** informs/pages/digests the user in the officer's own voice, for example
  after the officer resolved a blocker. It does not masquerade as a reply from the worker.

## 7. Security and abuse resistance

- Resolve policy and project membership server-side from `job_id`; ignore caller-supplied
  officer/user IDs.
- Store actor kind and ID on every route transition. Only the commissioned officer thread
  for that project may use officer actions.
- Delimit the original worker text and officer context in user notifications; neither is a
  trusted instruction to the orchestrator.
- Internal officer messages use separate per-project flood limits and async coalescing.
  Human email/channel rate limits still apply only when the user is actually notified.
- Redact secrets using the existing notification/message policy before either audience is
  served.
- Project scoping from [[officer_supervision_surface]] applies to inbox reads and actions.

## 8. Build slices

| Slice | Contents | Depends on | Gate |
|---|---|---|---|
| M1 | Validate/expose the post's `communication_policy`; effective-policy resolver and per-route snapshot (column rides O1 when built together) | [[officer_post]] O1 | vacancy, commission, explicit-recipient, and setting-change unit matrix |
| M2 | Route ledger + transactional blocking-send/outbox boundary + immediate-unavailable fallback | M1 | failure injection proves no frozen job without a durable route |
| M3 | `worker_message` wake/inbox rendering + reply/escalate descriptors and guards | M2, unified surface S2–S3 | live officer answer and user escalation resume the same thread |
| M4 | Leader-gated officer-SLA and total-timeout reconciler; CAS race handling | M2 | fake-clock tests; two reconcilers/answer race unblock once |
| M5 | Cockpit policy control and route badges/thread actions | M1–M4 | Vitest + live walk through all three policies |

M2 and M4 are one usable safety boundary: officer-first must not ship without both timeout
paths. M1 may land alongside [[officer_post]]; M3 reuses the shared tool surface rather than
adding Centurion-only HTTP code.

[[officer_backlog_pools]] B1/B2 are independent. B3 `auto_pull` should not be enabled for
blocking-capable workers until M2–M4 pass, because `waiting_for_reply` holds both claim and
capacity by design.

## 9. Acceptance

1. `officer_first`, blocking: worker asks → one high-urgency officer wake → officer replies
   → same thread resumes exactly once; no user notification is sent.
2. Officer escalates instead: user receives original question plus officer context; user
   reply resumes the same thread; Cockpit shows the complete route.
3. `officer_and_user`: both receive one logical thread; race their replies and prove one
   CAS resolution. A later user reply is delivered as higher-authority guidance, not lost.
4. `user_direct`: behavior and notification channels remain backwards-compatible.
5. Vacant, held, unreachable, and wake-enqueue-failure cases all send a blocking question
   directly to the user without leaving an orphaned officer route. Hold/decommission an
   officer with a route already pending and verify the same immediate handoff.
6. Let the officer SLA expire: exactly one escalation. Let the total timeout expire: route
   becomes `timed_out`, worker resumes with the no-answer system message, and its backlog
   claim/capacity can eventually reach disposition.
7. Change project policy while a message waits: the existing route keeps its snapshot; the
   next message uses the new policy.
8. Attempt cross-project inbox reads/actions and a worker-authored policy override: denied.
9. Async update through officer-first does not freeze the job and coalesces into the next
   officer inbox/SITREP.

## 10. Open questions (Legate)

1. ~~**Commissioning default:** recommend `officer_first` in the UI while retaining
   `user_direct` as the database/backfill default. Should new commissions opt in
   automatically, or require an explicit selection?~~ **RESOLVED 2026-08-16 (Legate):
   opt in automatically.** Migration 0163 flips the column DEFAULT to `officer_first`
   and moves every existing row still on `user_direct` onto it. Rows carrying an explicit
   `officer_and_user` were left untouched. The vacant-post collapse to `user_direct` is
   unchanged, so this only bites where a live officer is commissioned.
2. **Blocking officer SLA:** recommend 15 minutes, with immediate fallback while held or
   unreachable. Is 15 minutes right, or should the default align with the officer's
   configured `sleep_min`/wake cadence?
3. **Async `officer_and_user`:** recommend immediate delivery to both for literal policy
   fidelity. An alternative is officer immediately/user digest, but that is a fourth policy
   and should not be smuggled into the name.

## 11. Decision log

- **2026-08-14 (Legate proposal):** worker questions/blockers may enter the officer chain;
  the project/user chooses direct, both, or officer-first behavior; the officer may resolve
  or contact the user himself.
- **2026-08-14 (design):** make routing a server-enforced project policy, separate from job
  autonomy; preserve one canonical job message thread; snapshot policy per route; omit a
  strict officer-only mode until there is a safe product need.
- **2026-08-14 (code-audit correction):** the advertised 24-hour blocking timeout has no
  implementation. Officer-first cannot ship without durable officer-SLA escalation and a
  total-timeout reconciler.
