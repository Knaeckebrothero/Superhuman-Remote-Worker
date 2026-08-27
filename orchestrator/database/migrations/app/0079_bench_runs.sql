-- migration:     0079_bench_runs.sql
-- description:   Persist server-side Job Bench run specifications and their
--                submission ledgers so running benchmarks resume after an
--                orchestrator restart.
-- depends-on:    0078_project_knowledge_repo.sql
-- expected:      < 1s; creates one empty control-plane table.
-- locks:         No locks on existing application tables beyond the brief
--                catalog locks needed to validate the users foreign key.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE bench_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'paused', 'done', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    spec JSONB NOT NULL CHECK (jsonb_typeof(spec) = 'object'),
    state JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(state) = 'array')
);

COMMIT;
