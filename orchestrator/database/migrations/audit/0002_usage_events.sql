-- migration:     0002_usage_events.sql
-- description:   Usage-metering ledger (append-only) — the canonical cost/usage
--                spine for LLM tokens + workspace compute (query/storage later).
-- depends-on:    0001_initial.sql (shares the auditdb partition machinery).
-- expected:      Fast on empty DB. Creates 1 partitioned parent + the current
--                month and N+2 monthly leaf partitions.
-- locks:         Brief AccessExclusiveLock per CREATE TABLE / CREATE INDEX
--                (brand-new objects, no contention).
-- transactional: yes
--
-- Design: docs/features/observability_and_quotas.md — the *locked* usage_events
-- schema (its "The spine: one usage ledger" section). This is the ledger spine
-- that docs/features/usage_monitoring_and_rate_limiting.md Slice 4 implements,
-- now gateway-sourced (LLM rows materialized from the LiteLLM spend log; compute
-- rows emitted by the orchestrator at workspace open/close).
--
-- Three invariants, mirroring 0001's append-only tables:
--   1. APPEND-ONLY. Every row is a *finalized, costed* event — emitters INSERT,
--      never UPDATE. A workspace's open interval is mutable bookkeeping and lives
--      in the app-DB workspace_intervals table; only the closed (duration-known)
--      vcpu-hour/gib-hour rows land here. Never add an UPDATE path.
--   2. DO NOT add a GIN index on details (or any JSONB column). Same JSONB
--      write-amplification class 0001 exists to avoid; "search arbitrary detail
--      fields" is a non-goal (derive a column or roll up to usage_daily instead).
--   3. NO foreign keys. user_id / project_id / ref_id are *soft* references into
--      the app DB (a different server) — rows outlive their referents by design
--      (a job's cost survives the job's deletion; an aged-out user id dangles).
--
-- Partition maintenance (creation beyond the bootstrap below, retention, parent
-- ANALYZE) is owned by orchestrator/services/audit_partitions.py, where
-- usage_events is registered with `ts` (not `timestamp`) as its partition
-- column. As with 0001, NO DDL belongs in later migrations — DETACH PARTITION
-- CONCURRENTLY cannot run inside the runner's transaction.
-- ============================================================================

-- Deterministic month boundaries regardless of server timezone (SET LOCAL:
-- scoped to the runner's transaction), matching 0001's bootstrap.
SET LOCAL timezone = 'UTC';

-- ============================================================================
-- usage_events — one row per metered resource *dimension*. A workspace pod emits
-- a vcpu-hour row AND a gib-hour row; an LLM call emits a prompt-token row AND a
-- completion-token row. This keeps quantity/unit/rate/cost scalar and 1:1 with
-- the app-DB usage_rates table.
-- ============================================================================
CREATE TABLE usage_events (
    id          BIGSERIAL,
    -- When the usage occurred / the interval ended. The PARTITION KEY. Set by
    -- the emitter (request start time for LLM; ended_at for a compute interval),
    -- deterministic per logical event so the dedupe index below is stable across
    -- re-emits. DEFAULT now() is a backstop only.
    ts          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- Attribution target. Nullable: system events and fleet-key-fallback LLM
    -- traffic (no user/project on the scoped key) attribute to neither.
    user_id     UUID,
    project_id  UUID,
    ref_kind    TEXT,                       -- 'job' | 'thread' (the owning entity)
    ref_id      UUID,                       -- job_id or thread_id
    category    TEXT         NOT NULL,      -- 'llm' | 'compute' | 'query' | 'storage'
    resource    TEXT         NOT NULL,      -- '<model_id>' | 'workspace_pod' | 'agent_pod' | ...
    quantity    NUMERIC      NOT NULL,      -- 4096 (tokens), 1.83 (vcpu-hours), ...
    unit        TEXT         NOT NULL,      -- 'prompt-token'|'completion-token'|'vcpu-hour'|'gib-hour'|...
    rate_usd    NUMERIC,                    -- snapshot of the rate applied (NULL = unpriced)
    cost_usd    NUMERIC,                    -- quantity * rate_usd, stored (NULL until priced)
    -- Idempotency key for the at-least-once emitters (the design mandates
    -- at-least-once delivery). source ∈ {'litellm','orchestrator'}; source_id is
    -- the LiteLLM request_id (LLM) or a deterministic interval key (compute).
    -- (source, source_id, unit) identifies a logical row; ts is folded in only
    -- because a partitioned-table unique index must include the partition key.
    source      TEXT         NOT NULL,
    source_id   TEXT         NOT NULL,
    details     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- A bare BIGSERIAL PK is invalid on a partitioned table (unique constraints
    -- must include the partition key). id stays globally unique in practice:
    -- single sequence, append-only, no id reuse.
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE usage_events IS
    'Append-only usage/cost ledger, one row per metered resource dimension. '
    'Written by the orchestrator (compute intervals + LiteLLM spend-log '
    'materialization); read by /api/usage and the usage_daily rollup. Monthly '
    'partitions on ts (audit-store machinery), partition column is ts not '
    'timestamp. NEVER UPDATE rows and NEVER add a GIN index on details.';
COMMENT ON COLUMN usage_events.ts IS
    'Usage/interval-end time (partition key, UTC). Emitter-supplied so the '
    'dedupe index is stable; DEFAULT now() is a backstop only.';
COMMENT ON COLUMN usage_events.category IS
    'llm | compute | query | storage (open set). query/storage reserved.';
COMMENT ON COLUMN usage_events.quantity IS
    'Scalar amount in `unit`. One row per dimension keeps this 1:1 with a '
    'usage_rates (category, resource, unit) row.';
COMMENT ON COLUMN usage_events.rate_usd IS
    'Snapshot of the rate applied at write time — history is immutable, a later '
    'rate edit never rewrites past cost. NULL when the resource is unpriced '
    '(homelab models today): quantity is still recorded, cost is just absent.';
COMMENT ON COLUMN usage_events.source_id IS
    'Per-source idempotency id: LiteLLM request_id (LLM) or a deterministic '
    'workspace-interval key (compute). With source + unit it dedupes re-emits '
    '(ON CONFLICT DO NOTHING) so the at-least-once emitters cannot double-count.';
COMMENT ON COLUMN usage_events.details IS
    '{pod_name, started_at, ended_at, cpu_millicores, mem_bytes, model, '
    'request_id, ...} — free-form context. LZ4 TOAST. NO GIN INDEX.';

ALTER TABLE usage_events ALTER COLUMN details SET COMPRESSION lz4;

-- At-least-once dedupe: a re-polled spend log or a re-emitted interval close
-- collides here and ON CONFLICT DO NOTHING drops it. Must include ts (partition
-- key) to be a valid unique index on a partitioned table; ts is deterministic
-- per logical row so this is exact, not approximate.
CREATE UNIQUE INDEX usage_events_dedupe_idx
    ON usage_events (source, source_id, unit, ts);

-- Read paths: per-user usage over a window (the /api/usage endpoint + the
-- usage_daily rollup group by user) and per-job/thread cost (the job detail
-- line). Project rollups ride the rollup table; add a (project_id, ts) index
-- only if a raw project-scoped read appears (kept lean, 0001's philosophy).
CREATE INDEX usage_events_user_ts_idx ON usage_events (user_id, ts);
CREATE INDEX usage_events_ref_idx     ON usage_events (ref_id, ts);

-- ============================================================================
-- Partition bootstrap: current month + 2 months lookahead. Computed, never
-- hardcoded. NO DEFAULT partition (a misrouted row must fail loudly with 23514,
-- not silently park and later block ATTACH / forbid DETACH CONCURRENTLY).
-- Storage params MUST be applied per leaf (parents reject them) and MUST stay in
-- sync with audit_partitions.STORAGE_PARAMS — identical to 0001's set.
-- ============================================================================
DO $$
DECLARE
    m    DATE;
    part TEXT;
BEGIN
    FOR i IN 0..2 LOOP
        m    := (date_trunc('month', now()) + make_interval(months => i))::date;
        part := 'usage_events_p' || to_char(m, 'YYYY_MM');
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF usage_events FOR VALUES FROM (%L) TO (%L)',
            part, m, (m + interval '1 month')::date
        );
        EXECUTE format(
            'ALTER TABLE %I SET ('
            'fillfactor = 100, '
            'autovacuum_vacuum_insert_scale_factor = 0.05, '
            'autovacuum_vacuum_insert_threshold = 10000, '
            'autovacuum_analyze_scale_factor = 0.02, '
            'autovacuum_freeze_min_age = 0)',
            part
        );
    END LOOP;
END $$;

-- Partitioned parents are not autovacuum-analyzed; seed parent-level stats now
-- (empty is fine) and let audit_partitions' periodic ANALYZE keep them fresh.
ANALYZE usage_events;
