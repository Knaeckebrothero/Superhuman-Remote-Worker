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

## Common failure modes

| Symptom | Likely cause |
|---|---|
| Migration 0004/5/6 syntax error | SQL dialect issue — likely `BIGSERIAL` vs `BIGINT GENERATED BY DEFAULT AS IDENTITY` or a stray `;` in the trigger function |
| NOTIFY never arrives in §2 | Trigger function not installed, or condition `NEW.status <> OLD.status` failed because OLD is NULL on something other than UPDATE |
| GET magic-link returns 500 | Likely HTMLResponse import or `urllib.parse.quote` argument |
| POST magic-link consumes but agent doesn't wake | Agent's add_listener didn't register, OR the agent's connection isn't the same pool that received the NOTIFY |
| Watcher never emits | The NOT EXISTS subquery on `thread_notifications` is wrong, OR `requested_at < now() - ...` interval cast issue |
| SSE replay returns empty | `Last-Event-ID` header parsing rejecting the input (we tolerate malformed by treating as "no cursor") |

When a step fails, capture the orchestrator logs (terminal C) and `psql_dev -c "SELECT * FROM thread_notifications ORDER BY sent_at DESC LIMIT 5;"` for triage.
