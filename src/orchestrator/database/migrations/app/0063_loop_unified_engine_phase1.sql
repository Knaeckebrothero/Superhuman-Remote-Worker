-- migration:     0063_loop_unified_engine_phase1.sql
-- description:   Loop unified engine, Phase 1 (docs/features/loop_unified_engine.md).
--                Renames the scheduling modes (rotation → standard,
--                planner → campaign) and backfills current_stage_jobs for
--                active loops' in-flight width-1 turns, so the generalized
--                stage barrier can become the ONLY advance path (a sequential
--                step is a width-1 stage; current_job_id becomes a
--                display-only mirror for width-1 turns).
-- depends-on:    0062_canvas_bootstrap_exchange.sql
-- expected:      < 1s (control-table UPDATEs on a handful of rows).
-- locks:         Brief ACCESS EXCLUSIVE on project_loops (tiny control table).
-- transactional: yes
-- ============================================================================

-- Mode value rename. Constraint swapped in the same transaction so no row can
-- hold an old name after commit.
ALTER TABLE project_loops
    DROP CONSTRAINT IF EXISTS project_loop_scheduling_known;

UPDATE project_loops
SET scheduling = CASE scheduling
    WHEN 'rotation' THEN 'standard'
    WHEN 'planner' THEN 'campaign'
    ELSE scheduling
END;

ALTER TABLE project_loops
    ALTER COLUMN scheduling SET DEFAULT 'standard';

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_scheduling_known
        CHECK (scheduling IN ('standard', 'campaign'));

-- Unified engine: every in-flight turn is barrier-tracked in
-- current_stage_jobs (width 1 included). Backfill active loops' width-1 turns
-- so the completion hook's membership check finds them after the deploy — a
-- running loop whose pointer-only turn the new code can't see would wedge.
-- Terminal loops are inert (never swept, never advanced) and stay untouched.
UPDATE project_loops
SET current_stage_jobs = jsonb_build_array(current_job_id::text)
WHERE current_job_id IS NOT NULL
  AND current_stage_jobs = '[]'::jsonb
  AND status IN ('running', 'paused');

COMMENT ON COLUMN project_loops.scheduling IS
    'Scheduling mode: standard (the role_sequence stage list, one stage per '
    'turn — subsumes the old rotation mode and its fan-out stages) or '
    'campaign (a checkpoint Critic may expand the execution slot into a '
    'multi-stage campaign via a filed plan; formerly planner). '
    'Start-time-only. docs/features/loop_unified_engine.md.';

COMMENT ON COLUMN project_loops.current_stage_jobs IS
    'In-flight members of the loop''s current turn — the jobs the loop '
    'barriers on before rotating, width 1 included (the unified engine''s '
    'only advance path). Populated by the advance/start spawn; drained to [] '
    'by the atomic last-member barrier, which also nulls current_job_id so '
    'the torn-advance signature stays current_job_id IS NULL AND '
    'current_stage_jobs = ''[]''. docs/features/loop_unified_engine.md.';

COMMENT ON COLUMN project_loops.current_job_id IS
    'Display-only mirror of the in-flight turn when its width is 1 (cockpit '
    'links, MCP formatters). NULL for fan-out turns and between turns. The '
    'engine''s advance/heal correctness keys on current_stage_jobs, never '
    'on this column. docs/features/loop_unified_engine.md.';
