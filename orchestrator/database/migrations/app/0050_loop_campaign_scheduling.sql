-- migration:     0050_loop_campaign_scheduling.sql
-- description:   Campaign scheduling for project loops (the Critic as planner).
--                Adds an opt-in `scheduling` mode plus the campaign control
--                state that lets a checkpoint Critic allocate the execution
--                slot as a multi-job campaign (K sequential stages toward one
--                initiative) instead of the fixed one-job rotation. A loop
--                left on the default `rotation` is byte-identical to today.
--                docs/features/loop_campaign_scheduling.md (P0).
-- depends-on:    0048_loop_parallel_stages.sql
-- expected:      < 1s (ADD COLUMN with constant/NULL defaults; metadata-only,
--                no table rewrite on PG11+).
-- locks:         Brief ACCESS EXCLUSIVE on project_loops (tiny control table).
-- transactional: yes
--
-- Design notes:
--   * `campaign` holds the ACTIVE campaign's control state as one JSONB doc:
--     {id, plan_job_id, initiative_note_id, title, stages: [{role}...],
--      cursor, stages_done, extensions_used, member_failures, acceptance: [],
--      status: active|review|aborted}. Correctness does NOT ride this row
--     alone: each spawned member is stamped with `loop_campaign_id` +
--     `loop_campaign_index` in its job context (spawn-time truth), so the
--     advance derives "next stage to spawn" from the completed member's stamp
--     — the same stamp-preferring recovery model the torn-advance sweeper
--     already uses for seq_index/remaining. A lost write-back therefore heals
--     through the EXISTING re-point-and-re-advance path with no new torn
--     signatures and no sweeper changes.
--   * `campaign_history` is the bounded archive of disposed campaigns
--     (ship/kill/abort outcomes), newest last, capped app-side.
--   * `campaign_caps` is the optional per-loop override for the campaign
--     guardrails ({max_stages, max_extensions, abort_failures}); NULL means
--     the config defaults. Validated against hard ceilings at loop start.
--   * `scheduling` is start-time-only (not in the update allowlist): flipping
--     a live loop's mode mid-run would orphan in-flight campaign state.
-- ============================================================================

ALTER TABLE project_loops
    ADD COLUMN IF NOT EXISTS scheduling TEXT NOT NULL DEFAULT 'rotation';

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_scheduling_known
        CHECK (scheduling IN ('rotation', 'planner'));

ALTER TABLE project_loops
    ADD COLUMN IF NOT EXISTS campaign JSONB;

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_campaign_is_object
        CHECK (campaign IS NULL OR jsonb_typeof(campaign) = 'object');

ALTER TABLE project_loops
    ADD COLUMN IF NOT EXISTS campaign_history JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_campaign_history_is_array
        CHECK (jsonb_typeof(campaign_history) = 'array');

ALTER TABLE project_loops
    ADD COLUMN IF NOT EXISTS campaign_caps JSONB;

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_campaign_caps_is_object
        CHECK (campaign_caps IS NULL OR jsonb_typeof(campaign_caps) = 'object');

COMMENT ON COLUMN project_loops.scheduling IS
    'Execution-slot scheduling mode: rotation (fixed one-job slot, the '
    'default — byte-identical to pre-0050 behavior) or planner (a checkpoint '
    'Critic may expand the slot into a multi-stage campaign via a filed '
    'plan). Start-time-only. docs/features/loop_campaign_scheduling.md.';

COMMENT ON COLUMN project_loops.campaign IS
    'Active campaign control state (planner loops only): stages, cursor, '
    'counters, acceptance evidence, status active|review|aborted. Recovery '
    'truth lives in member-job context stamps (loop_campaign_id/_index), '
    'not here. NULL when no campaign has been planned.';

COMMENT ON COLUMN project_loops.campaign_history IS
    'Bounded archive of disposed campaigns (outcome ship|kill|abort + '
    'counters), newest last, capped app-side.';

COMMENT ON COLUMN project_loops.campaign_caps IS
    'Optional per-loop guardrail overrides: {max_stages, max_extensions, '
    'abort_failures}. NULL = config defaults. Validated against hard '
    'ceilings at loop start.';
