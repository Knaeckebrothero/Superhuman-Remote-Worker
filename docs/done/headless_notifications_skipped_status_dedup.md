# Headless notifications — skipped statuses don't block re-dispatch

**Status:** Resolved 2026-05-13. Sweeper SQL in `orchestrator/main.py:thread_permission_notify_sweeper` widened to include `skipped_no_email` and `skipped_already_resolved` in the permanent-suppression `IN (...)` set, plus a `make_interval(secs => $2)` recency floor (2 × sweeper interval) for the transient `skipped_rate_limit` / `skipped_smtp` cases. The in-process `already_notified()` probe was deleted — the sweeper SQL is now the authoritative dedup, and the standalone probe was duplicate logic that would drift apart. A new `record_notification` call was added on the `skipped_already_resolved` return path so the widened IN-set actually has a row to match against. Contract test pinning the new SQL shape lives in `tests/test_headless_notifications_phase4.py::TestPermissionNotifySweeperSQL`.

## Symptom (observed 2026-05-12 in Phase 2-4 smoke)

Three pending `thread_permission_requests` rows produced **13 `thread_notifications` rows over 3 minutes** during the smoke test. Each tick of `thread_permission_notify_sweeper` re-picked the same three requests and dispatched a fresh send attempt, even though the prior attempt had already returned a terminal verdict (no email on file, SMTP not configured, etc.).

```
$ psql_dev -c "SELECT request_id, delivery_status, sent_at FROM thread_notifications ORDER BY sent_at;"

 request_id |  delivery_status   |          sent_at
------------+--------------------+----------------------------
 req-A      | skipped_no_email   | 2026-05-12 11:42:03+00
 req-B      | skipped_no_email   | 2026-05-12 11:42:03+00
 req-C      | skipped_no_email   | 2026-05-12 11:42:03+00
 req-A      | skipped_no_email   | 2026-05-12 11:42:33+00     ← re-tried 30s later
 req-B      | skipped_no_email   | 2026-05-12 11:42:33+00
 req-C      | skipped_no_email   | 2026-05-12 11:42:33+00
 ... (continues every 30s for ~3min)
```

Expected: 3 rows, one per request, then quiet.

## Root cause

The sweeper's "needs notification" query in `orchestrator/main.py:11777`:

```sql
SELECT id, thread_id
FROM thread_permission_requests
WHERE status = 'pending'
  AND requested_at < now() - ($1 || ' seconds')::interval
  AND NOT EXISTS (
    SELECT 1 FROM thread_notifications tn
    WHERE tn.request_id = thread_permission_requests.id
      AND tn.kind = 'permission_pending'
      AND tn.delivery_status IN ('sent', 'failed')
  )
ORDER BY requested_at ASC
LIMIT 50
```

The `IN ('sent', 'failed')` filter only suppresses requests with a **terminal-delivery** outcome. Anything else — `skipped_dedup`, `skipped_rate_limit`, `skipped_no_email`, `skipped_smtp`, `skipped_already_resolved` — is not in that set, so the request remains eligible. Next tick, `send_permission_pending_email` runs again, hits the same skip condition, and writes a fresh `thread_notifications` row.

This is intentional for **`skipped_rate_limit`** — we want the request to become eligible again when the rate window resets. It is not intentional for the others:

| Status | Should retry? | Reason |
|---|---|---|
| `skipped_dedup` | N/A | already_notified should have caught this; if it didn't, retrying does nothing useful |
| `skipped_rate_limit` | **Yes** | retry after the limit window resets |
| `skipped_no_email` | No | the user doesn't have an email; retrying every 30s won't change that |
| `skipped_smtp` | Debatable | SMTP transient failures could heal, but in practice this is a config bug — back off, don't hammer |
| `skipped_already_resolved` | No | the request was decided before the email went out — moot forever |

## Impact

- **DB bloat**: each pending request that can't be delivered writes ~120 rows/hr to `thread_notifications` (every 30s × 2 send attempts per request for approve+deny… actually one row per send call, but rate-limited dispatches still double when the rate limit lifts). Across active threads this scales linearly.
- **Log noise**: each tick re-logs the skip reason at INFO, polluting the orchestrator log.
- **Future analytics rot**: anyone trying to chart "how many notifications did we send" needs to know to filter out skipped_* duplicates — easy to forget, easy to draw wrong conclusions.
- **No user-facing impact**: skipped means skipped — no double-emails ever reach a real inbox. This is a hygiene bug, not a correctness bug.

## Fix

Two-step approach, both small.

### Step 1 — Widen the suppression set

Add the three non-retry-worthy skip statuses to the dedup filter:

```sql
AND NOT EXISTS (
  SELECT 1 FROM thread_notifications tn
  WHERE tn.request_id = thread_permission_requests.id
    AND tn.kind = 'permission_pending'
    AND tn.delivery_status IN (
      'sent', 'failed',
      'skipped_no_email',       -- permanent: user has no email
      'skipped_already_resolved' -- permanent: decision is final
    )
)
```

Leave `skipped_dedup`, `skipped_rate_limit`, `skipped_smtp` out for now — they each have a defensible "try again later" story.

### Step 2 — Add a `last_attempted_at` floor on the others

For `skipped_rate_limit` / `skipped_smtp`, don't retry until a few ticks have passed — say, the most recent skip is older than `2 × interval_s`. Cheap predicate:

```sql
  AND NOT EXISTS (
    SELECT 1 FROM thread_notifications tn2
    WHERE tn2.request_id = thread_permission_requests.id
      AND tn2.kind = 'permission_pending'
      AND tn2.sent_at > now() - interval '60 seconds'
  )
```

Combined: terminal/permanent failures suppress forever; transient/maybe-retry failures back off for one cycle.

## Related code

- `orchestrator/main.py:11748` — `thread_permission_notify_sweeper`, the dedup SQL.
- `orchestrator/services/headless_notifications.py:173` — `already_notified()` uses the same `IN ('sent', 'failed')` filter. Same logic; same fix needed for symmetry, or it can be deleted now that the sweeper SQL is doing the same work upstream.
- `orchestrator/services/headless_notifications.py:333-416` — `send_permission_pending_email` is where the skipped_* outcomes get written.
- `orchestrator/database/migrations/app/0006_headless_notifications.sql` — defines `delivery_status` allowed values.

## Resolution (2026-05-13)

Shipped Step 1 (widened IN-set) and Step 2 (recency floor) together as proposed. The floor is parametrized as `2 × interval_s` rather than a hardcoded 60s so a future operator bump of `HEADLESS_NOTIFY_INTERVAL_S` scales it automatically. The `already_notified()` helper and its dedicated tests (`TestAlreadyNotified`, `test_skips_when_already_notified`) were removed — the sweeper SQL is the single source of truth for dedup now.
