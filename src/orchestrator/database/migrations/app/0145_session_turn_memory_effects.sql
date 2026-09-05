-- migration:     0145_session_turn_memory_effects.sql
-- description:   Give each durable stateless session turn a transaction-local
--                execution identity and add independent claim ownership for
--                the shared completion-effect drain.
-- depends-on:    0144_job_completion_status_reorder.sql
-- expected:      < 5s. Two nullable, metadata-only column additions.
-- locks:         Brief ACCESS EXCLUSIVE on thread_messages and
--                completion_effects; neither table is rewritten.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE thread_messages
    ADD COLUMN turn_execution_id UUID;

COMMENT ON COLUMN thread_messages.turn_execution_id IS
    'Identity minted on the exact accepted turn-boundary message inside the '
    'stateless claim''s fenced final-transcript transaction. It is reused by '
    'an idempotent reconcile and keys session_turn completion effects; it is '
    'NULL for pinned turns and messages that are not a finalized boundary.';

ALTER TABLE completion_effects
    ADD COLUMN claimed_by UUID;

COMMENT ON COLUMN completion_effects.claimed_by IS
    'Independent session-effect drain claim identity. NULL means unclaimed; '
    'a session drain may complete or release only the UUID it claimed.';

COMMENT ON TABLE completion_effects IS
    'One stable-name progress row per completion effect. Polymorphic by '
    'producer_kind and deliberately has no foreign key or state-driven '
    'partial index. job_completion producers use the command finalizer states; '
    'session_turn producers use pending, done, or dead and are age-pruned from '
    'created_at. Retention is explicit for both kinds.';

COMMIT;
