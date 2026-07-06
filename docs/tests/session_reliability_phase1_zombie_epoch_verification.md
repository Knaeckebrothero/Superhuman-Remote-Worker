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
