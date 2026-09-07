-- migration:     0221_threads_parent_tool_call_comment.sql
-- description:   Document both durable subagent-parent idempotency keys after
--                session parents gained their own unique replay index.
-- depends-on:    0220_stateless_subagent_recovery_events.sql
-- expected:      < 1s. COMMENT is a catalog-only metadata change.
-- locks:         Brief SHARE UPDATE EXCLUSIVE on threads.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

COMMENT ON COLUMN public.threads.parent_tool_call_id IS
    'kind=subagent only: the parent''s delegate_agent tool call this child '
    'answered. The non-NULL parent plus parent_tool_call_id is the idempotency '
    'key: a parent re-running its tools node after a hard kill replays the '
    'stored report instead of spawning again; both parent forms are unique in '
    'the database.';

COMMIT;
