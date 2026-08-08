# Codex brief — finish S1: make the stateless session lane usable by a human

The stateless session lane works. A turn is claimed from a DB queue, runs on any
pod, survives the pod being killed mid-generation, and never double-answers.
What it cannot do is be *used*: the cockpit has no concept of the lane, every
control verb rides a WebSocket that a stateless thread has no pod to open, and
nothing stops `/resume` from booting a pinned pod underneath it.

**Your goal: a user can drive a stateless-lane session from the cockpit exactly
like a pinned one — open it, send, stream, compact, rewind, change mode, and see
a sensible state when their turn is queued behind a busy pool.**

Branch off `feature/stateless-agents`. Do not push. Do not touch `develop`, and
do not build on `feature/stateless-workers-s3` (that is the parallel worker
track; see §7).

---

## 0. Phase 2 — where this stands (2026-08-08)

The **server half is built and verified** on `feature/stateless-sessions-s1-completion`
(commits `504b153f`, `fe436783`): the provisioning gates (more sites than §3.1
originally named — create-time provisioning, warm attach, late agent
registration, permission wake and officer respawn were also holes), a
discriminated `/connection`, a transactional warm-agent reservation, and
registration that no longer treats hostname as an ownership credential. Full
suite at the known baseline; real-Postgres probes green.

Work stopped at the control-verb step for a correct reason: **an
orchestrator-written journal frame collides with the stateless executor's
in-process seq allocator.** The writer seeds `_next_seq` once at attach and
allocates in memory, while `append_system_frame` allocates from the DB
high-water mark. Those coexist only when no pod holds the session — which is
what that helper was written for (rewind on a detached thread, the reaper's
post-steal frames). Warm affinity now keeps a pod attached for up to 300 s of
idle, so an orchestrator ack would take a seq the pod doesn't know about and
the pod's next flush would collide on the unique index and kill the writer.

**The design decision that unblocks it is made — do not re-litigate it:**

1. **Control verbs go over orchestrator REST for BOTH lanes**, not "REST if
   stateless, WebSocket if pinned". See the corrected §3.4: the client must
   never learn what a lane is, and this advances §7's plan to retire the
   per-session WebSocket rather than cutting against it.
2. **The orchestrator never writes the journal frame.** A verb becomes a
   durable, commit-ordered **control request** row (migration **0119**), and
   the **lease owner consumes it** — the pod applies the verb and journals the
   result with its own allocator. The executor already drains pending input at
   claim time (`turn_executor.py:633`); this is the same shape, and
   `thread_permission_requests` is a table precedent to mirror.
3. Verbs that require a live in-flight turn (`interrupt`) stay out of scope and
   keep their 501.

### 0.1 Scope correction (2026-08-08, second): the snapshot comes first

The "zero cockpit changes" claim in §3.4 was **wrong in one important way**, and
the audit that found it was right. Send and receive genuinely need no client
change — but `session.state` is emitted only over the WebSocket
(`persistent_app.py:3366`, via `_ws_send`) and **has no REST equivalent**. Its
own comment at `:3352` states the stakes: *"The durable row survives, but REST
history does not carry it, so without this a reload (or a dropped live stream)
leaves the approval card unrenderable and the gate unanswerable."*

So on a lane with no socket, a **supervised session becomes unanswerable after
any reload** — the permission card cannot render, the gate cannot be answered.
That is a functional dead end, not a cosmetic gap. `session.state` also carries
the authoritative cold-join signal, the running tool, permission/narration mode
and turn count.

**Therefore deliverable #1 is a lane-agnostic, transport-independent
session-state snapshot over REST, serving both lanes** — the same shape as the
control-verb decision, and for the same reason: the client stays lane-free and
the per-session WebSocket moves closer to retirement. Also fix the null
`ws_url` reaching `new WebSocket(null)`; a client-side guard or a sentinel, your
call, but it must not enter the reconnect ladder.

Then: the REST control inbox (§3.3 as shaped above — note the audit's point that
a request table alone can strand a control during completion, so it needs queue
watermarks, pinned-agent fencing and durable journal-write acknowledgement),
§3.4's verification, §3.5, §3.6, §3.7, and stateless ended-session wake.

### 0.2 When to stop — narrowed

An earlier instruction said to stop whenever something rests on a wrong premise.
That was too broad and it has now fired on a finding that **resized** the work
rather than invalidating it. Narrowed rule:

* **Stop** only when a premise is load-bearing *and* there is no reasonable
  alternative — e.g. "the CAS this depends on does not exist", "this write
  corrupts the journal allocator". Those were correct stops.
* **Adapt and continue** when the premise is wrong but the goal survives a
  different route — build the route, record the deviation in the log, and say
  what you changed and why. A discovered dependency is scope, not a blocker.

The goal has not changed: a user can drive a stateless-lane session from the
cockpit exactly like a pinned one, and never learns a lane exists.

**Phase 3 audit result (2026-08-08): §3.4's corrected premise was still
incomplete, so implementation stopped again.** An already-open active tab can
send over REST and receive turn frames over journal/SSE, but a reload cannot:
`session.state` is WebSocket-direct and carries load-bearing state that neither
thread metadata nor the journal replaces (`turn_in_flight` reconciliation,
running tool, pending permission rows, modes and model settings). In addition,
the no-socket response reaches `new WebSocket(null)` and the reconnect ladder.
The next phase starts with a lane-agnostic, transport-independent session-state
snapshot for both lanes. Migration 0119 remains unused; no control-inbox code
was retained. See implementation-log Session 4 for exact evidence and further
wrong-premise findings in permission retirement and ended-session wake.

---

## 1. Read first

1. `docs/features/stateless_agents.md` — **§9.1 Implementation status** tells you
   exactly what exists and what does not; it is written against the code. Then
   §5.3.1 (turn flow), §5.3.2 (epoch/seq — why the client cache survives a
   handoff), §5.3.6 (session semantics that must survive), §5.3.7 (capacity UX),
   §6.7 (the control-verb transport requirement, which is S1 scope, not P6's).
2. `docs/research/stateless_agents/implementation_log.md` — two build sessions.
   Read the "Traps hit" sections; they will save you hours.
3. `src/api/turn_executor.py` — how a turn actually runs.

---

## 2. What exists (do not rebuild)

Migrations 0115–0117 and `src/shared/run_queue/` give you the queue, lease,
fence, watermarks, affinity and reaper — 81 real-Postgres tests pin the
contract. `threads.execution_lane` (`'pinned'`/`'stateless'`) is the partition.
Input admission, the claim bundle, and `/interrupt` already branch on it
(`orchestrator/main.py`, three sites). The event journal, epoch and seq
machinery already keep a client's cached transcript valid across a cross-pod
handoff — that part is done and live-verified.

`scripts/stateless-lane-probe.sh` (`turn` | `burst` | `kill`) drives and
observes the lane; it auto-discovers a lane thread.

---

## 3. The work, in dependency order

### 3.1 Provisioning gate — server side (do this first; it is a safety fix)

Today `execution_lane` is consulted in exactly three places, and none of them is
`/resume` or `/prepare`. The rule "only flip a detached thread, never resume a
stateless one" is a convention in a doc, enforced nowhere. Calling `/prepare` on
a stateless thread right now will provision a pinned pod and bind it to a thread
the queue also serves — two executors on one conversation.

Close it at every entry: `resume_thread` (`orchestrator/main.py`), and
`_do_prepare` / `_provision_agent_for_thread` / `get_connection` in
`orchestrator/routers/sessions.py`. Fail **closed** — whitelist `'pinned'` for
the pinned path rather than blacklisting `'stateless'`, so a future lane cannot
silently inherit pinned provisioning. (Gate 1 on the worker branch used exactly
this whitelist shape for jobs; mirror it.)

### 3.2 `/connection` and `/prepare` answer honestly for the lane

The cockpit ladder is: `GET /api/sessions/{tid}/connection` → 200 with
`{ws_url, token}` when an agent is bound, or 425 when not; on 425 it POSTs
`/prepare`, waits for a `session.lifecycle` SSE `ready`, and retries
`/connection`. A stateless thread never binds an agent, so today that ladder
spins forever behind the provisioning card.

A stateless thread is **ready the moment it exists** — there is nothing to
provision. Decide and document the contract: `/connection` should report ready
without a `ws_url` (or with an explicit "no control socket on this lane"
marker), and `/prepare` should be a no-op success or a clear 409. Whatever you
choose, the cockpit must be able to tell "ready, no socket" apart from "not
ready yet" — that distinction is the whole bug.

### 3.3 Control verbs over REST (§6.7)

`mode.set`, `narration.set`, `config.update`, `compact`, `archive`,
`upgrade-to-workspace`, `undo`, `rewind` are WebSocket method branches in
`src/api/persistent_app.py` (~lines 3454–3570) and nothing else. A stateless
thread has no socket, so all of them are dead on the lane.

Ship the orchestrator-REST subset with **journaled acks** — the caller learns
the verb was accepted, and the result arrives as a journal frame the client
already knows how to render, which is what keeps this working when the verb is
accepted while no pod holds the thread. Two hard rules: authentication and
owner checks must be identical to the WS path (do not invent a second, weaker
authorization surface), and a verb that mutates live session state must be
applied by whichever pod next claims the thread, not by the orchestrator
pretending to be the agent. Some verbs (`compact`, `rewind`) already have
detached-thread REST equivalents — check before building
(`POST /api/agents/threads/{id}/rewind` exists and 409s when an agent is bound).

`/interrupt` is deliberately 501 on the lane. Interrupting a *leased* turn needs
a control path to the pod holding the lease, which is a different problem —
leave the 501 unless you solve it properly, and say so.

### 3.4 Cockpit — **the lane must stay invisible to the client**

**Corrected 2026-08-08.** An earlier version of this section said "teach it the
lane". That was wrong, and the correction matters: a user does not care which
agent serves their request, so `execution_lane` must not appear in the cockpit
at all. Two client paths keyed on a server-side execution model is exactly the
kind of leak that rots.

Read the client before writing any of it — the gap is much smaller than it
looks:

* The composer ungates on `connection.state === 'ready'`
  (`persistent-chat.service.ts:1789`), **not** on the socket opening.
* The control-WS open is already best-effort and its failure is swallowed
  (`:1792–1795`); the comment there states the SSE receive path is primary.
* Turn output already streams over the journal/SSE, which is transport-
  independent and demonstrably works on this lane today.

The requested verification was run and the zero-change claim is **false beyond
an already-open active tab**. `ConnectionPayload` still types `ws_url` as a
string and `_openControlWs` / `_reopenWithFreshToken` unconditionally install
it; null therefore reaches the WebSocket constructor and retry ladder.
`_ensureControlWs` can reset that ladder on focus and SSE recovery.

More importantly, `session.state` is sent directly over the control WebSocket,
not the journal. The client uses it to reconcile a cached in-flight prefix with
cursor-replayed deltas and to restore `running_tool`, pending permission cards,
permission/narration modes, turn count, model and temperature. A null-socket
guard alone would make the composer look ready while reload/multi-tab state is
wrong. Build one authenticated, owner-gated, lane-agnostic state snapshot for
**both** lanes (REST or an equivalent current-state SSE contract), feed it
through the existing `session.state` reducer, and prove mid-turn and pending-
permission reload before claiming send/receive parity. Discriminate transport
with `control_socket`, never with `execution_lane`; if lane invisibility is a
literal wire-contract requirement, the server response must also stop exposing
`execution_lane` to the Cockpit.

The one real gap is the **control verbs**. They go through `_sendControl` →
`controlOutbox` → the WebSocket (`:2082`), so with no socket the buttons queue
forever and silently do nothing. Fix it **lane-agnostically: route control
verbs over orchestrator REST for BOTH lanes.** That way the client gets
simpler rather than more conditional, nothing in it knows what a lane is, and
it advances §7's plan to retire the per-session WebSocket instead of cutting
against it. Do not build a "if stateless use REST, else use WS" fork.

### 3.5 Queued-turn UX (§5.3.7)

No `turn.queued` frame exists anywhere. A user whose turn is waiting behind a
busy pool currently sees nothing at all. Emit a frame at enqueue when the unit
is not claimed promptly, clear it when the claim lands, and render it. This is
the one *new* user-visible state the architecture introduces, and §5.3.7 says it
must have a UX rather than looking like a hang.

### 3.6 Permission-row retire on lease expiry

Nothing sweeps `thread_permission_requests` when a lease dies. A permission
prompt raised by a turn whose pod then vanished stays pending forever. Retire on
lease expiry — the reaper is the natural owner.

### 3.7 Warm-ups (take these first if you want a cheap win)

- **Path-A resume-compaction persistence — a live bug.** Path B persists its
  resume compaction (`src/api/persistent_app.py:6552`); Path A calls
  `ensure_within_limits(..., trigger="resume")` at `:6441` and returns at `:6473`
  without persisting. The comment at `:6543` says Path A skips deliberately, to
  avoid a live/history banner double-render — so the fix must advance the
  boundary row **without** reintroducing that double render. Impact: any thread
  that has ever compacted takes Path A, so an over-budget tail pays a blocking
  auxiliary-LLM summarization on *every claim* and throws it away.
- **Scoped metadata index on the worker path.** `begin_read_cache()` /
  `end_read_cache()` on `VirtualWorkspaceBackend` collapse dozens of rclone
  process spawns into one listing and are wired only into
  `PersistentSession.setup`. `src/agent.py` builds the same backend for lite
  jobs and never opens it; it needs the same open / `finally`-close pair around
  the job's workspace setup. See `no_workspace_agent_mode.md` §5.1.

---

## 4. Constraints

- **Migrations start at 0119.** `0118_jobs_execution_lane.sql` is taken on the
  parallel worker branch; picking 0118 guarantees a collision at merge.
- **Every change is lane-conditional.** The pinned lane is what users are on
  today. A regression there is worse than anything you can fix here — run the
  pinned smoke path before you finish.
- After **any** migration: regenerate with `scripts/schema-snapshot.sh` and stage
  the snapshot in the **same commit**, and bump `APP_CURRENT_MIGRATION_HEAD` in
  `tests/test_infrastructure_metering_migrations.py`. Never edit `schema.sql`.
- Never `git add -A` (there is an untracked `HomeLab/` that stays untracked).
- Never `helm upgrade`/`install` by hand and **never `tilt trigger srw` — it
  uninstalls the release.** Edit `deployment/values-local.yaml` and let Tilt
  reconcile.
- Keep the chart default off. The lane stays opt-in.

---

## 5. Traps (each of these cost real hours)

- **Tilt ships partially-edited images.** `updateStatus: ok` is not evidence.
  Before trusting any measurement or test on the cluster:
  `kubectl --context=k3d-srw -n srw exec <pod> -c agent -- grep -c "<a string you just wrote>" /app/<file>`
  on **every** running pod.
- **Never `git checkout` another branch while Tilt is up** — Tilt watches the
  filesystem, so a branch switch deploys that branch. To check whether a failure
  predates you, stash the one file (`git stash push -- <path>`) or use a worktree
  outside the watch root. Note a fresh worktree lacks gitignored files, so helm
  tests fail there spuriously — 19 of them.
- **admin-cli's `access_token` has no `sub`** and 500s the auth resolver; use the
  `id_token`. It expires in ~15 min and fails as a silent 401.
- **`kill -9 1` inside a container does nothing** (PID-1 signal protection). Use
  `kubectl delete pod --force --grace-period=0`.
- **Local pytest baseline is ~11 failures** on this branch (Python 3.14 env
  noise: `mcp_manager`, arxiv/semantic-scholar package contracts,
  `test_connect_disconnect`). Confirm any failure reproduces before chasing it,
  and do not let them hide a real one.

---

## 6. Definition of done

- A stateless-lane session driven end to end **from the cockpit** at
  `https://localhost/`: open, send, stream the reply, compact, rewind, change
  mode. Drive it with Playwright or by hand, and say which.
- The pinned lane is unchanged — walk the README smoke path.
- `/resume` and `/prepare` on a stateless thread are refused, with a test.
- A queued turn shows a queued state, and it clears when the turn starts.
- A permission request whose pod dies is retired rather than pending forever.
- `pytest tests/ -q` at the known baseline; `ruff check` + `ruff format --check`
  clean.
- Append decisions, deviations, measurements and failures to
  `docs/research/stateless_agents/implementation_log.md` as you go. Mark
  something DONE only when it is verified — an unverified DONE poisons the next
  session's assumptions.
- Update `docs/features/stateless_agents.md` §9.1 to match what you actually
  landed. That section is the project's status-of-record; leaving it stale is a
  defect.

---

## 7. What is NOT in scope

The worker/job lane. That work is on `feature/stateless-workers-s3` and is
blocked on a design problem: job completion needs durable command acceptance
plus a crash-recoverable idempotent finalizer before a worker driver can be
safe. Do not start it, do not enqueue a `worker_batch` unit, and do not build on
that branch — you would collide.

If you finish everything here, stop and report rather than starting S3.

---

## 8. Report back with

What you built, what you verified and how, what you decided differently from the
design doc and why, and what is still unverified. Numbers, not adjectives. If a
piece turns out to rest on a wrong premise in the doc — that has happened twice
already on this feature, both times caught by reading the call site instead of
the prose — say so plainly and stop rather than building on it.
