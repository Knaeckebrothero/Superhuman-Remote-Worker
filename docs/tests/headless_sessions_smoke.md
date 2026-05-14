# Headless Persistent Sessions — Smoke Test Runbook

Manual end-to-end smoke for Phases 2–4 of `docs/features/headless_persistent_sessions.md`. Covers the load-bearing wires that unit tests with mocked Postgres do not exercise: real LISTEN/NOTIFY, real CAS UPDATE, real SSE replay, real magic-link round-trip.

Target time: **30 minutes**. Run after Phase 4 lands, before stacking Phase 5 on top.

---

## 0. Prereqs

```bash
# Bring up the dev stack (postgres, mongo, neo4j, gitea, keycloak, ...)
podman-compose -f docker-compose.dev.yaml up -d

# Wait for postgres to be ready
until podman-compose -f docker-compose.dev.yaml exec postgres pg_isready -U srw; do
  sleep 1
done

# Apply schema + all migrations (0001 → 0006)
python init.py
```

Expected log lines from `init.py`:

```
Applying migration: 0004_thread_events.sql               OK
Applying migration: 0005_thread_permission_requests.sql  OK
Applying migration: 0006_headless_notifications.sql      OK
Applying migration: 0007_thread_pk_identity.sql          OK
Applying migration: 0008_thread_awaiting_user.sql        OK
```

If you see any `ERROR` line in the migration output, **stop here** — the migration syntax is the problem and the rest of the smoke is moot.

Set a convenience alias for the rest of the runbook:

```bash
alias psql_dev='podman-compose -f docker-compose.dev.yaml exec -T postgres psql -U srw -d srw'
```

---

## 1. Verify the new tables and trigger exist

```bash
psql_dev -c '\d thread_events'
psql_dev -c '\d thread_permission_requests'
psql_dev -c '\d magic_link_tokens'
psql_dev -c '\d thread_notifications'
psql_dev -c "\df notify_thread_permission_update"
psql_dev -c "SELECT tgname FROM pg_trigger WHERE tgname = 'thread_permission_notify_trigger';"
psql_dev -c '\d+ threads' | grep events_epoch
```

**Pass criteria:**
- All four tables listed without error.
- `notify_thread_permission_update` function shown.
- `thread_permission_notify_trigger` row returned.
- `threads.events_epoch` column present with `INTEGER NOT NULL DEFAULT 0`.

---

## 2. LISTEN/NOTIFY round-trip (Phase 3)

The keystone wire. We INSERT a pending row, UPDATE it, and verify the trigger fires on the global channel.

In **terminal A**, open a LISTEN session:

```bash
psql_dev -c "LISTEN thread_permission_updates; SELECT pg_sleep(60);"
```

In **terminal B**, drive an INSERT + UPDATE (use any valid thread_id — see `SELECT id FROM threads LIMIT 1` or create one in Section 4):

```bash
THREAD_ID=$(psql_dev -t -c "SELECT id FROM threads LIMIT 1;" | tr -d ' ')

# If no threads yet, create one:
if [ -z "$THREAD_ID" ]; then
  THREAD_ID=$(psql_dev -t -c "
    INSERT INTO threads (config_name, status)
    VALUES ('interactive', 'active')
    RETURNING id;
  " | tr -d ' ')
fi

REQ_ID=$(psql_dev -t -c "
  INSERT INTO thread_permission_requests
  (thread_id, tool_call_id, tool_name, tool_args)
  VALUES ('$THREAD_ID', 'tc-smoke', 'run_command',
          '{\"cmd\": \"ls /tmp\"}'::jsonb)
  RETURNING id;
" | tr -d ' ')

echo "Inserted request: $REQ_ID"

psql_dev -c "
  UPDATE thread_permission_requests
  SET status = 'approved', decided_at = now(), decided_by = 'smoke-test'
  WHERE id = '$REQ_ID';
"
```

**Pass criteria:** Terminal A should print a NOTIFY line like:

```
Asynchronous notification "thread_permission_updates" with payload
"{"id": "...", "thread_id": "...", "status": "approved"}" received from server process with PID ...
```

If you see no NOTIFY in terminal A but the UPDATE succeeded → the trigger isn't firing, **stop**.

---

## 3. Magic-link generation (sidestepped-auth shortcut)

Generate a token directly via the service module, store it, then hit the routes.

In **terminal C**, start the orchestrator:

```bash
uvicorn orchestrator.main:app --reload --port 8085
```

In **terminal D**, use Python's async REPL to mint a token directly (bypasses the auth check):

```bash
python -c "
import asyncio, hashlib, secrets
from datetime import datetime, timezone, timedelta
import asyncpg

THREAD_ID = '$THREAD_ID'
REQ_ID = '$REQ_ID'

async def main():
    pool = await asyncpg.create_pool(
        'postgresql://srw:srw_password@localhost:5432/srw',
        min_size=1, max_size=1,
    )
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    async with pool.acquire() as conn:
        await conn.execute('''
          INSERT INTO magic_link_tokens
          (token_hash, purpose, approval_id, thread_id,
           intended_decision, expires_at)
          VALUES (\$1, 'approve_permission', \$2, \$3, 'approved', \$4)
        ''', h, REQ_ID, THREAD_ID, expires)
    print(f'TOKEN={raw}')
    await pool.close()

asyncio.run(main())
" | tee /tmp/smoke-token.txt
```

Extract the token:

```bash
TOKEN=$(grep '^TOKEN=' /tmp/smoke-token.txt | cut -d= -f2)
echo "Token: $TOKEN"
```

---

## 4. GET shows confirmation page, POST consumes

First create a fresh pending request and token (the one from step 2 is already approved):

```bash
# New pending request
REQ_ID=$(psql_dev -t -c "
  INSERT INTO thread_permission_requests
  (thread_id, tool_call_id, tool_name, tool_args)
  VALUES ('$THREAD_ID', 'tc-magic', 'run_command',
          '{\"cmd\": \"rm -rf /important\"}'::jsonb)
  RETURNING id;
" | tr -d ' ')

# New token for it (re-run the python snippet from §3 with the new REQ_ID).
```

### 4a. GET — confirmation page

```bash
curl -i "http://localhost:8085/magic/approve/$TOKEN"
```

**Pass criteria:**
- HTTP 200.
- Body is HTML containing the tool name (`run_command`) and arguments (`rm -rf /important`).
- A `<form method="POST">` element pointing back at the same URL.
- **No** `UPDATE` happens on the DB. Verify:
  ```bash
  psql_dev -c "SELECT used_at FROM magic_link_tokens WHERE token_hash = '$(echo -n "$TOKEN" | sha256sum | cut -d' ' -f1)';"
  # Should print NULL.
  ```

### 4b. POST — consumes and resolves

```bash
curl -i -X POST "http://localhost:8085/magic/approve/$TOKEN"
```

**Pass criteria:**
- HTTP 200, body contains "approved" + "request has been approved".
- `magic_link_tokens.used_at` is now set:
  ```bash
  psql_dev -c "SELECT used_at, consumed_decision FROM magic_link_tokens WHERE approval_id = '$REQ_ID';"
  ```
- `thread_permission_requests.status` is `'approved'`:
  ```bash
  psql_dev -c "SELECT status, decided_by FROM thread_permission_requests WHERE id = '$REQ_ID';"
  ```

### 4c. Double-click protection

POST the same token again:

```bash
curl -i -X POST "http://localhost:8085/magic/approve/$TOKEN"
```

**Pass criteria:** HTTP 409 or 404 with a friendly "already used / expired" page. The DB state is unchanged from 4b.

---

## 5. Expired token rejected

```bash
# Mint an already-expired token directly.
psql_dev -c "
  INSERT INTO magic_link_tokens
  (token_hash, purpose, intended_decision, expires_at)
  VALUES ('expired-hash', 'approve_permission', 'approved',
          now() - interval '1 hour');
"

curl -i "http://localhost:8085/magic/approve/anything-that-hashes-to-expired-hash"
# Will 404 because the hash doesn't match anything real. The validate
# path is checked separately:
```

For the real path: generate a normal token, then UPDATE expires_at in the past:

```bash
TOKEN=$(...)  # rerun the §3 mint
psql_dev -c "
  UPDATE magic_link_tokens
  SET expires_at = now() - interval '1 minute'
  WHERE token_hash = '$(echo -n "$TOKEN" | sha256sum | cut -d' ' -f1)';
"
curl -i "http://localhost:8085/magic/approve/$TOKEN"
```

**Pass criteria:** HTTP 404, "Link expired or already used" body. `used_at` stays NULL.

---

## 6. Notification watcher emits (no SMTP)

The watcher logs an attempt regardless of whether SMTP is configured. Without `SMTP_HOST` set, the result will be `skipped_smtp` but a row should still appear in `thread_notifications`.

Wait ~30 seconds after creating a fresh pending request (the watcher polls every 30s).

```bash
# Fresh pending request
REQ_ID=$(psql_dev -t -c "
  INSERT INTO thread_permission_requests
  (thread_id, tool_call_id, tool_name, tool_args)
  VALUES ('$THREAD_ID', 'tc-watcher', 'run_command', '{}'::jsonb)
  RETURNING id;
" | tr -d ' ')

# Wait for watcher (next tick within 30s + processing slack).
sleep 45

# Check
psql_dev -c "
  SELECT kind, delivery_status, sent_at
  FROM thread_notifications
  WHERE request_id = '$REQ_ID';
"
```

**Pass criteria:** One row with `kind = 'permission_pending'`. `delivery_status` is one of:
- `skipped_no_email` — if the test thread has no `user_id` (most likely on a fresh dev DB)
- `skipped_smtp` — if SMTP_HOST is unconfigured
- `sent` — if you set up SMTP_HOST and the test thread has a user with an email
- `failed` — if SMTP barfed; check orchestrator logs

In all cases, the watcher noticed the pending request and ran its dedup/rate-limit/context-load chain to completion.

---

## 7. Dedup: watcher doesn't double-email

The watcher should NOT emit a second row for the same request_id:

```bash
sleep 30  # next watcher tick
psql_dev -c "
  SELECT COUNT(*) FROM thread_notifications WHERE request_id = '$REQ_ID';
"
```

**Pass criteria:** Still 1 (not 2). Watcher's dedup query (`NOT EXISTS`) is working.

---

## 8. SSE replay (Phase 2)

Verify `Last-Event-ID` replay works. First, manually insert some events so we have something to replay:

```bash
# Get a thread (use the same THREAD_ID).
for i in 1 2 3 4 5; do
  psql_dev -c "
    INSERT INTO thread_events (thread_id, epoch, seq, kind, payload)
    VALUES ('$THREAD_ID', 0, $i, 'token',
            '{\"content\": \"chunk-$i\"}'::jsonb);
  "
done
```

### 8a. Replay from cursor

```bash
# Note: this endpoint requires auth in production. For smoke,
# either patch out require_approved_user temporarily, or use the
# orchestrator's MCP-token path (X-MCP-Token header).
curl -N -H "Last-Event-ID: 0:2" \
     "http://localhost:8085/api/persistent/threads/$THREAD_ID/stream" &
CURL_PID=$!
sleep 2
kill $CURL_PID 2>/dev/null
```

**Pass criteria:** Output contains exactly three SSE frames with `id: 0:3`, `id: 0:4`, `id: 0:5` (seq > 2). The first two are skipped because the cursor was `0:2`.

### 8b. Epoch mismatch → GONE_BEYOND_HORIZON

```bash
curl -N -H "Last-Event-ID: 99:0" \
     "http://localhost:8085/api/persistent/threads/$THREAD_ID/stream" &
CURL_PID=$!
sleep 2
kill $CURL_PID 2>/dev/null
```

**Pass criteria:** Single SSE frame with `event: gone_beyond_horizon` and a JSON payload showing the current `(epoch, server_seq)`. Connection closes immediately after.

---

## 9. Teardown

```bash
psql_dev -c "
  DELETE FROM thread_permission_requests WHERE tool_call_id LIKE 'tc-%';
  DELETE FROM thread_events WHERE thread_id = '$THREAD_ID';
  DELETE FROM thread_notifications WHERE thread_id = '$THREAD_ID';
  DELETE FROM magic_link_tokens WHERE thread_id = '$THREAD_ID';
"

# If you created the thread for this smoke, drop it too:
# psql_dev -c "DELETE FROM threads WHERE id = '$THREAD_ID';"
```

---

## Phase 5 sections (attention sleep, magic-link wake, extend window)

These verify Phase 5 plumbing without standing up a real LangGraph loop. Run after §1 has confirmed migration 0008 is applied.

### P5.1 — Verify migration 0008 landed

```bash
psql_dev -c "
  -- New columns
  SELECT column_name FROM information_schema.columns
   WHERE table_name='threads'
     AND column_name IN ('awaiting_user_since','extend_count');
  -- Predicate index on awaiting_user_since
  SELECT indexname FROM pg_indexes
   WHERE tablename='threads'
     AND indexname='idx_threads_awaiting_user_since';
  -- CHECK constraint now allows awaiting_user / suspended
  SELECT pg_get_constraintdef(c.oid)
    FROM pg_constraint c JOIN pg_class t ON c.conrelid=t.oid
   WHERE t.relname='threads' AND c.conname='valid_thread_status';
"
```

**Pass criteria:** two column rows, one index row, and the constraint string contains `'awaiting_user'` and `'suspended'`.

### P5.2 — Status endpoint accepts awaiting_user, rejects suspended

```bash
# awaiting_user — should 200 and set awaiting_user_since
curl -s -X PUT -H "Content-Type: application/json" \
  -d '{"status":"awaiting_user"}' \
  "http://localhost:8085/api/agents/threads/$THREAD_ID/status"

psql_dev -c "SELECT status, awaiting_user_since IS NOT NULL AS ts_set,
                    extend_count FROM threads WHERE id='$THREAD_ID';"

# Second call — awaiting_user_since must NOT change (idempotent)
TS_BEFORE=$(psql_dev -At -c "SELECT awaiting_user_since FROM threads WHERE id='$THREAD_ID';")
sleep 1
curl -s -X PUT -H "Content-Type: application/json" \
  -d '{"status":"awaiting_user"}' \
  "http://localhost:8085/api/agents/threads/$THREAD_ID/status"
TS_AFTER=$(psql_dev -At -c "SELECT awaiting_user_since FROM threads WHERE id='$THREAD_ID';")
[ "$TS_BEFORE" = "$TS_AFTER" ] && echo "OK: timestamp preserved" || echo "FAIL: timestamp moved"

# Revert to active — clears the timer fields
curl -s -X PUT -H "Content-Type: application/json" \
  -d '{"status":"active"}' \
  "http://localhost:8085/api/agents/threads/$THREAD_ID/status"
psql_dev -c "SELECT status, awaiting_user_since IS NULL AS cleared,
                    extend_count FROM threads WHERE id='$THREAD_ID';"

# suspended must be rejected
curl -s -o /dev/null -w "%{http_code}\n" \
  -X PUT -H "Content-Type: application/json" \
  -d '{"status":"suspended"}' \
  "http://localhost:8085/api/agents/threads/$THREAD_ID/status"
```

**Pass criteria:** awaiting_user transitions set timestamp; second write preserves it; active revert clears both fields and resets extend_count to 0; `suspended` returns `400`.

### P5.3 — Attention-sleep watchdog suspends stale threads

The default TTL is 60 min, too slow to wait for during a smoke. We drop it to 0 so the very next sweeper tick (within 60s) suspends anything currently in `awaiting_user`.

In **terminal C** (the orchestrator window), stop the running process (Ctrl+C) and restart with the env override:

```bash
HEADLESS_ATTENTION_SLEEP_MINUTES=0 \
  uvicorn orchestrator.main:app --reload --port 8085
```

Watch the startup log for `Attention-sleep sweeper started (interval=60s, ttl=0min)` — that confirms the env took effect.

In **terminal D**:

```bash
# Force a stale awaiting_user state. The watchdog will see it on its
# next tick (every 60s) and call suspend_thread_workspace.
psql_dev -c "
  UPDATE threads
     SET status='awaiting_user',
         awaiting_user_since = now() - interval '5 minutes'
   WHERE id='$THREAD_ID';
"

echo 'Watching terminal C for the suspend log line (up to 75s)...'
sleep 75

psql_dev -c "SELECT status FROM threads WHERE id='$THREAD_ID';"
```

**Pass criteria:** Terminal C log contains *either*:

- `attention-sleep: thread <uuid> suspended (was awaiting_user >0m)` — full success; thread.status is now `suspended`.
- `attention-sleep: suspend declined for thread <uuid> (workspace not ready or already suspending)` — also a pass for this smoke. The watchdog **did select** the row and call `suspend_thread_workspace()`; the service correctly refused because there's no live workspace pod (no S3 / no provisioner on dev). Thread status stays `awaiting_user` in this case.

**Reset:** Ctrl+C terminal C and restart without the env var before continuing to P5.4.

### P5.4 — Magic-link extend window

Mint a fresh token + pending request (the same Python snippet as §3 — adapted to use a new `REQ_ID` so we don't conflict with earlier-consumed tokens):

```bash
REQ_ID=$(psql_dev -t -c "
  INSERT INTO thread_permission_requests
  (thread_id, tool_call_id, tool_name, tool_args)
  VALUES ('$THREAD_ID', 'tc-extend', 'run_command',
          '{\"cmd\": \"sleep 100\"}'::jsonb)
  RETURNING id;
" | tr -d ' ')

python -c "
import asyncio, hashlib, secrets
from datetime import datetime, timezone, timedelta
import asyncpg
THREAD_ID = '$THREAD_ID'
REQ_ID = '$REQ_ID'
async def main():
    pool = await asyncpg.create_pool(
        'postgresql://srw:srw_password@localhost:5432/srw', min_size=1, max_size=1)
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    async with pool.acquire() as conn:
        await conn.execute('''
          INSERT INTO magic_link_tokens
          (token_hash, purpose, approval_id, thread_id,
           intended_decision, expires_at)
          VALUES (\$1, 'approve_permission', \$2, \$3, 'approved', \$4)
        ''', h, REQ_ID, THREAD_ID, expires)
    print(f'TOKEN={raw}')
    await pool.close()
asyncio.run(main())
" | tee /tmp/smoke-extend-token.txt
TOKEN=$(grep '^TOKEN=' /tmp/smoke-extend-token.txt | cut -d= -f2)
```

Now exercise extend:

```bash
# Set thread to awaiting_user so the extend has something to bump.
psql_dev -c "
  UPDATE threads SET status='awaiting_user',
                     awaiting_user_since = now() - interval '10 minutes',
                     extend_count=0
   WHERE id='$THREAD_ID';
"

# First extend — should bump timestamp + extend_count, render 'extended' banner.
curl -s -X POST "http://localhost:8085/magic/extend/$TOKEN" | grep -i "extended by 60 minutes" \
  && echo "OK: extend 1 banner" || echo "FAIL: no banner"
psql_dev -c "SELECT extend_count,
                    awaiting_user_since > now() - interval '1 minute' AS bumped
               FROM threads WHERE id='$THREAD_ID';"

# Click 3 more times — extend_count reaches 4 (cap).
for i in 2 3 4; do curl -s -X POST "http://localhost:8085/magic/extend/$TOKEN" > /dev/null; done
psql_dev -c "SELECT extend_count FROM threads WHERE id='$THREAD_ID';"

# Fifth click — UPDATE misses (extend_count >= cap), banner shows 'cap_reached'.
curl -s -X POST "http://localhost:8085/magic/extend/$TOKEN" | grep -i "Extend limit reached" \
  && echo "OK: cap banner" || echo "FAIL: no cap banner"

# Confirm extend did NOT consume the approval token.
psql_dev -c "SELECT used_at IS NULL AS still_unused FROM magic_link_tokens
              WHERE token_hash = encode(sha256('$TOKEN'::bytea), 'hex');"
```

**Pass criteria:** first POST shows "Window extended" banner; `extend_count = 1` and `awaiting_user_since` bumped to ~now; after the 4th click `extend_count = 4`; the 5th shows the cap banner; the approve token's `used_at` is still NULL throughout.

### P5.5 — Invalid token on extend returns 404

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST "http://localhost:8085/magic/extend/this-is-not-a-real-token"
```

**Pass criteria:** `404`.

### P5.6 — Magic-link approve fires wake task on suspended workspace

The wake task is fire-and-forget after the POST returns, so this step is purely log inspection. We simulate a suspended state directly (no S3 / no provisioner needed — `workspace_suspension_service.is_enabled` only requires the S3 client object, which the dev compose initializes even without buckets, so the helper at least reaches the `restore_thread_workspace` call before failing).

> **Dev-cluster heads-up.** On a wired K8s dev cluster (where `persistent_provisioner` is configured), the wake helper goes further than the docker-compose path: after logging `magic-link wake: restoring suspended workspace …` it calls `restore_thread_workspace`, which succeeds structurally even on a snapshot 404, then provisions a real **workspace pod + PVC + agent pod** for the test thread. The DB-revert in the cleanup snippet below does NOT delete those cluster resources — see [`docs/issues/headless_sessions_smoke_leaks_cluster_pods.md`](../issues/headless_sessions_smoke_leaks_cluster_pods.md) and the `kubectl delete` step at the end of this section. The pass criterion is still "log line fired"; everything past the log line is best-effort restore on the cluster and best-effort no-op on docker-compose.

Mint a fresh approve token + pending request:

```bash
REQ_ID=$(psql_dev -t -c "
  INSERT INTO thread_permission_requests
  (thread_id, tool_call_id, tool_name, tool_args)
  VALUES ('$THREAD_ID', 'tc-wake', 'run_command',
          '{\"cmd\": \"date\"}'::jsonb)
  RETURNING id;
" | tr -d ' ')

python -c "
import asyncio, hashlib, secrets
from datetime import datetime, timezone, timedelta
import asyncpg
THREAD_ID = '$THREAD_ID'
REQ_ID = '$REQ_ID'
async def main():
    pool = await asyncpg.create_pool(
        'postgresql://srw:srw_password@localhost:5432/srw', min_size=1, max_size=1)
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    async with pool.acquire() as conn:
        await conn.execute('''
          INSERT INTO magic_link_tokens
          (token_hash, purpose, approval_id, thread_id,
           intended_decision, expires_at)
          VALUES (\$1, 'approve_permission', \$2, \$3, 'approved', \$4)
        ''', h, REQ_ID, THREAD_ID, expires)
    print(f'TOKEN={raw}')
    await pool.close()
asyncio.run(main())
" | tee /tmp/smoke-wake-token.txt
TOKEN=$(grep '^TOKEN=' /tmp/smoke-wake-token.txt | cut -d= -f2)
```

Mark the thread workspace as suspended in metadata, then POST:

```bash
psql_dev -c "
  UPDATE threads
     SET status='suspended',
         metadata = jsonb_set(
           COALESCE(metadata, '{}'::jsonb),
           '{workspace_container,status}',
           '\"suspended\"'::jsonb
         )
   WHERE id='$THREAD_ID';
"

curl -s -X POST "http://localhost:8085/magic/approve/$TOKEN" | head -c 200
echo ""
echo "Check terminal C for 'magic-link wake: restoring' line (within ~1s)"
```

**Pass criteria:** Terminal C (orchestrator) log contains *either*:

- `magic-link wake: restoring suspended workspace for thread <uuid>` followed by either success or `workspace restore failed` — confirms the wake helper fired and reached the restore call.
- Nothing at all → FAIL (wake task didn't fire).

The POST itself returns 200 with "Tool approved" body regardless — the wake is fire-and-forget after the permission UPDATE.

**Cleanup:** revert thread back to a clean state:

```bash
psql_dev -c "
  UPDATE threads SET status='active',
                     awaiting_user_since=NULL,
                     extend_count=0,
                     metadata=metadata - 'workspace_container'
   WHERE id='$THREAD_ID';
"
```

**On a wired dev cluster, also delete the orphaned pods + PVC** the wake task spawned. The three resources all carry the first 8 chars of the thread UUID in their names — `grep` them out, then delete:

```bash
# Adjust namespace if different (production manifests use superhuman-remote-worker).
NS=superhuman-remote-worker

# List what got created so you can sanity-check before deleting.
kubectl -n "$NS" get pods,pvc | grep "${THREAD_ID:0:8}" || \
  echo "Nothing matched — probably running against docker-compose, skip the delete."

# Delete pods + PVC matching the short thread ID. --wait=false so we don't
# block on PVC finalizers.
kubectl -n "$NS" get pods,pvc -o name | grep "${THREAD_ID:0:8}" | \
  xargs -r kubectl -n "$NS" delete --wait=false
```

If you skip this step, each P5.6 run leaks 1 workspace pod, 1 PVC, and 1 agent pod into the namespace. They will not be picked up by the normal idle/suspension sweepers because the thread row is now `active` with no `workspace_container` metadata.

---

## Phase 7 (cockpit) sections — SSE migration

These verify the cockpit's WS→SSE migration shipped 2026-05-13. They run against a real cockpit dev server + the existing orchestrator (cluster or local uvicorn). Unit tests in `cockpit/src/app/core/services/persistent-chat.service.spec.ts` cover the dispatcher with mocked EventSource + fake IndexedDB; this section catches the wire-up concerns the mocks can't see.

Prereqs in addition to §0:

```bash
# Cockpit dev server on :4200
(cd cockpit && npm start)

# Confirm the orchestrator is reachable from your browser host (port-forwarded
# cluster orchestrator or local uvicorn).
curl -s http://localhost:8085/api/health | head -c 100
```

Sign into the cockpit (Keycloak SSO via the normal flow) before starting. Open browser devtools — you'll want the Network and Application tabs visible.

Pick a thread to target. Either pick an existing one and note its UUID, or create one:

```bash
THREAD_ID=$(psql_dev -t -c "SELECT id FROM threads WHERE status='active' LIMIT 1;" | tr -d ' ')
echo "Targeting thread $THREAD_ID"
```

### P7.1 — SSE stream attaches on thread open (no cached cursor)

Navigate to `http://localhost:4200/sessions/$THREAD_ID` (or whatever the cockpit's thread-detail route is for your shell — desktop layout uses `/sessions/<id>`, simple layout uses `/chat/<id>`).

**Pass criteria:**

- Network tab shows exactly one `GET /api/persistent/threads/<id>/stream` request, with response type `text/event-stream` and status `200`.
- The request URL has **no** `last_event_id` query parameter (first visit — IndexedDB has no row yet).
- Application → IndexedDB → `cockpit-cache` shows a `threadCursors` object store (created at schema v3) but it's empty until events arrive.
- Console has no errors.

### P7.2 — Cursor saved on each event

In another terminal, manually drive events to verify the cockpit saves the cursor:

```bash
psql_dev -c "
  UPDATE threads SET events_epoch = COALESCE(events_epoch, 0) WHERE id = '$THREAD_ID';

  INSERT INTO thread_events (thread_id, epoch, seq, kind, payload)
  VALUES
    ('$THREAD_ID', 0, 10001, 'token', '{\"content\": \"smoke-\"}'::jsonb),
    ('$THREAD_ID', 0, 10002, 'token', '{\"content\": \"chunk\"}'::jsonb);
"
```

Within ~200ms the cockpit's SSE poll picks them up.

**Pass criteria:**

- Application → IndexedDB → `threadCursors` now has a row keyed by the thread UUID with `epoch=0`, `seq=10002`, fresh `updatedAt`.
- The streaming text UI didn't add anything visible (token events without a `turn.started` are dispatched into `streamingText` but the surrounding chat panel doesn't render mid-turn text without a turn-started marker — that's expected; we're testing transport, not UX).

### P7.3 — Cursor replays on tab close + reopen

Close the tab (full close, not refresh). Reopen `http://localhost:4200/sessions/$THREAD_ID`.

**Pass criteria:**

- The new SSE request URL carries `?last_event_id=0%3A10002` (URL-encoded `0:10002`).
- The orchestrator picks up where we left off — no replay storm of old events.

This is the load-bearing path for "close browser, agent works, come back, see what happened."

### P7.4 — gone_beyond_horizon triggers history reload

Bump the thread's epoch on the server. The cached cursor (epoch=0) will now be invalid:

```bash
psql_dev -c "UPDATE threads SET events_epoch = events_epoch + 1 WHERE id = '$THREAD_ID';"
```

Disconnect the cockpit's SSE briefly (devtools Network → right-click the stream → "Block request URL", reload, unblock) so it reopens and re-sends the stale cursor.

**Pass criteria:**

- Network tab shows the SSE response includes an `event: gone_beyond_horizon` frame and the connection closes.
- A new `GET /api/persistent/threads/<id>/messages` request fires immediately after (cockpit's transcript reload).
- A second SSE request fires after that, this time **without** `?last_event_id=` (cursor was dropped).
- IndexedDB → `threadCursors` for this thread is gone for a beat, then re-populates with the new epoch as fresh events arrive.

### P7.5 — POST /input replaces WS send for messages

Type a message in the chat composer and submit.

**Pass criteria:**

- Network tab shows `POST /api/persistent/threads/<id>/input` with body `{"content": "<your message>"}` and status 200.
- The user's message renders optimistically in the chat panel.
- **No** outbound WS frame with `method: "message"` (devtools → Network → WS tab → click the connection → Messages — should NOT see your text as an outgoing frame).

### P7.6 — POST /interrupt replaces WS send for interrupt

Trigger an interrupt mid-turn (the UI button while the agent is streaming, or wait until streaming and then click).

**Pass criteria:**

- Network tab shows `POST /api/persistent/threads/<id>/interrupt` with empty body and status 200.
- Subsequent `interrupt.ack` arrives over the SSE (not WS).
- No outgoing WS frame with `method: "interrupt"`.

### P7.7 — Control WS still handles slash commands + permission decisions

The cockpit retains a WebSocket strictly for control-plane verbs the migration left on WS by design. Send `/done` in the composer (or click approve/deny on a permission request).

**Pass criteria:**

- The cockpit's WS to `/ws/persistent/<id>` is open (Network → WS).
- The outgoing frame is `{"method": "archive"}` (for /done) or `{"method": "approve"}` / `{"method": "deny"}` (for permissions).
- The corresponding server response (e.g. session ended, permission resolved) arrives over **SSE**, not WS.

### P7.8 — 409 on concurrent multi-tab send is gracefully swallowed

Open the same thread in two tabs. In both, type the same (or different) message and submit at nearly the same moment.

**Pass criteria:**

- Both tabs show their message added optimistically.
- One of the `POST /input` returns 200, the other returns 409.
- The 409 tab's error signal stays clear (the cockpit treats 409 as "server has the turn, ignore" per the migration design).

### P7.9 — Teardown

```bash
psql_dev -c "
  DELETE FROM thread_events
  WHERE thread_id = '$THREAD_ID' AND seq >= 10001;
"
```

Reset the events_epoch if you bumped it for P7.4:

```bash
psql_dev -c "UPDATE threads SET events_epoch = 0 WHERE id = '$THREAD_ID';"
```

In the cockpit devtools Application tab, you can also clear the `threadCursors` store entirely if you want a clean next-run baseline.

---

## Common failure modes

| Symptom | Likely cause |
|---|---|
| Migration 0004/5/6 syntax error | SQL dialect issue — likely `BIGSERIAL` vs `BIGINT GENERATED BY DEFAULT AS IDENTITY` or a stray `;` in the trigger function |
| Migration 0008 fails with constraint conflict | Existing rows have `status='idle'` somehow — backfill to `'ended'` first (migration 0002 already did this, but a manual UPDATE could have re-introduced) |
| NOTIFY never arrives in §2 | Trigger function not installed, or condition `NEW.status <> OLD.status` failed because OLD is NULL on something other than UPDATE |
| GET magic-link returns 500 | Likely HTMLResponse import or `urllib.parse.quote` argument |
| POST magic-link consumes but agent doesn't wake | Agent's add_listener didn't register, OR the agent's connection isn't the same pool that received the NOTIFY |
| Watcher never emits | The NOT EXISTS subquery on `thread_notifications` is wrong, OR `requested_at < now() - ...` interval cast issue |
| SSE replay returns empty | `Last-Event-ID` header parsing rejecting the input (we tolerate malformed by treating as "no cursor") |
| P5.3 watchdog never suspends | Workspace context missing on thread metadata → `suspend_thread_workspace` returns False early. Either provision a workspace for the test thread or accept the "suspend declined" log line as proof the watchdog selected the row. |
| P5.4 extend POST returns 200 but no banner | The token's `thread_id` may be NULL (mint was missing the bind argument). Check `SELECT thread_id FROM magic_link_tokens WHERE token_hash = …` |
| P5.6 wake task silent | Either `workspace_suspension_service.is_enabled` is False, or the thread's metadata doesn't carry a `workspace_container` section — both abort the wake helper early. |
| P7.1 SSE request has no cursor when one is cached | The `getThreadCursor` Dexie read returned null on schema upgrade — check Application → IndexedDB for `cockpit-cache` schema version (should be ≥ 3). If stuck at v2, the user's browser kept the old DB; `DELETE FROM cockpit-cache` (devtools) and reload. |
| P7.3 reload sends `last_event_id=` but server returns no events | Cursor seq is ahead of the actual `MAX(seq)` on the server (manual DELETE between sessions). Falls into the "no replay needed" branch silently — not a bug per se, but if you expected replay you need to re-INSERT events with seq > the cursor. |
| P7.4 epoch bump doesn't trigger `gone_beyond_horizon` | The cockpit's EventSource is still in the same in-flight response when you bump — the server only re-evaluates the cursor on a fresh connection. Force a reconnect (block-request-URL trick in devtools, or `reconnectNow()` from the service). |
| P7.5 message goes over WS, not REST | The cockpit pre-2026-05-13 build is cached. Hard-reload (Ctrl+Shift+R) or clear the service worker if one is registered. |

When a step fails, capture the orchestrator logs (terminal C) and `psql_dev -c "SELECT * FROM thread_notifications ORDER BY sent_at DESC LIMIT 5;"` for triage.
