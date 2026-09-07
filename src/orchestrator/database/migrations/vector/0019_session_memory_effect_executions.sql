-- migration:     0019_session_memory_effect_executions.sql
-- description:   Destination-side idempotency ledger for stateless-session
--                final-memory effects. The ledger identity and every RecallStore
--                mutation commit in one vector-DB transaction, so an app-DB
--                receipt loss cannot insert or bump the same turn twice.
-- depends-on:    0018_project_loop_ttl_effects.sql
-- expected:      < 1s; creates one empty table and its primary-key index.
-- locks:         catalog-only locks for a new relation; no existing rows read.
-- transactional: YES.

CREATE TABLE IF NOT EXISTS session_memory_effect_executions (
    producer_id UUID NOT NULL,
    effect_name TEXT NOT NULL,
    thread_id UUID NOT NULL,
    input_message_id UUID NOT NULL,
    turn_number INT NOT NULL,
    boundary_seq BIGINT NOT NULL,
    end_seq BIGINT NOT NULL,
    memory_scope_kind TEXT NOT NULL,
    memory_scope_id UUID NOT NULL,
    state TEXT NOT NULL DEFAULT 'writing',
    extracted_count INT,
    stored_count INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,

    PRIMARY KEY (producer_id, effect_name),

    CONSTRAINT session_memory_effect_name CHECK (
        effect_name = 'final_memory_extraction'
    ),
    CONSTRAINT session_memory_effect_turn_positive CHECK (turn_number > 0),
    CONSTRAINT session_memory_effect_seq_window CHECK (
        boundary_seq > 0 AND end_seq >= boundary_seq
    ),
    CONSTRAINT session_memory_effect_scope CHECK (
        memory_scope_kind IN ('thread', 'project')
        AND (
            memory_scope_kind <> 'thread'
            OR memory_scope_id = thread_id
        )
    ),
    CONSTRAINT session_memory_effect_state CHECK (
        state IN ('writing', 'done')
    ),
    CONSTRAINT session_memory_effect_terminal_shape CHECK (
        (
            state = 'writing'
            AND extracted_count IS NULL
            AND stored_count IS NULL
            AND completed_at IS NULL
        )
        OR (
            state = 'done'
            AND extracted_count >= 0
            AND stored_count >= 0
            AND stored_count <= extracted_count
            AND completed_at IS NOT NULL
        )
    )
);

COMMENT ON TABLE session_memory_effect_executions IS
    'Immutable destination receipts for session_turn final-memory effects. '
    'The writing row is inserted, all memory mutations run, and the row becomes '
    'done in one vector transaction. Rows are not time-pruned: a delayed app-DB '
    'receipt replay must never become a new vector mutation.';

COMMENT ON COLUMN session_memory_effect_executions.producer_id IS
    'turn_execution_id minted by the fenced app-DB final-persist transaction.';

COMMENT ON COLUMN session_memory_effect_executions.memory_scope_id IS
    'Immutable thread or project destination captured with the accepted turn.';
