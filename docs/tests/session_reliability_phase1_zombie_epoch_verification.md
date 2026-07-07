# Session reliability — Phase 1 (kill the zombie epoch) — Verification Runbook

Cluster-level verification for **Phase 1** of
`docs/features/session_reliability_and_transport_simplification.md`. Covers the
three acceptance criteria that can't be exercised by unit tests because they
need a live agent + browser: **#5 synthetic epoch bump**, **#6 real
suspend/resume**, **#7 multi-turn smoke**.

**Status (2026-07-06):** Phase 1 implemented on `develop` (uncommitted),
**unit-verified** (`tests/test_thread_events_phase2.py::TestThreadEventStreamEpochRecheck`,
4/4) and **live-synced to both orchestrator replicas via Tilt**. Both new SQL
queries validated against the live schema. This runbook is the not-yet-run
cluster exercise.

## What the fix does (one line)

`orchestrator/main.py` `thread_event_stream` now re-reads `events_epoch` on its
own poll loop after ~`THREAD_EVENTS_EPOCH_RECHECK_S` (default 2.0s) of
**accumulated idle time**; if the epoch changed (an agent re-attached), it emits
one `gone_beyond_horizon` (`reason: epoch_bumped_mid_stream`, re-anchored to the
new epoch's last completed turn) and closes, instead of polling the dead old
epoch forever while its keepalive pings fool the client watchdog into believing
the stream is healthy (the "stale → refresh to fix" bug).

## The one mechanic you must internalize before testing

The re-check fires **only while the stream is idle** (empty polls). While a turn
is actively streaming, rows arrive every poll, the idle accumulator resets, and
the epoch is never re-read. This is by design (the "live old-epoch writer"
residual in the feature doc). **Consequence for testing: bump the epoch when the
stream is idle** — i.e. *after* a turn has fully completed, not while tokens are
flowing. A bump applied mid-stream is simply detected later, once the agent
stops emitting and the stream goes quiet for ~2s.

## Prerequisites

1. k3d up, Tilt running, orchestrator carrying the fix (both replicas):
   ```bash
   for p in $(kubectl --context=k3d-srw -n srw get pods -o name | grep srw-orchestrator); do
     echo -n "$p: "; kubectl --context=k3d-srw -n srw exec "$p" -c orchestrator -- \
       grep -c THREAD_EVENTS_EPOCH_RECHECK_S /app/main.py
   done   # expect: 2 on each pod
   ```
2. Browser at `https://localhost/`, logged in `test`/`test` (admin).
3. Two terminals free: one to tail **both** orchestrator replicas, one for psql.

### Tail both replicas (critical — the SSE stream lands on only one)

Traefik routes each `/stream` request to one of the two orchestrator pods, so
the `epoch bump` log line appears on **only that pod**. `kubectl logs
deploy/srw-orchestrator` tails just one pod and will miss it half the time. Tail
both, prefixed:

```bash
for p in $(kubectl --context=k3d-srw -n srw get pods -o name | grep srw-orchestrator); do
  ( kubectl --context=k3d-srw -n srw logs "$p" -c orchestrator -f --tail=0 \
      | sed "s#^#${p##*/}: #" ) &
done
# stop with: kill $(jobs -p)
```

Watch for: `thread_event_stream epoch bump N→N+1 (thread=… ), re-anchoring client to seq …`

### psql helper

```bash
pg() { kubectl --context=k3d-srw -n srw exec srw-postgres-0 -- \
         sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "'"$*"'"'; }
```

---

## #5 — Synthetic epoch bump (server-side proof)

Proves the generator detects a DB epoch change on its own and re-anchors the
client, with no duplicate turn. This is the direct exercise of the new code.

**Split-epoch caveat — read first.** A psql bump changes the DB epoch but *not*
the live agent's in-memory epoch, so the agent keeps journaling under the old
epoch while the reopened client stream now reads the new one. After the bump the
session is intentionally desynced: **new frames will not render** until a real
re-attach. That is expected here (criterion #6 covers true streaming
continuity). Treat the bumped session as disposable — end it afterward.

### Procedure

1. In the cockpit, open a session and **complete at least one full turn** (send
   a message, let the reply finish streaming). Leave the tab open and idle.
   Capture its thread id (Sessions list URL, or):
   ```bash
   kubectl --context=k3d-srw -n srw get pods -l srw/purpose=session \
     -o jsonpath='{range .items[*]}{.metadata.labels.srw\.io/thread-id}{"\n"}{end}'
   TID=<thread-id>
   pg "SELECT id,status,events_epoch FROM threads WHERE id='$TID';"   # note the epoch, call it N
   ```
2. With the tab idle (no turn running), bump the epoch:
   ```bash
   pg "UPDATE threads SET events_epoch = events_epoch + 1 WHERE id='$TID';"
   ```
3. Watch the log tail and the browser.

### Pass / fail

| Observable | PASS (fix present) | FAIL (old behavior) |
|---|---|---|
| Orchestrator log within ~2.5s | one `epoch bump N→N+1 … re-anchoring client to seq …` line | nothing; the stream keeps pinging the dead epoch |
| Network tab (`/stream`) | the request receives a `gone_beyond_horizon` frame, then closes; cockpit opens a fresh `/stream` | no new frame; same request lingers, only `event: ping` arrives |
| Chat transcript | unchanged — **no duplicated turn, no "SESSION RESUMED" divider** | (n/a on old build — it never re-anchors) |
| Console | no error spam | — |

### Results — 2026-07-07 · server-side PASS

Run against the **develop-experimental main-cluster deploy** (context `main`,
namespace `superhuman-remote-worker`) — *not* local k3d. Both orchestrator
replicas (`srw-orchestrator-6666b657cf-{d2hhn,srkdl}`, ~28 min old at test time)
carry the fix (`grep -c THREAD_EVENTS_EPOCH_RECHECK_S /app/main.py` matched; the
28-day-old `srw-prod-private` orchestrator has **0** matches, i.e. no fix — do
not test #5 there). This is a stronger result than the runbook assumed: the fix
works on a real multi-replica homelab deploy, not just k3d.

- **Session:** `7495ffe7-c55c-48ae-a595-93d087f998b8`, `status=active`, one
  completed turn, tab left idle (user-driven browser).
- **Pre-bump epoch:** `0`. Bump issued `15:52:32Z`
  (`UPDATE threads SET events_epoch = events_epoch + 1` → `UPDATE 1`).
- **Detection:** `15:52:33.079Z` — **~1 s later**, well inside the ~2.5 s idle
  re-check window. Exactly one line, on exactly the one replica serving the
  stream (`d2hhn`):
  > `thread_event_stream epoch bump 0→1 (thread=7495ffe7-…-f998b8), re-anchoring client to seq 0` (`main.py:18097`, `request_id=9d2c3f78ef83`)
  `re-anchor to seq 0` is correct here: the freshly-bumped epoch 1 has no
  journaled events yet, so `_no_cursor_replay_start` anchors at 0.
- **Post-bump epoch:** `1` ✅ (generator detected the DB change on its own).
- **No duplicate turn:** `thread_events` for the thread = **1071 events all under
  epoch 0** (`min_seq 1 … max_seq 1071`), **zero under epoch 1** — nothing
  spurious was journaled, so a history reload renders the same single turn. This
  is the server-side proof of the "Chat transcript unchanged" row.

**Server-side rows PASS** (orchestrator log + re-anchor + no dup). The three
browser-side rows (network `/stream` receives one `gone_beyond_horizon` frame
then closes → fresh `/stream` opens; console clean; transcript visually
unchanged) are to be eyeballed in the user's develop-cockpit tab. Per the
split-epoch caveat, **no new frames will render until a real re-attach** — that
is expected for a synthetic psql bump (criterion #6 covers true streaming
continuity). Treat this session as disposable.

### Cleanup

End the session from the cockpit (the split-epoch state makes it unsuitable for
continued use). If you'd rather keep it, revert the DB first — but only if no
real re-attach happened since: `pg "UPDATE threads SET events_epoch=<N> WHERE id='$TID';"`

---

## #6 — Real suspend/resume (the actual user-facing bug)

This is the symptom the user reported: *"the system gets stale, you send a
message, the stream doesn't arrive, you have to refresh."* Unlike #5 it uses a
**real** epoch bump (a genuine agent re-attach), so the whole pipeline — new
agent journaling under the new epoch, old stream detecting the bump, client
re-anchoring, new frames flowing — is exercised end to end. No split-epoch
caveat here.

The load-bearing step is triggering a real re-attach while **keeping the browser
tab (and its now-stale SSE stream) open**. Most reliable trigger: force the
agent pod away, then drive activity from the stale tab so a new agent attaches
and bumps the epoch.

### Procedure

1. Open a session, complete a turn, note `TID`. **Keep the tab open** for the
   rest — never refresh; a refresh masks the bug by reopening the stream.
2. Force the agent to detach (pick one):
   - **Delete the agent pod** (cleanest): find it by thread label and delete it.
     ```bash
     AP=$(kubectl --context=k3d-srw -n srw get pods -l srw/purpose=session \
            -o jsonpath="{range .items[?(@.metadata.labels.srw\.io/thread-id=='$TID')]}{.metadata.name}{end}")
     kubectl --context=k3d-srw -n srw delete pod "$AP"
     ```
   - or **idle-timeout** the session (wait for the boot/idle reaper to collect
     the pod), or **workspace tier upgrade** — anything that ends with a *new*
     agent attaching to the thread and re-running the attach-time epoch init.
3. From the **stale tab** (still open, not refreshed), send a message. This
   drives re-provisioning; the new agent attaches, bumps `events_epoch`
   (confirm: `pg "SELECT events_epoch FROM threads WHERE id='$TID';"` is now
   > the earlier value), and journals the reply under the new epoch.

### Pass / fail

| Observable | PASS (fix present) | FAIL (old behavior) |
|---|---|---|
| Reply after the stale-tab send | **streams in live, no refresh needed** | nothing renders; only a manual page refresh brings the stream back |
| Orchestrator log | `epoch bump …` on the stale stream's replica, then the client reopens and frames flow | stale stream keeps pinging; no bump line |
| DB epoch | increased (real re-attach) | increased, but the client never noticed |

PASS on the first row is the whole point of Phase 1.

### Results — 2026-07-07 · user-facing PASS, with a mechanism caveat

Run on the **same session as #5** (`7495ffe7-…-f998b8`, develop-experimental
deploy), chained straight off the synthetic bump. Timeline from the orchestrator
logs:

| Time (UTC) | Event |
|---|---|
| `15:40:33–41` | session provisioned, attached to `srw-agent-j-6a54e6a5`, first turn completed |
| `15:52:33` | #5 synthetic bump `0→1` detected; client re-anchored to epoch 1 |
| `16:13:21` | **workspace container deleted** — the idle-timeout suspend (waited ~33 min) |
| `16:20:01–09` | **workspace container re-created** — stale-tab send drove genuine re-provisioning (resume) |

- **Row 1 (the headline): PASS.** Genuine suspend + genuine resume, and the reply
  streamed in **live with tool calls, no refresh** — user-observed. The reported
  "stale → must refresh" bug did **not** reproduce end to end.
- **Row 3 (DB epoch increased): did NOT hold as written.** The **real re-attach
  did *not* bump `events_epoch`** — it stayed at `1` (the value #5's synthetic
  bump set). The only `epoch bump` log line in the whole 45 min is the 15:52:33
  synthetic one; **no second bump** on the 16:20 re-attach.
- **Row 2 (bump on the stale stream's replica): did NOT occur** — no re-anchor
  frame was needed. Instead the new agent journaled the reply **under epoch 1**
  (that epoch went 0 → **1208 events**), and the client — already anchored on
  epoch 1 from #5 — received those frames directly in its open stream.

**Verdict:** the user-facing recovery is a real PASS (real suspend, real resume,
live stream, no refresh). But because this was chained off #5, the run **rode
#5's re-anchor** and did **not** re-isolate the epoch-change *detection* path.
#5 already proved detection in isolation; #6 here proves end-to-end recovery —
together they cover Phase 1, but not via the exact "stale stream detects a fresh
real bump" sequence the table above assumes.

**Why the re-attach didn't bump — resolved from code (`src/api/persistent_app.py:1536–1570`).**
On attach the agent runs:

```
current_epoch = SELECT events_epoch FROM threads
max_seq       = SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE epoch = current_epoch
if max_seq > 0:  events_epoch += 1     # cold-restart: previous epoch has events → strand old cursors
else:            adopt current_epoch    # empty epoch → no bump
```

i.e. **a re-attach bumps the epoch iff the current epoch already holds journaled
events.** In this run, #5's synthetic bump left epoch 1 **empty** (the old agent
kept writing to epoch 0), so at the 16:20 re-attach `max_seq(epoch=1) = 0` → the
`else` branch → the new agent adopted epoch 1 with **no bump**. That is the sole
reason the epoch stayed at 1, and it's a pure artifact of chaining #6 onto #5 —
**not** a gap in the fix.

**Consequence for a clean run:** on a **FRESH** session (no prior synthetic
bump), the re-attach sees `max_seq(epoch=0) > 0` (the completed turn's events) →
bumps `0→1` → strands the old stream on the dead epoch 0 → the Phase 1 periodic
re-read detects `0→1` → `gone_beyond_horizon` → client re-anchors → frames flow.
So a fresh #6 **does** exercise the full detection handoff the table above
assumes — *in principle*. In practice, see the clean-run attempt below.

### Results — 2026-07-07 · clean run (attempt 2) · reproduction blocked by cluster behavior

Fresh session `507a472e-…-fadde6`, clean `epoch 0` with **314 events** — the
ideal starting state. Deleted its agent pod `srw-agent-j-d86c5081` (default grace
period) to force the suspend, then sent turn-2 from the open tab. It **streamed
live** — but *not* via a re-attach:

- Persistent agents get a **long termination grace window**. `d86c5081` stayed
  alive ~3 min (`16:42`→`16:45`) and **served turn-2 itself** — events grew
  314 → **1099 all under epoch 0**, `max(created_at) 16:45:04`. **No re-attach
  line, no `Bumped events_epoch`, epoch stayed 0.** The old agent's in-memory
  epoch 0 was intact, so it just journaled onto it.
- It died at `16:45:12` → a `GET /connection 425` ("Too Early") storm → and at
  **`16:47:59` the orchestrator *released* the session**: `Workspace snapshot
  captured … before release` + `Workspace container deleted: ws-thread-507a472e`.
  The workspace container is gone and there is **no open SSE `/stream`**.

**Why the literal #6 sequence is hard to force here (the real lesson).** On this
deploy, agent death → the SSE stream **closes** and the session **fully releases**
(workspace snapshotted + torn down) → the client reconnects *fresh* on whatever
epoch is current. A stale stream is therefore **never left stranded on a dead
epoch** by a pod deletion. The zombie-epoch condition Phase 1 fixes is a
**provisioning race** (`persistent_app.py:1526–1532`: a client opens the SSE
stream *during* provisioning, then the attach-time bump-check supersedes its
epoch) — **which is exactly what #5's synthetic psql bump reproduces**, and #5
passed cleanly (epoch change detected mid-stream in ~1 s, re-anchor emitted, no
dup turn).

Two independent gotchas defeated a clean pod-deletion #6, in order: (1) attempt 1
rode #5's empty-epoch-1 (no bump on adopt); (2) attempt 2 hit the graceful-
termination window (old agent served the message). A third try would need
`kubectl delete pod --grace-period=0 --force` **and** still wouldn't strand the
stream (it closes on agent death) — so it would confirm the agent-side
`Bumped events_epoch` line but likely still not the orchestrator-side re-anchor,
because the client reconnects fresh rather than staying stranded.

**Verdict for #6.** *User-facing* recovery is verified **twice** (both real
suspend/resume attempts streamed live with **no refresh** — the reported bug did
not reproduce). The *internal* detection path — the actual code Phase 1 adds — is
authoritatively proven by **#5**, since the re-read compares `events_epoch`
regardless of *how* it changed. Chasing the literal "real-bump strands an open
stream" sequence via pod deletion is **impractical on this cluster and adds
marginal value over #5**; do not sink more budget into it. If a future run wants
it, force a genuine **provisioning race** (open the stream mid-provision), not a
post-turn pod kill.

---

## #7 — Multi-turn steady-state smoke (no false positives)

Confirms the idle re-check doesn't fire spuriously during normal use.

### Procedure

1. Start the both-replica log tail (above), grepping for the new lines:
   ```bash
   for p in $(kubectl --context=k3d-srw -n srw get pods -o name | grep srw-orchestrator); do
     ( kubectl --context=k3d-srw -n srw logs "$p" -c orchestrator -f --tail=0 \
         | grep -E "epoch bump|gone_beyond_horizon" | sed "s#^#${p##*/}: #" ) &
   done
   ```
2. In one session, run **5–8 turns** back to back, with a few multi-second idle
   gaps between some of them (to exercise the idle poll path without any bump).
3. Do not touch the DB epoch.

### Pass / fail

| Observable | PASS | FAIL |
|---|---|---|
| Log grep over the whole session | **zero** `epoch bump` / `gone_beyond_horizon` lines | any spurious line (the re-check is misfiring on an unchanged epoch) |
| Chat | every turn renders once, streams normally | duplicated turns / dropped streams |

### Results — 2026-07-07 · PASS (stronger than spec)

Session `f1f9675d-…-4d59c35` (develop-experimental deploy, `gemma-4-31b`),
**45.5 min** span (`16:54:47`→`17:40:17`), **3 completed turns** (`turn.completed`
×3, all fully streamed — 2384 `token` + 2340 `thinking` events).

- **Epoch never moved:** `events_epoch = 0`, all **4750** journal events under a
  single epoch 0 (`seq 1…4750`).
- **Zero spurious lines:** a 120-min grep of **both** orchestrator replicas for
  `epoch bump` / `gone_beyond_horizon` / `re-anchor` scoped to this thread returned
  **nothing**.
- **Idle stress far beyond the spec's "few multi-second gaps":** the largest
  inter-turn gaps were **1800 s (30 min)**, 446 s, and 265 s. The 30-min gap even
  fired a `session.idle_timeout` event (also journaled under epoch 0) — yet no
  re-attaching turn followed it before `session.ended`, so nothing bumped the
  epoch. During those idle stretches the stream was open (4 `ready` events) and
  the ~2 s idle re-check ran hundreds of times **without a single misfire**.

**Verdict: PASS.** Fewer turns than the runbook's 5–8 (3 here), but the idle path
— the actual thing #7 guards against false-firing — was exercised *harder* than
the spec (a 30-min idle + a real idle-timeout vs. multi-second gaps) and stayed
silent. Note `/stream` never appears in the access logs (long-lived SSE isn't
per-request logged) — same as observed in #5/#6; absence there is not evidence the
stream was closed.

---

---

## Phase 2 live criteria (client — send-kickstart, wake recovery)

Phase 2 (`persistent-chat.service.ts`) is the **client** half of the same
"stale stream" bug: it makes the cockpit *notice* a dead receive path instead of
waiting for a refresh. Unit-verified (`persistent-chat.service.spec.ts`,
11 new tests). Two criteria need a browser:

### P2-#1 — Send-kickstart reconnects a silently-severed stream

Simulates: the SSE socket is dead but the browser still thinks it's OPEN; the
user sends anyway.

1. Open a session, complete a turn. Open DevTools → Network, find the live
   `/stream` request.
2. Kill the receive path *without* the client noticing — the reliable way is
   server-side: `kubectl --context=k3d-srw -n srw delete pod <the orchestrator
   replica serving the stream>` (find it via the both-replica tail; the SSE will
   silently stop delivering, but `readyState` stays OPEN until the OS keepalive
   trips). Do **not** toggle DevTools "Offline" (that fires `onerror` and the
   existing reconnect path handles it — not what we're testing).
3. Immediately type and send a message.
4. **PASS:** within ~5–6s of the send's POST `200`, a new `/stream` request opens
   and the reply streams in live, **no duplicated text**, no refresh.
   **FAIL (pre-P2):** the composer clears but nothing streams; only a manual
   refresh recovers.

### P2-#8 — Background-tab wake resumes frames

1. Open a session, send a message that produces a long/slow reply (so a turn is
   active), then background the tab (switch to another app/tab) for **>45s**.
2. Return to the tab.
3. **PASS:** frames resume within ~1s of focus (the visibility/`pageshow`/
   `resume` wake path forces a revalidate; a socket that died while the tab was
   frozen is reopened even though it reported OPEN). **FAIL (pre-P2):** the turn
   looks frozen until a manual refresh.

Tip: also worth a quick check that **normal** short tab-switches (<45s) do
*not* cause a visible reconnect blip — the wake path only forces a reopen past
the 45s watchdog window.

---

## Phase 3 live criteria (client — the outbox / "Creating thread" swallow)

Phase 3 (`persistent-chat.service.ts` + `persistent-chat.component.*`) is the
categorical fix for the **original bug #1**: a message typed while the card says
"Creating thread" is no longer swallowed. The queue is now owned by user intent
(an `outbox`), so it survives the `disconnect()` that thread-creation runs
internally. Unit-verified (10 tests in `persistent-chat.service.outbox.spec.ts`
+ 3 updated in the main spec). Two criteria need a browser:

### P3-#7 — Send on the "Creating thread" card is never lost

1. From the Sessions list, click **New Session** and, the instant the card shows
   "Creating thread…", type a message and hit **Enter** (before "assigning
   agent" appears — the exact window that used to swallow it).
2. **PASS:** the message appears immediately as a **muted bubble with a clock
   avatar** (queued style), stays through provisioning, then — once the agent is
   ready — un-mutes, POSTs exactly once, and the reply streams. After a **hard
   reload**, the message renders **exactly once** (not zero, not doubled).
   **FAIL (pre-P3):** the bubble flickers in and vanishes; nothing is sent.
3. Bonus check (the send-once guarantee): watch the orchestrator log / Network
   tab — exactly one `POST …/input` for that message, even though creation ran a
   `disconnect()`/`connect()` in between.

### P3-#8 — Two rapid sends during startup both queue and flush in order

1. New Session, then hit **Enter** twice quickly with two different messages
   while it's still starting (both land before ready).
2. **PASS:** both show as queued (clock) bubbles in order, then flush **FIFO**
   with one POST at a time once ready — two turns, correct order, no collision.
   **FAIL (old):** the second overwrites the first (single-slot), or they
   collide on the same turn_id and one is dropped behind a 409.

Failure-mode spot checks (optional, via DevTools "Offline" toggle mid-send):
a **503/network** error keeps the bubble + shows the banner and does **not**
auto-retry (no double-send) — the next send retriggers the flush; a **404/410**
(e.g. the thread was deleted) drains the queue and removes the bubbles.

---

## Phase 4 live criteria (client — de-flicker generation)

Phase 4 attacks bug #2 (flicker during generation): streamed deltas coalesce on
an 80ms timer (one CD pass per burst instead of per token), DOM post-processing
(code-collapse, copy buttons, KaTeX) is gated off the still-streaming block, and
scroll pinning re-checks intent at fire time. Service coalescing is unit-tested
(6 new tests); the rendering effects are browser-verified. Use a **long-turn
fixture**: a reply with **5 fenced code blocks and a `$$…$$` math expression**.

### P4-#7 — DOM enhancements never attach mid-stream, appear right after

While the turn streams, in DevTools watch `details.code-collapse` /
`.code-copy-btn` counts:
- **PASS:** counts are **monotonically non-decreasing**; no collapse wrapper or
  copy button ever exists inside an element with class `.streaming-block`; each
  block gets its collapse/button within a frame of *that block* finishing (not
  only at turn end). **Also test with the turn manually collapsed mid-stream**
  (click the turn's chevron while it streams) — the final-answer path must stay
  gated too (this is the vacuous-pass trap the review caught).
- **FAIL (pre-P4):** wrappers/buttons flicker in and out every token; the whole
  turn visibly reflows on each delta.

### P4-#8 — KaTeX renders once, on completion

- **PASS:** the `$$…$$` shows as **raw text** while the block streams (no KaTeX
  DOM), then typesets **once** when the block completes — no per-token height
  jump. **FAIL:** the equation re-typesets every token, jumping the layout.

### P4-#9 — Scroll pinning respects intent

- Pinned to bottom during a stream → stays pinned smoothly, no jumps.
- **Wheel up mid-stream** → the view stays where you scrolled; the next token
  must **not** yank you back to the bottom (the fire-time `autoScroll` re-check).
  Scroll back to the bottom → auto-pin resumes.

### P4-#10 — Reflow reduction (optional, timeboxed)

DevTools Performance: record a token burst on `develop` vs this branch over the
same fixture. Expect a **visibly reduced** layout/recalc-style count. Don't sink
the whole verification budget chasing an exact "≥10×" — "visibly reduced + trace
attached" is enough.

## Notes & gotchas

- **Two replicas.** Repeated everywhere above because it's the most likely way to
  "see nothing and conclude it's broken." The stream + its log line live on one
  pod; always tail both.
- **Idle-only detection is a feature, not a miss.** If you bump mid-stream and
  see the log line only after the turn finishes, that's correct (the doc's
  "live old-epoch writer" residual). Don't file it as a bug.
- **ngsw / stale bundle.** If the cockpit behaves like an old build (e.g. never
  reopens `/stream`), the service worker may be serving a cached bundle:
  DevTools → Application → unregister `ngsw-worker.js`, clear caches, hard
  reload. (README troubleshooting has the full snippet.)
- **Self-signed cert.** `https://localhost/` uses the local Traefik cert; accept
  it once per browser profile.
- **The client needed zero changes for Phase 1.** The existing
  `gone_beyond_horizon` handler already reloads history + reopens; #5/#6 are
  really testing that the *server* now emits that frame on its own. If the client
  mishandles the frame, that's a pre-existing issue, not a Phase 1 regression.
- **Phase 1 does not fix the *client's* failure to notice a silently-dead
  socket** (that's Phase 2's send-kickstart + wake-recovery). So a socket killed
  at the TCP layer while the tab is hidden may still look stuck until Phase 2
  lands — keep the two phases distinct when interpreting results.
