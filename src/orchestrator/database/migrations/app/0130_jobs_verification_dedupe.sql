-- migration:     0130_jobs_verification_dedupe.sql
-- description:   Retire pre-existing duplicate verification critics before
--                the immutable one-critic-per-round unique index is built.
-- depends-on:    0129_thread_interrupt_validate_constraints.sql
-- expected:      One indexed-candidate scan plus updates only for duplicate
--                losers. Production pre-flight found 138 candidates and no
--                duplicates; k3d found 7 candidates and no duplicates.
-- locks:         RowExclusiveLock on jobs; duplicate loser rows are locked by
--                the UPDATE. Ordinary job reads and writes remain available.
-- transactional: yes
--
-- The future index deliberately ignores status, so setting a loser to
-- cancelled is not enough to make the duplicate key buildable. Preserve the
-- original round and chosen winner under context.verification_dedupe, then
-- remove only verification_round so the historical loser leaves the exact
-- immutable index predicate without losing its verification_target identity.

BEGIN;
SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '10min';

WITH ranked AS MATERIALIZED (
    SELECT
        id,
        first_value(id) OVER critic_round AS winner_id,
        row_number() OVER critic_round AS duplicate_rank
    FROM jobs
    WHERE parent_job_id IS NOT NULL
      AND context->>'verification_target' IS NOT NULL
      AND jsonb_exists(context, 'verification_round')
    WINDOW critic_round AS (
        PARTITION BY parent_job_id, (context->>'verification_round')
        ORDER BY created_at ASC NULLS LAST, id ASC
    )
), losers AS (
    SELECT id, winner_id
    FROM ranked
    WHERE duplicate_rank > 1
)
UPDATE jobs AS job
SET status = 'cancelled',
    assigned_agent_id = NULL,
    context = jsonb_set(
        job.context - 'verification_round',
        '{verification_dedupe}',
        CASE
            WHEN jsonb_typeof(job.context->'verification_dedupe') = 'object'
                THEN job.context->'verification_dedupe'
            ELSE '{}'::jsonb
        END || jsonb_build_object(
            'migration', '0130_jobs_verification_dedupe',
            'reason', 'duplicate_parent_round',
            'original_round', job.context->'verification_round',
            'winner_job_id', losers.winner_id::text
        ),
        true
    ),
    updated_at = CURRENT_TIMESTAMP
FROM losers
WHERE job.id = losers.id;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM jobs
        WHERE parent_job_id IS NOT NULL
          AND context->>'verification_target' IS NOT NULL
          AND jsonb_exists(context, 'verification_round')
        GROUP BY parent_job_id, (context->>'verification_round')
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            '0130 failed: duplicate verification critic keys remain';
    END IF;
END
$$;

COMMIT;
