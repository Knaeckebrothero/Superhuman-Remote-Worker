-- migration:     0018_project_loop_ttl_effects.sql
-- description:   Response-loss idempotency ledger for the project-loop KB TTL
--                decrement. The completion handoff inserts one immutable turn
--                identity and decrements knowledge_index in the SAME vector-DB
--                transaction, so retrying after a lost response cannot consume
--                two cycles. App-DB markers cannot close this cross-DB window.
-- depends-on:    0007_knowledge_index_ttl.sql
-- expected:      < 1s; creates one empty table and its primary-key index.
-- locks:         catalog-only locks for a new relation; no existing rows read.
-- transactional: YES.

CREATE TABLE IF NOT EXISTS project_loop_ttl_effects (
    loop_id UUID NOT NULL,
    total_jobs_run INT NOT NULL CHECK (total_jobs_run >= 0),
    completed_member_id UUID NOT NULL,
    project_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (loop_id, total_jobs_run)
);

COMMENT ON TABLE project_loop_ttl_effects IS
    'Immutable project-loop turn identities whose knowledge_index cycle TTL decrement committed. A key collision with different project/member identity is corruption and callers fail closed.';
