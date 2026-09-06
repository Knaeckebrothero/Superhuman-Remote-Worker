-- migration:     0046_jobs_dispatchable_partial_idx.notx.sql
-- description:   Partial covering index for the auto-assign dispatcher's hot
--                query (PostgresDB.get_dispatchable_jobs). It scans jobs for
--                the dispatchable set — assigned_agent_id IS NULL AND
--                freeze_data IS NULL AND status IN ('created','paused') — then
--                ORDER BY priority DESC, created_at ASC LIMIT N. This index
--                pre-filters (partial WHERE) and pre-sorts (mixed-direction
--                key) so the planner walks a small candidate stream and early-
--                exits at LIMIT instead of a full seq-scan + Sort of jobs.
--
--                NOT index-only: get_dispatchable_jobs projects 13 columns and
--                keeps a residual COALESCE(cloud_baseline...) <> 'seeding'
--                filter + a correlated recursive NOT EXISTS (ancestor cascade
--                guard) as post-index work. The win is the pre-filtered,
--                pre-sorted, LIMIT-bounded candidate scan.
--
--                The mixed-direction key (priority DESC, created_at ASC) is
--                REQUIRED: a plain btree can serve only all-ASC or all-DESC, so
--                a same-direction index can't satisfy this ORDER BY without a
--                Sort. It serves ONLY this query — the llm-outage sweeper
--                (get_dispatchable_llm_outage_jobs) and the cascade subquery
--                lack the freeze_data IS NULL / status terms.
--
--                Predicate contract: get_dispatchable_jobs embeds the statuses
--                as LITERAL SQL (status IN ('created','paused')). This index
--                can only be used while they stay literal — rewriting to
--                status = ANY($1) makes the predicate non-immutable at plan
--                time and permanently disables it. A code comment on the method
--                mirrors this warning.
--
--                HOT trade-off: the predicate columns (assigned_agent_id,
--                freeze_data, status) now count as indexed, but status flips
--                are already non-HOT via idx_jobs_status — no new regression.
-- depends-on:    0001_initial.sql (jobs table + idx_jobs_status)
-- expected:      seconds on dev; on a large jobs table CONCURRENTLY takes
--                longer but never blocks writers.
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO. CREATE INDEX CONCURRENTLY can't run inside a transaction
--                block; hence the .notx.sql suffix the runner recognises.
-- runbook:       If CONCURRENTLY is interrupted (crash/timeout) it leaves an
--                INVALID index behind. IF NOT EXISTS will then NOT rebuild it
--                (the name exists), so the build silently no-ops. Recover with:
--                    DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_dispatchable;
--                then re-run this migration. Detect stragglers via:
--                    SELECT indexrelid::regclass FROM pg_index
--                    WHERE NOT indisvalid
--                      AND indexrelid::regclass::text = 'idx_jobs_dispatchable';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_dispatchable
    ON jobs (priority DESC, created_at ASC)
    WHERE assigned_agent_id IS NULL
      AND freeze_data IS NULL
      AND status IN ('created', 'paused');
