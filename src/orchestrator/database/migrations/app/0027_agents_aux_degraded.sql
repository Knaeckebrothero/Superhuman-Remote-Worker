-- migration:     0027_agents_aux_degraded.sql
-- description:   Surface silent auxiliary-model outages on the agent row.
--                Adds agents.aux_degraded, set from the AuxHealth snapshot the
--                agent now carries on its heartbeat (metrics.aux.degraded). The
--                Cockpit admin agents view renders a warning badge off this
--                flag; the compact failure detail rides in agents.metadata.aux
--                for the badge tooltip. Part of aux Phase 2
--                (docs/issues/surface_silent_aux_failures.md).
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. ADD COLUMN with a constant default is a
--                metadata-only change in PostgreSQL 11+ (no table rewrite).
-- locks:         brief AccessExclusiveLock on agents for the catalog update.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS aux_degraded BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN agents.aux_degraded IS
    'Latest heartbeat-reported auxiliary-model health: TRUE while the agent''s '
    'auxiliary LLM (memory extraction/curation/assembly, session titles) is '
    'sustained-failing. Set from metrics.aux.degraded; detail in metadata.aux. '
    'See docs/issues/surface_silent_aux_failures.md (aux Phase 2).';

COMMIT;
