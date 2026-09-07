-- migration:     0192_stateless_input_delivery_validate.sql
-- description:   Validate the lane and owner constraints installed NOT VALID
--                by 0185 after its rollout-serialized historical backfill.
-- depends-on:    0191_stateless_input_deliveries.sql
-- expected:      < 1s. Three scans of the bounded input-delivery ledger. The
--                preceding migration already rewrote every genuine historical
--                row from its locked owning-thread lane. A failure therefore
--                reports unsupported/contradictory history and must be
--                reconciled, never silently widened.
-- locks:         SHARE UPDATE EXCLUSIVE on thread_input_deliveries; ordinary
--                reads and writes continue while PostgreSQL validates.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.thread_input_deliveries
    VALIDATE CONSTRAINT thread_input_deliveries_lane_check,
    VALIDATE CONSTRAINT thread_input_deliveries_owner_shape,
    VALIDATE CONSTRAINT thread_input_deliveries_claim_shape;

COMMIT;
