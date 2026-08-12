-- migration:     0115_run_queue.sql
-- description:   Stateless-agents S1 substrate (docs/features/stateless_agents.md
--                §5.1/§5.2): the run_queue work/lease table with its claim,
--                expiry and dedup indexes, plus threads.execution_lane — the
--                per-thread lane partition flag ('pinned' | 'stateless').
-- depends-on:    0114_compute_interval_epoch_shape_repair.sql
-- expected:      < 5s. New table + indexes on it (empty, instant); the threads
--                ADD COLUMN has a constant default (catalog-only since PG 11).
-- locks:         Brief ACCESS EXCLUSIVE on threads for the ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- The one small hot table every stateless executor polls (Solid Queue's
-- lesson: claim against a tiny indexed set). One row per work unit; the
-- shared claim/lease/fence SQL lives in src/shared/run_queue/.
CREATE TABLE run_queue (
    unit_id       UUID PRIMARY KEY,
    unit_kind     TEXT NOT NULL,
    dedup_key     TEXT,
    state         TEXT NOT NULL DEFAULT 'queued',
    priority      INT  NOT NULL DEFAULT 0,
    fair_key      TEXT,
    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts_since_completion INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 5,
    lease_token   BIGINT NOT NULL DEFAULT 0,
    leased_by     TEXT,
    leased_until  TIMESTAMPTZ,
    input_seq     BIGINT,
    consumed_seq  BIGINT,
    queued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    enqueue_ord   BIGINT GENERATED ALWAYS AS IDENTITY
);

COMMENT ON TABLE run_queue IS
    'Work queue + recorded lease for stateless execution '
    '(docs/features/stateless_agents.md §5.1/§5.2). One DURABLE row per unit '
    '(thread, job, or bg task): rows are never deleted while the unit lives, '
    'so lease_token stays monotonic — delete-and-reinsert would reset it and '
    'break fencing. State machine: queued -> leased -> {done | queued | '
    'parked}; states are app-validated by design (no CHECK), values: queued, '
    'leased, done, parked. All writes go through src/shared/run_queue/.';

COMMENT ON COLUMN run_queue.unit_id IS
    'Polymorphic unit id: thread_id (session_turn), job_id (worker_batch), or '
    'a fresh task id (bg_task). Deliberately NO foreign key — the queue '
    'outlives and predates its referents across kinds.';

COMMENT ON COLUMN run_queue.unit_kind IS
    'session_turn | worker_batch | bg_task (app-validated). Claims filter on '
    'kind so bg work never starves interactive claims.';

COMMENT ON COLUMN run_queue.dedup_key IS
    'Collapse key for collapsible bg work (e.g. cloud_push:<thread>). Dedup '
    'is QUEUED-ONLY (partial unique index below): one pending and one running '
    'instance may coexist; a signal arriving mid-run must produce a new '
    'pending row, never be swallowed (§5.1).';

COMMENT ON COLUMN run_queue.lease_token IS
    'Kleppmann fencing token: MONOTONIC per unit, bumped on EVERY claim and '
    'EVERY reaper steal, NEVER reset by enqueue/complete/release. Every '
    'persist transaction fences on it (fence_lease, FOR SHARE); a zombie '
    'writer holding a stale token is rejected at persist time.';

COMMENT ON COLUMN run_queue.leased_by IS
    'Pod name. Diagnostics only — never correctness; ownership is proven by '
    'lease_token alone.';

COMMENT ON COLUMN run_queue.input_seq IS
    'Newest thread_messages.seq enqueued for this unit (input watermark). '
    'Input arriving during a leased turn bumps ONLY this column — flipping a '
    'leased row''s state would break the lease.';

COMMENT ON COLUMN run_queue.consumed_seq IS
    'Newest seq a COMPLETED turn has answered (consumed watermark). '
    'Completion re-queues when input_seq is ahead; every claim compares the '
    'two watermarks BEFORE invoking the LLM (skip-if-answered) so a steal '
    'landing between final persist and completion cannot double-answer.';

COMMENT ON COLUMN run_queue.attempts_since_completion IS
    'Claims since the last successful completion (incremented at claim, not '
    'at release). Reset to 0 only by complete_unit and unpark_unit. The '
    'reaper parks the unit when it reaches max_attempts.';

COMMENT ON COLUMN run_queue.run_after IS
    'Not claimable before this instant: scheduling + retry backoff. Reset by '
    'completion; pushed out by error release and reaper steals.';

COMMENT ON COLUMN run_queue.queued_at IS
    'Fairness position: reset on completion, voluntary release and steal, so '
    'claim order (priority DESC, queued_at) round-robins within a priority '
    'class instead of letting the oldest unit win every cycle.';

COMMENT ON COLUMN run_queue.fair_key IS
    'Per-user fairness dimension for session_turn claims (user id). The '
    'column ships with S1; the per-key round-robin CTE follows (§5.3.7).';

COMMENT ON COLUMN run_queue.enqueue_ord IS
    'Insertion-order tiebreak: final ORDER BY key of the claim so '
    'equal-timestamp claims are deterministic FIFO. Never reused, never '
    'meaningful beyond ordering.';

-- Claim hot set: tiny partial index the 250ms-1s pod polls walk in claim
-- order (priority DESC, queued_at, enqueue_ord). New table => plain CREATE
-- INDEX is instant and transactional.
CREATE INDEX idx_run_queue_claim
    ON run_queue (unit_kind, priority DESC, queued_at, enqueue_ord)
    WHERE state = 'queued';

-- Reaper scan: expired leases only.
CREATE INDEX idx_run_queue_expiry
    ON run_queue (leased_until)
    WHERE state = 'leased';

-- Queued-only dedup (§5.1): collapses repeated collapsible signals while one
-- is pending; deliberately excludes 'leased' so a signal during a run is
-- never swallowed.
CREATE UNIQUE INDEX idx_run_queue_dedup
    ON run_queue (unit_kind, dedup_key)
    WHERE dedup_key IS NOT NULL AND state = 'queued';

-- Lane partition flag for the coexistence ruling (§5.4.4 as adapted by the
-- implementation log decision 1: jobs.runner_kind carries grant semantics, so
-- threads get their own column). Constant default => catalog-only ALTER.
-- App-validated ('pinned' | 'stateless') — deliberately NO CHECK constraint:
-- adding a validated CHECK to this existing hot table would force the
-- NOT VALID + VALIDATE migration split for no integrity gain.
ALTER TABLE threads
    ADD COLUMN execution_lane TEXT NOT NULL DEFAULT 'pinned';

COMMENT ON COLUMN threads.execution_lane IS
    'Which execution plane serves this thread: ''pinned'' (registered-agent '
    'pod, the default) or ''stateless'' (run_queue claim by any pod). '
    'App-validated by design — no CHECK. See '
    'docs/features/stateless_agents.md §5.4.4.';

COMMIT;
