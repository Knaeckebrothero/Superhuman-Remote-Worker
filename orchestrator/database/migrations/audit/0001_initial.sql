-- migration:     0001_initial.sql
-- description:   Initial audit-DB schema — replaces the MongoDB collections
--                llm_requests / agent_audit / chat_history (observability tier).
-- depends-on:    (none — first migration on srw-auditdb)
-- expected:      Fast on empty DB. Creates 3 partitioned parents + the current
--                month and N+2 monthly leaf partitions each.
-- locks:         Brief AccessExclusiveLock per CREATE TABLE / CREATE INDEX
--                (brand-new objects, no contention).
-- transactional: yes
--
-- Design: docs/features/postgres_audit_store_implementation.md §3 (copied
-- verbatim; live-validated on PostgreSQL 15.18). Runtime partition maintenance
-- (creation beyond this bootstrap, parent ANALYZE; retention deferred in the
-- lean-cut PR 1) lives in orchestrator/services/audit_partitions.py.
-- ============================================================================
-- migrations/audit/0001_initial.sql — PostgreSQL audit store (replaces MongoDB
-- collections llm_requests / agent_audit / chat_history).
--
-- Runs inside the migration runner's single transaction (advisory-locked,
-- checksum-tracked — orchestrator/database/migrate.py). No CREATE DATABASE,
-- no CREATE EXTENSION required. Targets PostgreSQL 16 (15.9+ compatible).
-- Requires a server built --with-lz4 (true for official/PGDG images); the
-- SET COMPRESSION statements below fail loudly otherwise.
--
-- Design: docs/features/postgres_audit_store.md. Three invariants enforced
-- here and in review:
--   1. agent_audit is APPEND-ONLY. The Mongo two-phase insert+update becomes
--      two INSERTs (event_phase 'pre' then 'post', correlated by pre_id).
--      Never add an UPDATE path.
--   2. DO NOT add a GIN index on agent_audit.payload (or any payload/request
--      JSONB column). It would re-introduce the JSONB write-amplification
--      class this design exists to avoid, and "search arbitrary payload
--      fields" is explicitly a non-goal (derived columns or an external
--      index are the answer if that feature ever lands).
--   3. NO foreign keys between these tables — see the soft-reference note
--      on agent_audit.request_id below.
--
-- Partition maintenance (creation beyond the bootstrap below, retention,
-- parent ANALYZE) is owned by orchestrator/services/audit_partitions.py,
-- NOT by later migrations: DETACH PARTITION CONCURRENTLY cannot run inside
-- the runner's transaction.
-- ============================================================================

-- Deterministic month boundaries for partition bounds regardless of server
-- timezone config (SET LOCAL: scoped to the runner's transaction).
SET LOCAL timezone = 'UTC';

-- ============================================================================
-- llm_requests — one row per LLM call (main loop + auxiliary/vision/audio).
-- Mirrors LLMArchiver.archive() (src/core/archiver.py): every Mongo document
-- field has a column or a JSONB home. INSERT-only, no post phase.
-- ============================================================================
CREATE TABLE llm_requests (
    id                  BIGSERIAL,
    job_id              UUID         NOT NULL,
    agent_type          TEXT,
    call_type           TEXT         NOT NULL DEFAULT 'main',
    model               TEXT         NOT NULL,
    iteration           INTEGER,
    timestamp           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    latency_ms          INTEGER,
    request             JSONB        NOT NULL,
    response            JSONB        NOT NULL,
    metadata            JSONB,
    auxiliary_metadata  JSONB,
    metrics             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- A bare BIGSERIAL PK is invalid on a partitioned table (unique
    -- constraints must include the partition key). id is still globally
    -- unique in practice: single sequence, append-only, no id reuse.
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

COMMENT ON TABLE llm_requests IS
    'Full LLM request/response archive, one row per call. Written by the '
    'agent''s LLMArchiver only; read by /api/requests/{id} and '
    '/api/jobs/{id}/llm-requests. Monthly partitions, 90-day retention.';
COMMENT ON COLUMN llm_requests.agent_type IS
    'Naming crossover preserved from Mongo: carries config.agent_id values '
    '(e.g. "universal", "vision", "transcription"), not a pod identity. '
    'There is no agent_id column because the writer has no such field.';
COMMENT ON COLUMN llm_requests.call_type IS
    'main | summarization | memory_extraction | memory_assembly | '
    'knowledge_curation | auxiliary | vision | transcription (open set).';
COMMENT ON COLUMN llm_requests.timestamp IS
    'Writer-supplied UTC insert time (partition key). DEFAULT now() is a '
    'backstop only.';
COMMENT ON COLUMN llm_requests.request IS
    'Full untruncated message bodies: {messages:[...], message_count, '
    'tools?, tool_count?, model_kwargs?}. Dominates row size — LZ4 TOAST.';
COMMENT ON COLUMN llm_requests.metrics IS
    '{input_chars, output_chars, tool_calls, token_usage:{...}}. '
    'token_usage lives HERE (nested), not top-level — the reader surfaces '
    'it in /llm-requests.';

ALTER TABLE llm_requests ALTER COLUMN request            SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN response           SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN metadata           SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN metrics            SET COMPRESSION lz4;

-- Serves: list_llm_requests (job page ordered by timestamp) and per-job
-- maintenance queries. get_request(id) uses the PK's id prefix across the
-- few attached partitions. No call_type or GIN indexes: their only would-be
-- consumers (LLMArchiver.get_job_stats/get_audit_stats) are dead code with
-- zero callers. Add later via a .notx.sql migration if a consumer appears.
CREATE INDEX llm_requests_job_ts_idx ON llm_requests (job_id, timestamp);

-- ============================================================================
-- agent_audit — APPEND-ONLY step trace.
-- 'pre' rows are written at call dispatch (audit_step / audit_tool_call /
-- audit_llm_call); 'post' rows replace the old Mongo update_one (result
-- backfill) and carry only the delta payload. Readers stitch pairs via
-- pre_id and serve ONE logical row per pre row (counts, step_number and
-- wire shape therefore match the Mongo store exactly).
-- ============================================================================
CREATE TABLE agent_audit (
    id            BIGSERIAL,
    job_id        UUID         NOT NULL,
    agent_type    TEXT,
    iteration     INTEGER,
    step_type     TEXT         NOT NULL,
    node_name     TEXT,
    phase         TEXT,
    phase_number  INTEGER,
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    latency_ms    INTEGER,
    event_phase   TEXT         NOT NULL DEFAULT 'pre',
    pre_id        BIGINT,
    request_id    BIGINT,
    payload       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    metadata      JSONB,
    PRIMARY KEY (id, timestamp),
    CONSTRAINT agent_audit_event_phase_check
        CHECK (event_phase IN ('pre', 'post')),
    -- post rows must point at their pre row; pre rows must not.
    CONSTRAINT agent_audit_pre_id_check
        CHECK ((event_phase = 'pre') = (pre_id IS NULL))
) PARTITION BY RANGE (timestamp);

COMMENT ON TABLE agent_audit IS
    'Append-only agent step trace (tool calls, LLM calls, checks, errors, '
    'phase transitions, memory ops). Two-phase calls = two rows (pre/post) '
    'correlated by pre_id; one LOGICAL step per pre row. Monthly '
    'partitions, 90-day retention. NEVER UPDATE rows here and NEVER add a '
    'GIN index on payload (see file header).';
COMMENT ON COLUMN agent_audit.step_type IS
    'Open set; 13 values observed: initialize, llm, tool, check, warning, '
    'error, phase_transition, phase_complete, feedback_resume, '
    'memory_inject, memory_dedup, memory_store, memory_retrieve.';
COMMENT ON COLUMN agent_audit.event_phase IS
    '''pre'' = written at dispatch (tool/llm result fields null inside '
    'payload); ''post'' = second INSERT carrying the result delta that the '
    'Mongo store applied as an in-place $set.';
COMMENT ON COLUMN agent_audit.pre_id IS
    'On post rows: agent_audit.id of the pre row this completes. Soft '
    'self-reference, deliberately NOT a FK (self-referential FKs between '
    'partitions sit on the most corruption-prone partitioning code path '
    'of the 15.9/16.5/16.9 minor-release fixes, and add nothing here).';
COMMENT ON COLUMN agent_audit.request_id IS
    'Soft reference to llm_requests.id (set on llm post rows; surfaced to '
    'the wire inside payload.llm.request_id). DELIBERATELY NOT A FOREIGN '
    'KEY: partitioned-to-partitioned FKs make partition retention '
    'permanently unexecutable when windows differ (chat_history keeps 365d '
    'against llm_requests'' 90d, so referenced partitions would still be '
    'referenced at detach time and every DETACH errors), SHARE-lock the '
    'referencing tables during detach, and ride the same bug-ridden code '
    'path as pre_id above. Orphaned ids after the referenced row ages out '
    'are by design — treat as opaque correlation ids (GitLab loose-FK / '
    'TimescaleDB norm for log stores).';
COMMENT ON COLUMN agent_audit.payload IS
    'The writer''s free-form data dict (Mongo merged it at document top '
    'level; readers re-splat it over the row dict for wire parity). Known '
    'quirk preserved: at step_type=phase_complete the payload contains a '
    '"phase" OBJECT that shadows the phase TEXT column when splatted — '
    'readers must not assume phase=strategic|tactical on those rows. '
    'NO GIN INDEX — see file header.';

ALTER TABLE agent_audit ALTER COLUMN payload  SET COMPRESSION lz4;
ALTER TABLE agent_audit ALTER COLUMN metadata SET COMPRESSION lz4;

-- Logical-row (pre) indexes: every read path filters event_phase='pre' and
-- orders by id (the step_number source). Partial indexes keep post rows out.
CREATE INDEX agent_audit_job_id_idx
    ON agent_audit (job_id, id) WHERE event_phase = 'pre';
-- FilterCategory (step_type IN (...)) + graph-delta queries
-- (step_type='tool' AND payload->'tool'->>'name' = ANY(...)): the index
-- narrows to the job's tool rows; the name filter is applied per-row, which
-- is bounded because every such query is job-scoped. This replaces the
-- design doc's global expression index on payload->'tool'->>'name' (a
-- cross-job index cannot serve job-scoped queries better than this, and
-- fewer per-partition indexes preserves fast-path lock slots).
CREATE INDEX agent_audit_job_step_idx
    ON agent_audit (job_id, step_type, id) WHERE event_phase = 'pre';
-- Pre/post stitch join (post.pre_id = pre.id).
CREATE INDEX agent_audit_pre_id_idx
    ON agent_audit (pre_id) WHERE event_phase = 'post';

-- ============================================================================
-- chat_history — one row per main-loop turn (clean conversational delta).
-- Written only via the archive(call_type='main') cascade. 365-day retention.
-- ============================================================================
CREATE TABLE chat_history (
    id            BIGSERIAL,
    job_id        UUID         NOT NULL,
    agent_type    TEXT,
    iteration     INTEGER,
    model         TEXT,
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    latency_ms    INTEGER,
    phase         TEXT,
    phase_number  INTEGER,
    request_id    BIGINT,
    inputs        JSONB        NOT NULL,
    response      JSONB        NOT NULL,
    reasoning     JSONB,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

COMMENT ON TABLE chat_history IS
    'Conversational delta per main-loop LLM turn: inputs since the last AI '
    'message + the response, with previews. Monthly partitions, 365-day '
    'retention (longer than llm_requests — a reason request_id is a soft '
    'reference, not a FK).';
COMMENT ON COLUMN chat_history.request_id IS
    'Soft reference to llm_requests.id (no FK — see '
    'agent_audit.request_id). Dangles by design once the llm_requests row '
    'ages out of its 90-day window.';
COMMENT ON COLUMN chat_history.iteration IS
    'Nullable but always written by the archiver (Mongo wrote null '
    'explicitly; readers treat null as absent-equivalent).';
COMMENT ON COLUMN chat_history.inputs IS
    'List of {type: human|tool, content, content_preview<=500, '
    'tool_call_id?, tool_name?} — messages after the last AIMessage, '
    'system messages excluded.';
COMMENT ON COLUMN chat_history.response IS
    '{content, content_preview<=500, has_tool_calls, '
    'tool_calls?:[{id, name, args_preview<=200}]}.';

ALTER TABLE chat_history ALTER COLUMN inputs    SET COMPRESSION lz4;
ALTER TABLE chat_history ALTER COLUMN response  SET COMPRESSION lz4;
ALTER TABLE chat_history ALTER COLUMN reasoning SET COMPRESSION lz4;

-- Serves the chat pane + bulk sync (ORDER BY timestamp, id). No request_id
-- index: no production reader joins chat->llm_requests today.
CREATE INDEX chat_history_job_ts_idx ON chat_history (job_id, timestamp);

-- ============================================================================
-- Partition bootstrap: current month + 2 months lookahead for each table,
-- computed — never hardcoded. NO DEFAULT partition, deliberately: a default
-- converts "partition missing" from a loud SQLSTATE 23514 insert error
-- (which the writer logs and the maintenance loop alarms on) into silent
-- data-parking that later blocks ATTACH and forbids DETACH CONCURRENTLY.
--
-- Storage parameters MUST be applied per leaf partition — partitioned
-- parents reject them ("unrecognized parameter", verified on 15.18/16).
-- orchestrator/services/audit_partitions.py applies the same settings to
-- every partition it creates; keep the two lists in sync.
--   fillfactor=100                       append-only, no HOT-update headroom
--                                        needed (explicit = documentation)
--   autovacuum_vacuum_insert_*           insert-driven vacuums run early and
--                                        incrementally (visibility map /
--                                        index-only scans stay effective)
--   autovacuum_analyze_scale_factor      fresher per-leaf stats on the
--                                        active month
--   autovacuum_freeze_min_age=0          insert vacuums freeze immediately;
--                                        no anti-wraparound storm on cold
--                                        partitions right before they drop
-- ============================================================================
DO $$
DECLARE
    parent TEXT;
    m      DATE;
    part   TEXT;
BEGIN
    FOREACH parent IN ARRAY ARRAY['llm_requests', 'agent_audit', 'chat_history'] LOOP
        FOR i IN 0..2 LOOP
            m    := (date_trunc('month', now()) + make_interval(months => i))::date;
            part := parent || '_p' || to_char(m, 'YYYY_MM');
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                part, parent, m, (m + interval '1 month')::date
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
    END LOOP;
END $$;

-- Partitioned parents are not autovacuum-analyzed; seed parent-level stats
-- now (empty is fine — establishes the entries) and let the maintenance
-- loop's periodic ANALYZE keep them fresh.
ANALYZE llm_requests;
ANALYZE agent_audit;
ANALYZE chat_history;
