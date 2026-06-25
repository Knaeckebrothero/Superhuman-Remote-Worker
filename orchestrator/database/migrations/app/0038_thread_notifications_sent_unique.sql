-- migration:     0038_thread_notifications_sent_unique.sql
-- description:   Partial unique index for headless permission-email dedup
--                (HA / M1 leader election,
--                docs/superpowers/plans/2026-06-25-orchestrator-m1-leader-election.md
--                Task 5).
--
--                thread_permission_notify_sweeper is leader-gated, but leader
--                election has no fencing: during a partition / Postgres
--                failover two replicas can briefly both run the sweep and both
--                send the same "approval needed" email. The pre-existing dedup
--                was a racy read (NOT EXISTS ... delivery_status='sent' in the
--                sweep query + a COUNT rate-limit probe), so both sweepers
--                could pass the check and both send.
--
--                This index makes the 'sent' marker a single claimable slot per
--                (request_id, kind): send_permission_pending_email now INSERTs
--                the 'sent' row with ON CONFLICT DO NOTHING *before* sending and
--                only sends if it won the slot. The predicate is exactly
--                `delivery_status = 'sent'` so the INSERT's ON CONFLICT arbiter
--                inference matches. request_id IS NULL rows (other kinds) are
--                never constrained — NULLs are distinct under a unique index —
--                so non-permission notifications keep appending freely.
--
--                Pre-existing duplicate 'sent' rows (from the old racy path)
--                are collapsed first (keep the earliest id) so the unique index
--                can build; the dropped rows are redundant audit duplicates.
-- depends-on:    0037_processed_inbound_emails.sql
-- expected:      < 200ms. thread_notifications is small + low-traffic.
-- locks:         Brief SHARE lock on thread_notifications for the non-concurrent
--                index build (blocks writes for the build only); covered by
--                lock_timeout. Acceptable on a small table.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Collapse any pre-existing duplicate 'sent' markers for the same
-- (request_id, kind), keeping the earliest row, so the unique index can build.
DELETE FROM thread_notifications a
USING thread_notifications b
WHERE a.delivery_status = 'sent'
  AND b.delivery_status = 'sent'
  AND a.request_id IS NOT NULL
  AND a.request_id = b.request_id
  AND a.kind = b.kind
  AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tn_sent_request_kind
    ON thread_notifications (request_id, kind)
    WHERE delivery_status = 'sent';

COMMENT ON INDEX uq_tn_sent_request_kind IS
    'At most one delivery_status=''sent'' row per (request_id, kind). The '
    'insert-as-claim dedup slot for headless emails (HA / M1) — '
    'send_permission_pending_email claims it before sending so the transient '
    'dual-leader window cannot double-send. NULL request_id rows are unconstrained.';

COMMIT;
