# Resume races its own cloud-folder provisioning — session runs unsynced for life

**Status**: FIXED (race), k3d-verified 2026-08-04 — uncommitted
**Filed**: 2026-08-04
**Observed on**: main dev cluster, thread `5833c729-c0cd-496f-9a40-e9b811ae0ced`
**Severity**: the toast is *accurate* — the affected session really does run with
cloud sync off for its entire life, and nothing recovers it.

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

## Still open

1. **The agent can't recover from a degraded attach.** Any *other* cause (cloud
   genuinely down at attach, a transient WebDAV failure) still pins
   `workspace_sync = None` for the session's whole life. It should re-resolve
   cloud config once at the next turn boundary, and the cockpit needs a matching
   event to clear `cloudSyncDegraded` and dismiss the sticky toast.
2. **The session folder is never shared to the user** (see above). Note the
   create-path fix does *not* transfer: `ensure_user` cannot provision Nextcloud
   accounts — each human must sign into the cloud once before any share can
   land. So the recovery path is the share-only retry on a later resume, which
   already exists; what's missing is surfacing "your cloud folder isn't shared
   yet, sign in once" to the user instead of silently retrying forever.

## Related UI defects seen in the same report

- **Toast is translucent over the artifact pane.** `toast-container.component.scss:51`
  sets `background: var(--danger-tint)` = `rgba(204, 70, 71, 0.20)` (dark theme,
  `styles/themes/_theme-config.scss:152`). The toast is `position: fixed; bottom;
  right`, so it floats over the canvas/artifact panel and the text behind bleeds
  through at 80%. Every other `--danger-tint` consumer is an inline banner on an
  opaque page background, where 20% alpha is fine — the toast is the one place it
  floats over arbitrary content and needs an opaque surface with the tint layered
  on top.
- **Composer is disabled while a session is ended-but-resumable.**
  `canComposeDuringSession` (`persistent-chat.component.ts:328`) returns
  `isConnected || isStartingSession || isDraftSession`. An ended session is none of
  those, so the `<textarea>` is `[disabled]` (`:1771`). The draft text survives (it
  lives in the `inputText` model, which is why it reappears after resume) but
  cannot be edited during the outage. The landing-draft state already supports
  "type first, session starts on send" — the ended/resumable state should get the
  same affordance.
