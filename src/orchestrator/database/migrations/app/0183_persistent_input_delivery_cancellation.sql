-- migration:     0183_persistent_input_delivery_cancellation.sql
-- description:   Give a direct human input stopped before provider admission
--                a terminal, non-reclaimable delivery outcome.
-- depends-on:    0182_deliverable_contract_authority.sql
-- expected:      < 1s. Three nullable columns and two CHECK replacements on
--                the small persistent-input ledger; no row rewrite.
-- locks:         ACCESS EXCLUSIVE on thread_input_deliveries, briefly.
-- transactional: yes
-- rollout:       Deploy this reader schema/code fleet-wide and drain old
--                persistent/warm agents before enabling the default-off
--                PERSISTENT_INPUT_CANCELLATION_ENABLED writer gate. Once a
--                cancelled row exists, rollback below the 0183-aware reader
--                is forbidden. Deleting only its ledger row is unsafe because
--                the associated visible human transcript row would remain and
--                re-enter old-reader model context; rollback requires that
--                transcript/thread to be tombstoned/deleted or another
--                exclusion an old reader already understands.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.thread_input_deliveries
    ADD COLUMN cancelled_at TIMESTAMPTZ,
    ADD COLUMN cancelled_turn_number BIGINT,
    ADD COLUMN cancelled_reason TEXT;

ALTER TABLE public.thread_input_deliveries
    DROP CONSTRAINT thread_input_deliveries_state_check,
    ADD CONSTRAINT thread_input_deliveries_state_check CHECK (state IN (
        'persisted', 'owned', 'queued', 'admitted', 'settled', 'deferred',
        'cancelled'
    )) NOT VALID,
    ADD CONSTRAINT thread_input_deliveries_cancellation_shape CHECK (
        (
            state <> 'cancelled'
            AND cancelled_at IS NULL
            AND cancelled_turn_number IS NULL
            AND cancelled_reason IS NULL
        )
        OR
        (
            state = 'cancelled'
            AND source = 'direct_human'
            AND cancelled_at IS NOT NULL
            AND cancelled_turn_number IS NOT NULL
            AND cancelled_turn_number > 0
            AND cancelled_reason IS NOT NULL
            AND btrim(cancelled_reason) <> ''
            AND length(cancelled_reason) <= 120
        )
    ) NOT VALID;

COMMENT ON COLUMN public.thread_input_deliveries.cancelled_at IS
    'Terminal timestamp for an explicit direct-human Stop before provider admission.';
COMMENT ON COLUMN public.thread_input_deliveries.cancelled_turn_number IS
    'Exact transcript turn whose pre-provider Stop cancelled this delivery.';
COMMENT ON COLUMN public.thread_input_deliveries.cancelled_reason IS
    'Bounded server-owned reason for terminal cancellation; never model supplied.';

COMMIT;
