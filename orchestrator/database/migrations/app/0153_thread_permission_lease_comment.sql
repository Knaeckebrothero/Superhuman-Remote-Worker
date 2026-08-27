-- migration:     0153_thread_permission_lease_comment.sql
-- description:   Clarify rolling-compatibility retirement of legacy NULL
--                stateless permission lease identities.
-- depends-on:    0152_completion_effects_producer_comment.sql
-- expected:      < 1s. COMMENT is a catalog-only metadata change.
-- locks:         Brief SHARE UPDATE EXCLUSIVE on thread_permission_requests.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

COMMENT ON COLUMN thread_permission_requests.accepted_lease_token IS
    'Immutable exact stateless session_turn lease captured while admission '
    'holds the threads -> run_queue locks. NULL identifies pinned or legacy '
    'rows and is never guessed by a generic expiry sweep. For rolling '
    'compatibility, a NULL row may be expired only at a proven writer-exclusive '
    'stateless owner-loss or terminal boundary.';

COMMIT;
