-- migration:     0077_message_log_session_thread_ids.sql
-- description:   Let notification rows be keyed by a persistent-session
--                thread UUID (36 chars), not just the 6-hex email-threading
--                key both columns were sized for.
--
--                Officer pages (docs/features/centurion.md §6) have no job
--                behind them: the session thread is the addressable thing,
--                and the only working reply channel is the session log at
--                /sessions/{thread_id} (F4 addendum in
--                docs/issues/officer_conference_live_fire_findings.md).
--                _dispatch_officer_page now persists an outbound message_log
--                row — the notification bell's backing store — with
--                job_id NULL (the jobs FK forbids anything else) and
--                thread_id = the session UUID so the cockpit action center
--                can route "Open session log". At VARCHAR(12) that INSERT
--                could not exist.
--
--                notification_queue gets the same widening because
--                session_wake._notify_owner already passes a session UUID as
--                thread_id into dispatch(); during quiet hours the queue
--                INSERT overflowed VARCHAR(12) and the notice was silently
--                dropped (the write is best-effort, warning-only).
--
--                Existing writers (send_agent_message: 6-hex thread ids,
--                _notify_loop_event: 'loop-' + 6 chars) are unaffected.
-- depends-on:    0076_contacts_normalize.sql
-- expected:      < 1s. varchar length increase is metadata-only in PG 9.2+ —
--                no table rewrite, no index rebuild (idx_message_log_thread
--                stays valid).
-- locks:         Brief ACCESS EXCLUSIVE on message_log and notification_queue
--                for the ALTER COLUMN TYPE catalog updates.
-- transactional: yes
-- ============================================================================

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE message_log ALTER COLUMN thread_id TYPE VARCHAR(64);
ALTER TABLE notification_queue ALTER COLUMN thread_id TYPE VARCHAR(64);

COMMENT ON COLUMN message_log.thread_id IS
    'Conversation key. Job messages use a short hex token (email threading), '
    'loop events use ''loop-'' + 6 chars, officer pages use the persistent '
    'session thread UUID (job_id is NULL on those rows — the jobs FK forbids '
    'storing a thread id there, and the session log is the reply channel).';

COMMIT;
