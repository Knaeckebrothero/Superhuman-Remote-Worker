-- migration:     0170_project_status_validate.sql
-- description:   Validate the tightened projects.status CHECK that 0169 added
--                NOT VALID. Split into its own migration because the runner
--                wraps each transactional file in a single transaction:
--                validating alongside the ADD would hold that ADD's ACCESS
--                EXCLUSIVE lock across the scan and defeat the point of
--                NOT VALID entirely — which is exactly what squawk's
--                constraint-missing-not-valid rule flags. Phase 1b of
--                knowledge-base/knowledge/features/project_and_job_list_filtering.md §4.1.
-- depends-on:    0169_project_status_active_archived.sql
-- expected:      < 1s. One sequential scan of projects, which is small (one
--                row per project; dev: ~15). Expected to pass without
--                rejecting a row: 0168 already swept NULL, 'paused' and
--                'completed' onto 'active' in a committed transaction, so
--                every surviving row is 'active' or 'archived'. If this
--                migration DOES fail, the scan found a row written between
--                0168 and here — repair that row and re-run rather than
--                widening the constraint back.
-- locks:         SHARE UPDATE EXCLUSIVE on projects — ordinary reads and
--                writes continue throughout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- VALIDATE is what flips convalidated, so the constraint stops being merely
-- forward-looking: only after this does it assert anything about rows that
-- already existed, and only then may the planner rely on it.
ALTER TABLE projects VALIDATE CONSTRAINT valid_project_status;

COMMIT;
