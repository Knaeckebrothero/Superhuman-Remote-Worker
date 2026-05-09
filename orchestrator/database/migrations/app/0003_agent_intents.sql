-- migration:     0003_agent_intents.sql
-- description:   Add intents JSONB column to agents table.
--
--                Phase 1 of the unified instance lifecycle redesign
--                (docs/features/unified_instance_lifecycle.md). Intents are
--                orchestrator-set hints the agent reads from heartbeat
--                responses and reacts to (drain, version upgrade, etc.).
--                The column exists so the heartbeat handler can write to
--                it from the orchestrator side without ever being
--                overwritten by the agent's reported status — closing the
--                "draining is a write-only label" bug structurally.
--
--                Shape (informal):
--                  {
--                    "should_drain": true|false,
--                    "drain_reason": "stale_image" | "scale_down" | ...,
--                    "target_version": "<build_sha>",
--                    "set_at": "<ISO timestamp>"
--                  }
--
--                Phase 1a only adds the column; nothing reads or writes
--                it yet. The cutover from the Phase 0 stopgap (CASE on
--                status='draining' in the heartbeat handler) to
--                intent-based drain happens in Phase 1b alongside the
--                AgentInstanceManager wiring.
-- depends-on:    0002_collapse_thread_status.sql
-- expected:      < 1s; agents is small.
-- locks:         AccessExclusiveLock briefly for ALTER TABLE ADD COLUMN
--                (NOT NULL with default is metadata-only on PG 11+, no
--                table rewrite).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS intents JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN agents.intents IS
    'Orchestrator-set drain/upgrade intents. Read by the heartbeat '
    'response and reconciler; never written by the agent.';

COMMIT;
