-- migration:     0152_completion_effects_producer_comment.sql
-- description:   Document the polymorphic completion-effect producer and
--                retention contract without changing already-applied 0145.
-- depends-on:    0151_job_wake_undeliverable_validate.sql
-- expected:      < 1s. COMMENT is a catalog-only metadata change.
-- locks:         Brief SHARE UPDATE EXCLUSIVE on completion_effects.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

COMMENT ON TABLE completion_effects IS
    'One stable-name progress row per completion effect. Polymorphic by '
    'producer_kind and deliberately has no foreign key or state-driven '
    'partial index. job_completion producers use the command finalizer states; '
    'session_turn producers use pending, done, or dead and are age-pruned from '
    'created_at. Retention is explicit for both kinds.';

COMMIT;
