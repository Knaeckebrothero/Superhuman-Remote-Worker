-- migration:     0158_repair_unified_tool_group_markers.sql
-- description:   Repair the tool-group marker damage 0156 left in stored tool
--                policies. 0156 minted explicit job_control: [] /
--                job_inspection: [] members (and left orchestrator: [] behind
--                after moving every name out of it). The legacy (experts-off)
--                session path treats == [] as a hard-disable marker
--                (_apply_session_tool_group_markers in src/api/persistent_app.py
--                and the default-append logic in persistent_session.py), while
--                an ABSENT key gets that group's defaults appended — so
--                migrated rows silently lost their job tools. Repair forward
--                only; applied 0156 is never edited.
-- depends-on:    0157_project_officers_post.sql
-- expected:      Short JSONB updates; data-only (no DDL, so the schema_current
--                artifact is unchanged). Idempotent: repaired rows no longer
--                match. On the dev cluster this touches ~127 rows
--                (120 jobs.resolved_config + 7 threads).
-- locks:         Row locks on matching config-bearing rows only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Restores pre-0156 EFFECTIVE semantics per rule:
--
-- (a) Delete job_control / job_inspection members whose value is exactly [].
--     Neither key existed before 0156 (the groups were introduced by the same
--     commit that shipped it), so every empty one is a migration artifact —
--     never a user opt-out. Deleting them returns the key to ABSENT, which is
--     what the pre-0156 row effectively was.
--
-- (b) Delete an orchestrator member whose value is exactly [] ONLY where at
--     least one job group is non-empty: non-empty groups are the proof that
--     0156 moved names OUT of orchestrator (that is the only way a pre-split
--     row grows job-group names), so the [] residue never existed as stored
--     data — pre-0156 the key held those names and was therefore enabled.
--     An orchestrator: [] with both job groups empty/absent is a legitimate
--     pre-existing opt-out and survives untouched.
--
-- The two F3 shapes this reverses:
--
--   * The get_session_context row: {orchestrator: ["get_session_context"]}
--     became {orchestrator: ["get_session_context"], job_control: [],
--     job_inspection: []}. Pre-0156 the session appended the job defaults
--     (no job keys existed); post-0156 the minted []s hard-disabled both job
--     groups. Rule (a) deletes both artifacts; orchestrator is non-empty so
--     rule (b) does not apply — byte-identical to the pre-0156 row.
--
--   * The all-job-names row: {orchestrator: ["create_worker_job", ...]} (only
--     job names) became {orchestrator: [], job_control: ["create_job", ...],
--     job_inspection: [...]}. The orchestrator residue reads as a
--     fleet-management opt-out the user never made. Rule (b) deletes it (the
--     non-empty job groups are the movement proof); the renamed job grants
--     stay, and the absent orchestrator key reads enabled again as pre-0156.
--
-- Observed dev-cluster damage matches shape one plus the resolved worker
-- blobs: {..., orchestrator: [], job_control: [], job_inspection: []} where
-- orchestrator: [] predates 0156 (worker configs legitimately grant no
-- orchestrator tools). There rules (a)+(b) delete only the two artifacts and
-- keep the pre-existing orchestrator opt-out — again byte-identical to the
-- pre-0156 row.
--
-- Boolean policy values (orchestrator: true) were skipped whole-row by
-- 0156's non-array guard and are skipped here too; their job surface is
-- restored in code at the expansion site
-- (src/core/tool_policy.expand_category_true), not by rewriting stored rows.
CREATE FUNCTION pg_temp.repair_job_tool_markers(config JSONB)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    tools JSONB;
BEGIN
    -- Same missing/null/non-object guard as 0156: jsonb_typeof() of an
    -- absent member is SQL NULL, and several valid stored fragments declare
    -- no tools at all.
    IF jsonb_typeof(config) IS DISTINCT FROM 'object'
       OR jsonb_typeof(config->'tools') IS DISTINCT FROM 'object' THEN
        RETURN config;
    END IF;
    tools := config->'tools';

    -- Rule (a): the minted empty job groups. jsonb equality against
    -- '[]'::jsonb is exact — a boolean, object, non-empty array, or absent
    -- member never matches (absent compares as SQL NULL, and the IF falls
    -- through).
    IF tools->'job_control' = '[]'::jsonb THEN
        tools := tools - 'job_control';
    END IF;
    IF tools->'job_inspection' = '[]'::jsonb THEN
        tools := tools - 'job_inspection';
    END IF;

    -- Rule (b): evaluated against the groups that SURVIVED rule (a), which
    -- by construction are exactly the non-empty ones — the names 0156 moved.
    IF tools->'orchestrator' = '[]'::jsonb
       AND (
            (jsonb_typeof(tools->'job_control') = 'array'
             AND jsonb_array_length(tools->'job_control') > 0)
         OR (jsonb_typeof(tools->'job_inspection') = 'array'
             AND jsonb_array_length(tools->'job_inspection') > 0)
       ) THEN
        tools := tools - 'orchestrator';
    END IF;

    RETURN jsonb_set(config, '{tools}', tools);
END;
$$;

CREATE FUNCTION pg_temp.repair_resolved_job_tool_markers(config JSONB)
RETURNS JSONB
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN jsonb_typeof(config->'agent') = 'object'
        THEN jsonb_set(
            config,
            '{agent}',
            pg_temp.repair_job_tool_markers(config->'agent')
        )
        ELSE config
    END
$$;

-- Same seven stores 0156 rewrote, so every minted artifact is reachable.
UPDATE experts
SET config = pg_temp.repair_job_tool_markers(config)
WHERE config IS DISTINCT FROM pg_temp.repair_job_tool_markers(config);

UPDATE project_experts
SET config_override = pg_temp.repair_job_tool_markers(config_override)
WHERE config_override IS DISTINCT FROM
      pg_temp.repair_job_tool_markers(config_override);

UPDATE projects
SET default_config_override = pg_temp.repair_job_tool_markers(default_config_override)
WHERE default_config_override IS DISTINCT FROM
      pg_temp.repair_job_tool_markers(default_config_override);

UPDATE automations
SET config_override = pg_temp.repair_job_tool_markers(config_override)
WHERE config_override IS DISTINCT FROM
      pg_temp.repair_job_tool_markers(config_override);

UPDATE config_overrides
SET value_json = pg_temp.repair_job_tool_markers(value_json)
WHERE value_json IS DISTINCT FROM pg_temp.repair_job_tool_markers(value_json);

UPDATE threads
SET metadata = jsonb_set(
    metadata,
    '{config_override}',
    pg_temp.repair_job_tool_markers(metadata->'config_override')
)
WHERE metadata->'config_override' IS DISTINCT FROM
      pg_temp.repair_job_tool_markers(metadata->'config_override');

-- Paused/resumable jobs hydrate their frozen resolved_config, so the minted
-- markers must be repaired inside the embedded agent config as well.
UPDATE jobs
SET config_override = pg_temp.repair_job_tool_markers(config_override),
    resolved_config = pg_temp.repair_resolved_job_tool_markers(resolved_config)
WHERE config_override IS DISTINCT FROM pg_temp.repair_job_tool_markers(config_override)
   OR resolved_config IS DISTINCT FROM
      pg_temp.repair_resolved_job_tool_markers(resolved_config);

COMMIT;
