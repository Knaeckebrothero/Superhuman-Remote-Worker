-- migration:     0168_sweep_project_status.sql
-- description:   Sweep projects.status onto the active|archived vocabulary
--                ahead of the CHECK tightening in 0169. Phase 1b of
--                knowledge-base/knowledge/features/project_and_job_list_filtering.md §4.1.
--                DML only, deliberately split from 0169's DDL: mixing the two
--                in one transaction would hold the constraint swap's ACCESS
--                EXCLUSIVE lock for the UPDATE's whole duration
--                (knowledge-base/knowledge/db_migration.md §Common Antipatterns #2;
--                0150/0151 is the in-repo precedent and says so in its header).
-- depends-on:    0167_message_delivery_quota_intents.sql
-- expected:      < 1s, and expected to match ZERO rows. projects holds one row
--                per project (dev: ~15). Nothing has ever written 'paused' or
--                'completed' — 0001 admitted them into the CHECK but no INSERT
--                site lists status at all (they take the column DEFAULT
--                'active') and update_project() skips None, so the column
--                cannot be set to NULL through the API either. The sweep is
--                the belt to 0169's braces: a CHECK evaluates to UNKNOWN on
--                NULL and therefore PASSES, so a NULL row would survive the
--                tightening silently and only surface later as a row the
--                default ?status= filter cannot classify.
-- locks:         ROW EXCLUSIVE on projects — ordinary reads and writes
--                continue throughout. No DDL here, so no lock-retry block.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- 'active' is the correct landing spot for all three cases. NULL and 'paused'
-- are indistinguishable from "never deliberately archived", and 'completed'
-- never meant "archived" — it was aspirational vocabulary from 0001 that no
-- code path ever wrote. Archiving is an explicit user action (PATCH
-- /api/projects/{id} with status='archived'); nothing here should silently
-- archive a project the owner never archived.
--
-- Note: projects carries the BEFORE UPDATE trigger update_projects_updated_at,
-- so any row this touches gets a fresh updated_at and moves to the front of
-- the list query that orders by p.updated_at DESC. Accepted rather than worked
-- around: the expected match count is zero, and a row that does match was
-- already outside the vocabulary the product understands.
UPDATE projects
   SET status = 'active'
 WHERE status IS NULL
    OR status IN ('paused', 'completed');

COMMIT;
