-- migration:     0231_run_queue_park_lifecycle.sql
-- description:   Park lifecycle for the stateless run_queue
--                (knowledge-base/knowledge/features/stateless_turn_resilience.md
--                step 2). A parked unit was a bare state with no reason, no
--                timestamp and no failure record, so an operator could not tell
--                a shutdown-cancelled completion from a poison attach, and the
--                attach-failed release loop counted nothing (dev thread
--                ad7eb761: 12 claims of one 400 in ten minutes, never parked).
--                These columns let every failure path count, back off, and end
--                somewhere visible: `park_reason` names the disposition,
--                `parked_at` dates it, `last_error` / `last_error_signature`
--                carry the failing attach (the signature is the loop-breaker:
--                the same signature three times in a row parks), and
--                `attach_failures` is the consecutive same-signature counter.
--                All nullable or constant-default: catalog-only on PG 11+.
-- depends-on:    0117_run_queue_affinity.sql
-- expected:      < 1s. Five ADD COLUMNs (no rewrite) on a table holding one
--                row per live unit.
-- locks:         Brief ACCESS EXCLUSIVE on run_queue for the ADD COLUMNs.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '60s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.run_queue
    ADD COLUMN IF NOT EXISTS park_reason          TEXT,
    ADD COLUMN IF NOT EXISTS parked_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error           TEXT,
    ADD COLUMN IF NOT EXISTS last_error_signature TEXT,
    ADD COLUMN IF NOT EXISTS attach_failures      INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.run_queue.park_reason IS
    'Why the unit is parked: attach_failed | shutdown_cancelled | '
    'completion_cas_failed | reaper_max_attempts | claim_loss_hold | '
    'a free-form executor reason. NULL while not parked. Cleared by unpark.';
COMMENT ON COLUMN public.run_queue.parked_at IS
    'When the current park was recorded. NULL while not parked.';
COMMENT ON COLUMN public.run_queue.last_error IS
    'Message of the most recent attach failure (bounded by the executor). '
    'Cleared by completion and unpark.';
COMMENT ON COLUMN public.run_queue.last_error_signature IS
    'Normalised class+message signature of the most recent attach failure; '
    'consecutive identical signatures are what park a poison attach.';
COMMENT ON COLUMN public.run_queue.attach_failures IS
    'Consecutive attach failures with the same signature since the last '
    'completion or unpark; reset to 1 when the signature changes.';

COMMIT;
