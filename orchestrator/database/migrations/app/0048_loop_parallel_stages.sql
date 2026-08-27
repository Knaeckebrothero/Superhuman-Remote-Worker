-- migration:     0048_loop_parallel_stages.sql
-- description:   Parallel analysis stages for project loops. Adds
--                current_stage_jobs — the in-flight members of a fan-out stage —
--                so a role_sequence entry can be an ARRAY of roles that run
--                concurrently and barrier before the loop rotates (e.g.
--                [["scholar","product-qa"], "critic"] runs scholar ∥ product-qa,
--                then a single critic triages both streams). Single-role stages
--                are unchanged: they keep using current_job_id and leave this
--                '[]'. docs/features/loop_parallel_stages.md (Phase 1).
-- depends-on:    0035_project_loops.sql
-- expected:      < 1s (one ADD COLUMN with a constant default; metadata-only,
--                no table rewrite on PG11+).
-- locks:         Brief ACCESS EXCLUSIVE on project_loops (tiny control table).
-- transactional: yes
--
-- Design notes:
--   * Membership is the SET of not-yet-terminal jobs in the current fan-out
--     stage. A single-role stage never populates it (current_job_id is the
--     pointer); a parallel stage sets current_job_id=NULL and lists its members
--     here. The loop rotates only when the LAST member finishes — detected by an
--     atomic "drain this to [] iff every member is terminal" UPDATE (the barrier,
--     services/... claim_project_loop_stage_barrier). Membership is immutable
--     for the stage's life (drained in one shot, never shrunk incrementally), so
--     the torn-advance recovery has exactly one signature to reason about:
--     current_job_id IS NULL AND current_stage_jobs = '[]' (same as the
--     single-role tear — see docs/issues/loop_advance_nonatomic_wedges_loop.md).
--   * Kept as JSONB (not uuid[]) to mirror role_sequence's JSONB encoding and
--     reuse the existing update_project_loop JSONB path; the barrier query casts
--     elements to uuid where it needs to join jobs.
-- ============================================================================

ALTER TABLE project_loops
    ADD COLUMN IF NOT EXISTS current_stage_jobs JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_stage_jobs_is_array
        CHECK (jsonb_typeof(current_stage_jobs) = 'array');

COMMENT ON COLUMN project_loops.current_stage_jobs IS
    'In-flight members of a parallel (fan-out) role_sequence stage — the jobs '
    'the loop barriers on before rotating. Empty for single-role stages, which '
    'use current_job_id instead. Populated by the advance/start spawn; drained '
    'to [] by the atomic last-member barrier. docs/features/loop_parallel_stages.md.';
