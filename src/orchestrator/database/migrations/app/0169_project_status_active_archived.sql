-- migration:     0169_project_status_active_archived.sql
-- description:   Collapse the projects.status vocabulary from four values to
--                two: active|archived. 0001 admitted 'paused' and 'completed'
--                speculatively and nothing ever wrote either; 0168 swept any
--                stragglers (and NULL) onto 'active' in its own transaction.
--                Phase 1b of
--                knowledge-base/knowledge/features/project_and_job_list_filtering.md §4.1;
--                ProjectUpdate.status became Literal['active','archived'] in
--                phase 1a, so this closes the gap between the API vocabulary
--                and the one the database will actually enforce.
-- depends-on:    0168_sweep_project_status.sql
-- expected:      < 1s. Catalog-only constraint replacement — NOT VALID means
--                no scan of existing rows happens here. Validation is deferred
--                to 0170 so the scan cannot extend this ACCESS EXCLUSIVE
--                transaction, and because squawk's constraint-missing-not-valid
--                fires when NOT VALID and VALIDATE CONSTRAINT share a
--                transaction — and the runner wraps each transactional file in
--                exactly one transaction, so "separate transaction" means
--                "separate file" (0084's header states this; 0002 is the
--                same-shape precedent that predates the rule).
-- locks:         Brief ACCESS EXCLUSIVE on projects for the constraint swap,
--                bounded by lock_timeout and retried with jittered backoff.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- DROP + ADD rather than a second constraint: the name is the contract the
-- rest of the codebase reasons about (main.py and test_project_access.py both
-- cite valid_project_status by name when explaining that 'deleted' was always
-- dead code). Keeping one constraint under the original name keeps that true.
--
-- Deliberately NOT adding NOT NULL. The column stays nullable so the
-- fail-toward-showing arm in §4.2 — an unknown or NULL status stays visible
-- rather than being swallowed by the default ?status=active filter — remains
-- reachable; §4.7 point 5 asks for it to be kept.
DO $$
DECLARE
    max_attempts CONSTANT int    := 30;
    cap_ms       CONSTANT bigint := 60000;
    base_ms      CONSTANT bigint := 10;
    delay_ms              bigint;
    done                  boolean := false;
BEGIN
    FOR i IN 1..max_attempts LOOP
        BEGIN
            ALTER TABLE projects DROP CONSTRAINT IF EXISTS valid_project_status;
            ALTER TABLE projects
                ADD CONSTRAINT valid_project_status
                    CHECK (status IN ('active', 'archived')) NOT VALID;
            done := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            delay_ms := round(random() * least(cap_ms, base_ms * 2 ^ i));
            PERFORM pg_sleep(delay_ms::numeric / 1000);
        END;
    END LOOP;
    IF NOT done THEN
        RAISE EXCEPTION 'lock acquisition failed on projects after % attempts',
            max_attempts;
    END IF;
END $$;

COMMENT ON COLUMN projects.status IS
    'Project lifecycle: active|archived, enforced by valid_project_status '
    'since 0169. Archiving is an explicit owner action and hides the project '
    'from the default GET /api/projects list (?status= opts it back in); '
    'deletion is a hard row delete, never a status. The column remains '
    'NULLABLE on purpose — a CHECK passes on NULL, and the API fails toward '
    'showing an unclassifiable row rather than hiding it.';

COMMIT;
