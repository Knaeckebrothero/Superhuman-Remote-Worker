-- Supersedes the constraint shape that 0183 installed, without editing 0183.
--
-- Why this file exists rather than a two-character change to 0183:
-- `ff178032` ("fix(ci): unblock protected cloud cutover gates") appended
-- NOT VALID to both CHECK constraints *inside* 0183 after 0183 had already
-- been applied. Applied migrations are immutable -- `run_migrations` compares
-- a sha256 of each file against the checksum recorded in schema_migrations
-- (migrate.py:810) and raises "checksum changed" -- so every database that had
-- already run 0183 refused to boot the orchestrator at all. Because every
-- other pod's init container waits on the orchestrator, the entire stack stays
-- down, presenting as a Helm post-install timeout rather than as a migration
-- fault. 0183 has been restored to its original committed content and the
-- intended change is re-expressed here.
--
-- NOT VALID means: enforce this constraint for every INSERT and UPDATE from
-- now on, but do not scan the existing rows. That is the point -- rows written
-- before the cancellation contract existed cannot satisfy it, and a validating
-- ADD CONSTRAINT would fail on exactly the deployments that have history.
-- Postgres still applies a NOT VALID CHECK to all new writes; only the
-- backfill scan is skipped. Run VALIDATE CONSTRAINT later, out of band, if the
-- legacy rows are ever cleaned up.
--
-- Idempotent by construction: DROP ... IF EXISTS then ADD, so this is safe on
-- a database that applied the original 0183, on one that applied the edited
-- 0183, and on a fresh install where 0183 just ran.

ALTER TABLE public.thread_input_deliveries
    DROP CONSTRAINT IF EXISTS thread_input_deliveries_state_check,
    ADD CONSTRAINT thread_input_deliveries_state_check CHECK (state IN (
        'persisted', 'owned', 'queued', 'admitted', 'settled', 'deferred',
        'cancelled'
    )) NOT VALID;

ALTER TABLE public.thread_input_deliveries
    DROP CONSTRAINT IF EXISTS thread_input_deliveries_cancellation_shape,
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
