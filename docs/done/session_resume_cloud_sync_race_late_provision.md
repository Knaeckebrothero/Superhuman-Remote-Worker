# Resume races its own cloud-folder provisioning — session runs unsynced for life

**Status**: DONE — shipped and live on the dev cluster 2026-08-05.
**Filed**: 2026-08-04
**Observed on**: main dev cluster, thread `5833c729-c0cd-496f-9a40-e9b811ae0ced`
**Severity**: the toast was *accurate* — the affected session really did run with
cloud sync off for its entire life, and nothing recovered it.

Four defects, one thread of investigation:

| # | Defect | Where |
|---|--------|-------|
| 1 | Resume's attach beats its own cloud provisioning → session unsynced for life | Fix, below |
| 2 | A degraded attach was permanent — no rebuild path existed | Follow-up 1 |
| 3 | Ended session had no composer; the draft was unreachable | Follow-up 2 |
| 4 | Stale-epoch replay pinned the ended chrome over a live session | Follow-up 3 |

Plus two smaller ones fixed in passing: the danger toast rendered at 20% alpha
over the artifact pane, and `cloudSyncDegraded` rendered *nowhere* — the
dismissible toast was the only sign a session was running unsynced.

### Deployed state (verified in-cluster 2026-08-05)

| Component | Image | Carries |
|-----------|-------|---------|
| orchestrator | `sha-dbc6ec9` | attach gate — `_await_late_cloud_setup` present in `main.py` (3×) and `routers/sessions.py` (2×) |
| agent | `sha-03442f8` | `_retry_cloud_sync_start` + `workspace_sync.recovered`, confirmed inside a freshly-provisioned pod |
| cockpit | `sha-03442f8` | `resumedFromEpoch`, `endedSendResumes`, `cloudSyncOff` all present in the served bundle/i18n |

**Operational caveat**: long-lived session pods keep the agent image they booted
with. `persistent-d67ee261-334` (the Better Resavio officer, 6d uptime) still
runs pre-fix agent code and will until it is recycled — the turn-boundary sync
recovery does not apply to it.

## Symptom

Resuming an ended session pops a sticky red toast:

> Cloud sync could not start for this session. The workspace may be missing files
> from the cloud, and changes won't be saved back to it. Cloud sync could not be
> set up for this session (no sync target was provisioned).

Yet querying the thread a few minutes later shows a perfectly healthy sync target:

```
$ curl -H "X-Internal-Key: $MCP_INTERNAL_KEY" \
    localhost:8085/api/agents/threads/5833c729-.../workspace
"cloud_sync": { "version": 2, "session_folder": {
    "backend": "nextcloud",
    "webdav_url": ".../dav/files/agent-service/sessions/5833c729/" }, ... }
"cloud_sync_degraded": false
```

The message is not stale UI. It was true at the instant the agent read it, and it
stays true for that session because the agent never re-reads.

## Root cause — a lost race inside `POST /api/persistent/threads/{id}/resume`

`resume_thread` (`orchestrator/main.py:25200`) kicks off **two detached tasks**
and returns:

| line | task | what it does | measured cost |
|------|------|--------------|---------------|
| `25340` | `_late_cloud_setup` | MKCOL the session folder over WebDAV, resolve the cloud user, `update_thread_main_cloud()` | **~5.1 s** |
| `25449` | `_reprovision` | advisory lock → a few DB reads → `_send_session_attach` to an idle pool agent | **~90 ms** |

The agent, once attached, immediately calls `GET /api/agents/threads/{id}/workspace`
to fetch its cloud config. That read happens ~150 ms after resume — roughly **5 s
before** `_late_cloud_setup` writes the handle to the DB. This is not a flaky
race: the attach path is pure DB work and the provisioning path is several WebDAV
round-trips, so the attach wins every time.

Timeline from the orchestrator log (all `request_id 86f26cce6848` = the resume call):

```
08:45:39.735  POST /resume 200 (48ms)
08:45:39.827  Assigned thread ... to persistent agent d6bb700c (10.42.3.27:8001)
08:45:39.831  Thread ...: resumed via idle pool agent srw-agent-j-6a226135
08:45:39.990  GET /api/agents/threads/.../workspace 200   ← agent reads config (handle still NULL)
08:45:40.100  GET /api/agents/threads/.../workspace 200   ← ditto
08:45:40.769  MKCOL .../agent-service/sessions          → 405 (exists)
08:45:42.463  MKCOL .../agent-service/sessions/5833c729 → 201 Created
08:45:42.463  Created session folder: sessions/5833c729
08:45:42.824  GET /api/agents/threads/.../workspace 200   ← still NULL: folder made, DB not written
08:45:44.853  ★ workspace_sync.error "no sync target was provisioned"
08:45:44.877  Thread ...: late-provisioned cloud session folder   ← DB write finally lands
08:45:47.293  ready
```

Note the endpoint gates on the **DB column**, not on the folder existing, so even
the 08:45:42.82 read (after `201 Created`) still reported degraded.

### Why the read returns degraded

`agent_get_thread_workspace` (`orchestrator/main.py:21168`):

```python
cloud_sync_degraded = bool(
    _cloud_up                                   # Nextcloud is up  → True
    and not cloud_mount_cfg                     # no rclone mount   → True (see below)
    and not cloud_sync_cfg                      # _build_agent_cloud_sync → None (no handle)
    and (metadata.get("protected_cloud") or not thread.get("nc_session_folder"))
)
```

`_build_agent_cloud_sync` (`:23874`) resolves the session folder from
`main_cloud_session_handle or nc_session_folder`; both NULL at that instant →
returns `None`. `cloud_mount_cfg` is None independently because this is a
`virtual` (lite) workspace: `_runtime_supports_rclone_mount` (`:23958`) requires
`workspace_container.status == "ready"`, and a lite thread has no workspace pod.

### Why the agent never recovers

`src/api/persistent_app.py:2143` takes the `elif cloud_degraded_hint:` branch,
broadcasts the toast, and leaves `_session.workspace_sync = None`. Every later use
is guarded by `if _session.workspace_sync:` (`:2321`, `:5035`, `:5166`, `:6719`) and
there is **no rebuild path**. So the turn-boundary push/pull is silently skipped for
the whole session even though the sync target existed 5 seconds later.

## Why the handle was missing in the first place

This thread was created 2026-08-01, before the dev cluster's OpenCloud → Nextcloud
migration. `update_thread_main_cloud` (`orchestrator/database/postgres.py:6335`)
writes `nc_session_folder = NULL` for any backend that isn't `nextcloud`, and the
OpenCloud backend was down (502 for ~25 h) so `_late_cloud_setup` returned early at
`:25298` without persisting anything. The thread reached 2026-08-04 with both
handle columns NULL.

Session **create** does not have this bug — `create_thread` *awaits*
`asyncio.gather(_setup_gitea(), _setup_main_cloud())` at `:23650` before the agent
attaches. Only **resume** detaches it.

## Blast radius

```sql
SELECT count(*) FROM threads;                                        -- 217
SELECT count(*) FROM threads
 WHERE main_cloud_session_handle IS NULL AND nc_session_folder IS NULL; -- 215
SELECT count(*) FROM threads WHERE main_cloud_share_handle IS NULL;  -- 217
```

**215 of 217 threads have no session handle**, so each one takes the
`needs_full_provision` branch on its next resume and loses the same race. Only 7
`workspace_sync.error/provision` events exist so far (across 2 threads) because
`cloud_sync_degraded` also requires `_cloud_up` — the OpenCloud outage suppressed
the signal. Now that Nextcloud is live, this fires on the first resume of every one
of those 215 threads. It is one-shot per thread (the handle persists afterwards),
but it costs that entire session's cloud sync.

## Adjacent finding: the session folder is never shared to the user

`main_cloud_share_handle IS NULL` for **all 217 threads**. Nextcloud currently has
exactly two accounts:

```
$ curl -u admin:… /ocs/v2.php/cloud/users?format=json
{"users":["admin","agent-service"]}
```

The user `knaeckebrothero` has no Nextcloud account, so
`resolve_user_identity_cached` returns `None` and `share_session_folder` is never
called. The folder is created under `agent-service`'s files and is invisible to the
user. Note the asymmetry: the **create** path calls `backend.ensure_user(...)`
(`:23626`), which *provisions* the account; the **resume** path calls
`resolve_user_identity_cached` (`:25310`), which only *looks it up* — so resume can
never bootstrap the account, and `needs_share_only` will keep retrying-and-failing
on every future resume without persisting anything (`:25317-25321`).

## The fix (shipped)

A registry + gate, mirroring the `_schedule_protected_engage` pattern the
protected-cloud path already uses for the same class of engage-vs-attach race:

- `orchestrator/main.py` — `_late_cloud_setup_tasks` registry,
  `_register_late_cloud_setup()`, and `_await_late_cloud_setup()` (bounded by
  `LATE_CLOUD_SETUP_ATTACH_TIMEOUT_S = 15`, using `asyncio.shield` so a waiter
  that gives up does not cancel provisioning).
- `resume_thread` registers the task **only when `needs_full_provision`** —
  the share-only retry leaves both handle columns untouched, so gating on it
  would add its cloud user-lookup cost to every resume of a thread whose share
  never landed (i.e. every thread, until its owner signs into the cloud once)
  for no change in what the agent can resolve.
- Both attach paths await the gate before binding an agent, outside the thread
  advisory lock (holding it across the wait would stall the fresh pod's own
  `POST /api/agents/register`, which takes the same lock):
  `resume_thread._reprovision` and `orchestrator/routers/sessions.py::_do_prepare`.

`POST /resume` still returns in ~20 ms — the wait is in the background attach
task, not the request. A full provision now costs the resume the provisioning
time (~5 s on dev, ~3 s on k3d) before the agent binds. That is the intended
trade: a few seconds of resume latency, once per legacy thread, instead of a
whole session with no cloud sync.

Tests: `tests/test_late_cloud_setup_gate.py` (8 cases — ordering contract,
no-op when unregistered, failure isolation, timeout, shield-does-not-cancel,
registry slot discipline) and
`tests/test_sessions_router_prepare.py::test_do_prepare_waits_for_cloud_folder_before_binding_an_agent`
(gate → lock → provision ordering).

### k3d verification

Reproduced the exact pre-fix state on a thread (`490c07b8`): NULLed
`nc_session_folder` / `main_cloud_session_handle` / `main_cloud_share_handle`
and deleted the Nextcloud folder, then resumed. Orchestrator log:

```
09:50:22  POST /resume 200 (19ms)
09:50:24  MKCOL .../sessions/490c07b8 → Created session folder
09:50:25  Shared session folder 'sessions/490c07b8' with user 'admin' (share_id=56)
09:50:25  Thread 490c07b8...: late-provisioned cloud session folder   ← DB write
09:50:25  Agent pod created: srw-agent-s-0fb48916                     ← bind AFTER
09:50:31  GET /api/agents/threads/490c07b8.../workspace 200
```

Agent pod log: `Cloud workspace sync coordinator started (1 mount(s))` — the
`if cloud_cfg:` branch, not the degraded one. Zero `workspace_sync.error` rows
on the thread; endpoint reports `cloud_sync_degraded: false`.

## Follow-up 1: recovery from a degraded attach (shipped)

The race fix removes the *common* cause, but any other one (cloud down at
attach, a transient WebDAV failure) still pinned `workspace_sync = None` for
the session's whole life. Now:

- `src/api/persistent_app.py` — `_cloud_sync_retry_pending` is set at both
  degraded-attach sites (failed initial pull, and the no-target branch).
  `_retry_cloud_sync_start()` runs from `_loop_on_turn_start` once per turn
  while pending, re-resolves cloud config, rebuilds the coordinator and seeds
  it with a pull. Silent after the first failure — the toast already fired at
  attach; only the transition back to working is announced, via a new
  `workspace_sync.recovered` frame. Fail-closed on `protected_cloud` (F-C1):
  never hand a protected thread a live WebDAV sync, and stop retrying, since
  that verdict cannot change mid-session. A failed pull does NOT install the
  coordinator — otherwise the turn-end push would run against a mount already
  known to be broken. Cleared at attach and teardown so a pending retry can't
  leak between threads on a pool agent.
- `cockpit` — handles `workspace_sync.recovered`: clears `cloudSyncDegraded`,
  dismisses the sticky toast **by its stored id** (rather than clearing every
  toast on screen), and confirms with a success toast.
- `cockpit` — a status-bar `Cloud sync off` badge bound to `cloudSyncDegraded`.
  That signal previously rendered *nowhere*: the dismissible toast was the only
  indication a session was running unsynced, so dismissing it erased all trace.

Tests: `tests/test_cloud_sync_retry_after_degraded_attach.py` (9 cases) and a
cockpit spec asserting the recovered frame clears the flag.

### k3d verification

Pinned a thread to the `opencloud` backend (not configured on k3d) with NULL
handles so no target could resolve — the agent attached degraded, as intended.
Live DOM on `https://localhost/`:

```
tone: danger
backgroundColor: rgb(194, 178, 148)                          ← opaque --surface-2
backgroundImage: linear-gradient(rgba(156,40,50,0.15), …)    ← tint as a LAYER
badges: [gemma-4-moe, Turn 0, "Cloud sync off", Supervised, …]
```

Then made the target resolvable mid-session (backend back to `nextcloud`,
handles restored, folder created) and sent one turn:

```
13:38:23  workspace_sync.error      (degraded attach)
13:39:40  workspace_sync.recovered  {turn_id: 1}
13:39:40  workspace_sync.pulling    {turn_id: 1}
13:39:41  workspace_sync.pulled
13:39:44  workspace_sync.pushing
```

Agent log: `Cloud workspace sync recovered on turn 1 (1 mount(s))`. Post-turn
DOM: zero toasts, no `Cloud sync off` badge. A session that would have run
unsynced for its whole life was fully syncing from its first turn.

## Follow-up 2: draft-while-ended, send-to-resume (shipped)

The whole composer block was removed from the DOM on `threadStatus === 'ended'`
(an `@if` around `.composer-wrap`, not merely the `[disabled]` on the textarea —
the disabled binding never got a chance to matter). The draft survived in
`sessionStorage`, so text reappeared on resume, but during the outage it was
unreachable: not editable, not even visible.

Now the composer renders on an ended session and `canComposeDuringSession` gains
two terms — `isEnded` (draft freely) and `isResuming` (don't blink out mid-send;
`isStartingSession` tests `threadStatus !== 'ended'` and so goes false during the
handover). **Typing never resumes.** Resume reserves an agent pod and a
workspace, so it stays strictly send-triggered: `sendMessage` queues into the
outbox as always, then branches to `resumeSession()` instead of `_flushOutbox()`,
and the message rides the resume exactly like a landing-draft's first message
rides thread creation — `connect()` preserves the outbox on a same-thread
reconnect and `markSessionReady` flushes it. The placeholder says so
(*"Type a message — sending resumes the session"*).

The resume card stays, at the tail of the transcript directly above the composer,
as the resume-without-typing path; only its body copy changed to stop implying
the button is the only way back. `isResuming` moved from a component-local signal
to the service, since a send-triggered resume has no click to hang a flag off.

Known edge: the attach and mic buttons stay gated on `isConnected`, so on an
ended session you can type but not attach — uploads genuinely need a live
session. The card is the "resume first, then attach" path.

Tests: 3 new `canComposeDuringSession` cases, plus service specs for
send-on-ended (resume fired, `/input` NOT called, message left in the outbox)
and for open-without-send (no `/resume`).

### k3d verification

On an ended session: composer present and enabled, placeholder correct, card
present. Typed a full message — thread stayed `ended`, no `/resume` or
`/prepare`, **zero session agent pods**. Pressed Enter:

```
POST /api/persistent/threads/490c07b8…/resume
POST /api/sessions/490c07b8…/prepare
PUT  /api/agents/threads/490c07b8…/status      ← agent attached
POST /api/persistent/threads/490c07b8…/input   ← queued draft flushed
```

`thread_messages` then holds the drafted text as `human` with the agent's reply
after it; composer back to its normal placeholder, input cleared, card gone.
Full cockpit suite: 1661 passed.

## Follow-up 3: ended UI pinned over a live session (shipped)

Reported after Follow-up 2 shipped: send-to-resume worked, the session came back
and streamed — but the end marker, resume card and *"sending resumes the
session"* placeholder stayed on screen for the rest of the session (seen on dev
`5833c729` at Turn 7, mid-stream).

**Pre-existing bug, newly easy to hit.** The RESUME button takes the identical
`resumeSession()` path; Follow-up 2 only made the wrong state permanently
visible (before, the composer was absent so there was less to look wrong).

Mechanism. A resume reopens the SSE *before* the agent attaches, so the client
is still anchored to the thread's OLD epoch. The stream generator polls

```sql
SELECT seq, kind, payload FROM thread_events
 WHERE thread_id=$1 AND epoch=$2 AND seq > $3 ORDER BY seq ASC LIMIT 500
```

and happily streams that epoch's remaining tail — whose last rows are
`session.idle_timeout` + `session.ended`. `_handleSseFrame` dispatches them with
no epoch check, so `threadStatus` is set back to `'ended'` *after*
`loadThreadMeta` had correctly read `created`. The epoch bump is only noticed
later, on the idle recheck (`THREAD_EVENTS_EPOCH_RECHECK_S`), which emits
`gone_beyond_horizon`; the client re-anchors and live frames flow again — but
`_handleGoneBeyondHorizon` reloads *history* only, never thread meta, and
nothing else re-reads status. Result: live streaming session, ended chrome,
forever.

Two guards, both client-side:

- **Resume watermark.** `resumeSession()` records the epoch it is resuming
  *from* (read from the persisted cursor) before anything reconnects;
  `session.ended` / `session.idle_timeout` frames whose `id:` epoch is at or
  below it are dropped as describing a session life already superseded. A frame
  with no parseable id is always applied — never swallow a live end. The
  watermark is deliberately **not** cleared in `disconnect()` (which `connect()`
  calls first, and would therefore wipe it before the replay it exists to
  suppress); it clears on a genuine thread switch instead.
- **Self-heal on re-anchor.** `_handleGoneBeyondHorizon` now also awaits
  `loadThreadMeta`. An epoch bump means a new agent attached, so any `'ended'`
  the client is holding is stale by definition — this recovers a view that
  already applied a replayed frame.

Tests: replayed-vs-live terminal frames after a resume, id-less frame still
applied, and the re-anchor status refresh. Full cockpit suite 1743 passed.

Verified on k3d only as a **regression check** — send-to-resume still clears the
ended chrome end-to-end (card, end marker and placeholder all gone once the
agent is up, drafted message delivered). The replay ordering itself was not
reproduced live: it needs a thread whose epoch tail carries journaled
`session.*` rows *and* a client cursor sitting behind them, and the available
k3d thread ended mid-push without ever journaling those rows. That path is
covered by the unit tests above rather than a live run.

## Deferred — deliberately not built (nothing here blocks closing this doc)

**The session folder is never shared to the user until they sign in once.**
Confirmed root cause, and it is not fixable server-side: `NextcloudBackend.ensure_user`
returns `resolve_user_identity(...)` with the docstring "Nextcloud's user_oidc
app provisions on first login; no admin API." Verified on dev — after the owner
signed into `cloud.srw.works`, the OIDC account appeared
(`9edad2a0f55b…`, `backend: user_oidc`, matched by the email search the
resolver uses), and the very next resume logged
`Shared session folder 'sessions/00ae0977' with user '9edad2a0f55b…'` and
persisted `main_cloud_share_handle`. The 214 threads still without a folder
will get one, plus a share, on their next resume.

What's missing is telling a *new* user that: today an unsigned-in owner gets a
silent share-retry on every resume forever and an invisible cloud folder. Worth
surfacing as "your cloud folder isn't shared yet — sign in once" rather than
building anything server-side. Deferred until there are users beyond the owner;
it is a product affordance, not unfinished work on any of the four defects
above. Split out so it stays in the backlog rather than holding this doc open:
`docs/issues/cloud_folder_invisible_until_owner_signs_into_cloud.md`.

## Related UI defects from the same report (both fixed)

- **Danger toast was translucent over the artifact pane.** The tone variants
  assigned `background: var(--danger-tint)` — `rgba(204, 70, 71, 0.20)` dark,
  `rgba(156, 40, 50, 0.15)` light (`styles/themes/_theme-config.scss:152`/`:75`)
  — which *replaced* the opaque surface set by the base rule. The container is
  `position: fixed; bottom; right`, so it floats over the canvas/artifact pane
  and whatever sat behind bled through. The `*-tint` tokens are correct for
  their ~10 other callers, which are inline banners on an opaque page
  background; the toast is the only consumer floating over arbitrary content.
  Fixed by painting the tint as a **layer** —
  `background-image: linear-gradient(<tint>, <tint>)` — so the base rule's
  `background-color` survives underneath. Verified via live computed style:
  `backgroundColor: rgb(194, 178, 148)` with
  `backgroundImage: linear-gradient(rgba(156,40,50,0.15), …)`.
- **Composer was unusable while a session was ended-but-resumable.** See
  "Follow-up 2" above.
- **`cloudSyncDegraded` rendered nowhere.** Found while wiring Follow-up 1: the
  signal was set in the service and read only by its own spec, so a dismissible
  toast was the entire indication that a session was running unsynced — dismiss
  it and every trace was gone. Now drives a `Cloud sync off` status-bar badge.
