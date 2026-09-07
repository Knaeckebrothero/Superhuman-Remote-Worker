-- migration:     0083_datasource_scope_auto_attach.sql
-- description:   Add explicit connector availability scope, owner defaults,
--                optimistic policy revisions, durable project reconciliation,
--                and finish the legacy job-datasource association migration.
--                Originally authored as 0082; renumbered on merge after
--                0082_usage_cloud_rate_cards.sql landed on the same number
--                from a parallel branch (docs/db_migration.md
--                §Conflict resolution: the second to merge renumbers).
--                The two CHECK constraints land NOT VALID here and are
--                validated by 0084 in its own transaction; the partial
--                index on the pre-existing datasources table is built
--                CONCURRENTLY by 0085.
-- depends-on:    0082_usage_cloud_rate_cards.sql
-- expected:      Proportional to datasources/project_datasources; legacy link
--                and immutable job snapshots scan existing work once.
-- locks:         Brief ACCESS EXCLUSIVE lock while adding datasource columns;
--                row-level writes for provenance backfills.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE datasources
    ADD COLUMN scope_mode TEXT NOT NULL DEFAULT 'all',
    ADD COLUMN auto_attach BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN policy_revision BIGINT NOT NULL DEFAULT 1;

-- NOT VALID so the ADD takes only a brief ACCESS EXCLUSIVE lock instead of
-- scanning the table under it; both are enforced against new/updated rows
-- immediately. Every row present here trivially satisfies them (the columns
-- were just added with satisfying defaults, and the backfills below only ever
-- write 'projects'), so 0084's VALIDATE is a formality that confirms the
-- existing set under a non-blocking SHARE UPDATE EXCLUSIVE lock.
ALTER TABLE datasources
    ADD CONSTRAINT datasources_scope_mode_check
    CHECK (scope_mode IN ('all', 'projects')) NOT VALID;

ALTER TABLE datasources
    ADD CONSTRAINT datasources_policy_revision_positive
    CHECK (policy_revision > 0) NOT VALID;

-- The reconciliation worker re-reads PostgreSQL and applies the current state
-- to the external knowledge stores. There are deliberately no foreign keys:
-- project/datasource deletion is itself a state that must remain queued.
-- Queue generations come from a dedicated never-reused sequence. They are
-- intentionally independent of datasource.policy_revision: lease-expired or
-- dual-leader workers must never be able to ACK a later corrective event with
-- a historical claim token.
CREATE SEQUENCE datasource_project_reconcile_generation_seq AS BIGINT;

CREATE TABLE datasource_project_reconcile_queue (
    project_id UUID NOT NULL,
    datasource_id UUID NOT NULL,
    policy_revision BIGINT NOT NULL,
    claim_token BIGINT NOT NULL DEFAULT
        nextval('datasource_project_reconcile_generation_seq'),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, datasource_id)
);

CREATE INDEX idx_datasource_project_reconcile_due
    ON datasource_project_reconcile_queue (next_attempt_at, updated_at);

-- Copy the final legacy clone-to-job association before runtime readers stop
-- consulting datasources.job_id. The canonical row is retained for historical
-- compatibility, but every attachment is now represented by the junction.
INSERT INTO job_datasources (job_id, datasource_id)
SELECT job_id, id
FROM datasources
WHERE job_id IS NOT NULL
ON CONFLICT (job_id, datasource_id) DO NOTHING;

-- Preserve the complete selected-ID contract outside the datasource FK
-- cascade. If a selected connector is later deleted, job_datasources loses the
-- row by design, while this immutable snapshot lets delivery detect the
-- mismatch and fail closed instead of silently running with fewer connectors.
WITH materialized_job_selections AS (
    SELECT
        j.id,
        COALESCE(
            jsonb_agg(
                jd.datasource_id::TEXT ORDER BY jd.linked_at, jd.datasource_id
            ) FILTER (WHERE jd.datasource_id IS NOT NULL),
            '[]'::JSONB
        ) AS datasource_ids,
        COALESCE(
            jsonb_object_agg(
                jd.datasource_id::TEXT,
                d.policy_revision ORDER BY jd.datasource_id
            ) FILTER (WHERE jd.datasource_id IS NOT NULL),
            '{}'::JSONB
        ) AS policy_revisions
    FROM jobs j
    LEFT JOIN job_datasources jd ON jd.job_id = j.id
    LEFT JOIN datasources d ON d.id = jd.datasource_id
    GROUP BY j.id
)
UPDATE jobs j
SET context = COALESCE(j.context, '{}'::JSONB) || jsonb_build_object(
    'datasource_selection',
    COALESCE(j.context->'datasource_selection', '{}'::JSONB)
        || jsonb_build_object(
            'datasource_ids', selection.datasource_ids,
            'policy_revisions', selection.policy_revisions
        )
)
FROM materialized_job_selections selection
WHERE selection.id = j.id;

-- Native project KB rows are server-owned project resources. Repair a missing
-- junction when the marker is a valid extant project UUID, then lock the row to
-- project scope. A malformed/orphan marker is still made project-scoped below,
-- leaving it safely unavailable instead of broadening it globally.
INSERT INTO project_datasources (project_id, datasource_id, read_only)
SELECT p.id, d.id, TRUE
FROM datasources d
JOIN projects p
  ON p.id = CASE
      WHEN d.config->>'native_project_id' ~*
           '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      THEN (d.config->>'native_project_id')::UUID
      ELSE NULL
  END
WHERE d.job_id IS NULL
  AND d.type = 'kb'
ON CONFLICT (project_id, datasource_id) DO NOTHING;

UPDATE datasources
SET scope_mode = 'projects',
    auto_attach = TRUE
WHERE job_id IS NULL
  AND type = 'kb'
  AND NULLIF(config->>'native_project_id', '') IS NOT NULL;

-- Ownerless private connectors historically had no portable owner access;
-- their project links were their only useful boundary. Preserve that boundary.
UPDATE datasources d
SET scope_mode = 'projects',
    auto_attach = FALSE
WHERE d.job_id IS NULL
  AND d.created_by IS NULL
  AND d.is_global = FALSE
  AND NOT (
      d.type = 'kb'
      AND NULLIF(d.config->>'native_project_id', '') IS NOT NULL
  )
  AND EXISTS (
      SELECT 1
      FROM project_datasources pd
      WHERE pd.datasource_id = d.id
  );

COMMENT ON COLUMN datasources.scope_mode IS
    'Execution availability upper bound: all or every selected work project.';
COMMENT ON COLUMN datasources.auto_attach IS
    'Creator-owned creation-time default; never a runtime force attachment.';
COMMENT ON COLUMN datasources.policy_revision IS
    'Optimistic concurrency token for scope/default/project-link policy.';
COMMENT ON TABLE datasource_project_reconcile_queue IS
    'Durable coalescing queue for syncing datasource/project link state to external knowledge stores.';
COMMENT ON COLUMN datasource_project_reconcile_queue.claim_token IS
    'Never-reused sequence token rotated on enqueue and claim; guards stale worker completion.';

-- Junction writes may happen through connector edit, Project Details, or an FK
-- cascade. Centralizing the revision/queue bookkeeping in a trigger covers all
-- three. Retained rows are never rewritten by a full-set diff, so they do not
-- spuriously bump the revision or lose their per-project overrides.
CREATE FUNCTION reconcile_datasource_project_policy_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    affected_project_id UUID;
    affected_datasource_id UUID;
    current_revision BIGINT;
BEGIN
    affected_project_id := COALESCE(NEW.project_id, OLD.project_id);
    affected_datasource_id := COALESCE(NEW.datasource_id, OLD.datasource_id);

    UPDATE datasources
    SET policy_revision = policy_revision + 1,
        updated_at = NOW()
    WHERE id = affected_datasource_id
    RETURNING policy_revision INTO current_revision;

    -- A datasource cascade can remove the datasource row before this trigger
    -- runs. Policy revision remains useful diagnostic metadata; the separate
    -- sequence-backed claim_token is the actual stale-worker fence.
    current_revision := COALESCE(current_revision, 1);

    INSERT INTO datasource_project_reconcile_queue (
        project_id,
        datasource_id,
        policy_revision,
        attempts,
        next_attempt_at,
        last_error,
        updated_at
    ) VALUES (
        affected_project_id,
        affected_datasource_id,
        current_revision,
        0,
        NOW(),
        NULL,
        NOW()
    )
    ON CONFLICT (project_id, datasource_id) DO UPDATE
    SET policy_revision = GREATEST(
            datasource_project_reconcile_queue.policy_revision,
            EXCLUDED.policy_revision
        ),
        claim_token = nextval(
            'datasource_project_reconcile_generation_seq'
        ),
        attempts = 0,
        next_attempt_at = NOW(),
        last_error = NULL,
        updated_at = NOW();

    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE TRIGGER datasource_project_policy_change
AFTER INSERT OR UPDATE OR DELETE ON project_datasources
FOR EACH ROW
EXECUTE FUNCTION reconcile_datasource_project_policy_change();

COMMIT;
