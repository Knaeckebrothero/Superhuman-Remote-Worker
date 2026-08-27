-- migration:     0122_thread_cloud_sync_generations.sql
-- description:   Per-mount durable cloud-sync generations for stateless
--                session handoff (docs/features/stateless_agents.md §5.3.5).
--                A queue-fenced owner advances required_generation before
--                starting push(N). The cloud resource receives a separate
--                commit marker after the bytes land; the next claimant must
--                reconcile that marker before pull(N+1).
-- depends-on:    0121_thread_control_validate_constraints.sql
-- expected:      < 1s. New empty table and indexes only.
-- locks:         AccessExclusiveLock on the new table only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE thread_cloud_sync_generations (
    thread_id              UUID        NOT NULL
        REFERENCES threads(id) ON DELETE CASCADE,
    mount_id               TEXT        NOT NULL,
    required_generation    BIGINT      NOT NULL DEFAULT 0,
    acknowledged_generation BIGINT     NOT NULL DEFAULT 0,
    required_lease_token   BIGINT      NOT NULL DEFAULT 0,
    workspace_generation   TEXT        NOT NULL,
    sync_scope_sha256      CHAR(64)    NOT NULL,
    required_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at        TIMESTAMPTZ,

    PRIMARY KEY (thread_id, mount_id),
    CONSTRAINT thread_cloud_sync_mount_id_nonempty
        CHECK (mount_id <> ''),
    CONSTRAINT thread_cloud_sync_workspace_generation_nonempty
        CHECK (workspace_generation <> ''),
    CONSTRAINT thread_cloud_sync_scope_digest_shape
        CHECK (sync_scope_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT thread_cloud_sync_generation_shape
        CHECK (
            required_generation >= 0
            AND acknowledged_generation >= 0
            AND acknowledged_generation <= required_generation
            AND required_lease_token >= 0
            AND (required_generation = 0 OR required_lease_token > 0)
        )
);

CREATE INDEX idx_thread_cloud_sync_pending
    ON thread_cloud_sync_generations (thread_id, required_generation)
    WHERE acknowledged_generation < required_generation;

COMMENT ON TABLE thread_cloud_sync_generations IS
    'Queue-fenced required generation per thread cloud mount. Before a '
    'stateless owner starts push(N), it increments required_generation in '
    'the same statement that proves its live run_queue lease. The remote '
    'cloud marker is the resource-side commit acknowledgement; the DB '
    'acknowledgement is an observable mirror, not a substitute for reading '
    'that marker on the next claim.';

COMMENT ON COLUMN thread_cloud_sync_generations.mount_id IS
    'Stable cloud_sync mount identity from the claim bundle; legacy session '
    'folders use the reserved value legacy-session.';

COMMENT ON COLUMN thread_cloud_sync_generations.required_generation IS
    'Highest push generation a successor must observe committed on the cloud '
    'resource before it may pull. Monotonic for the lifetime of the thread.';

COMMENT ON COLUMN thread_cloud_sync_generations.acknowledged_generation IS
    'Highest resource marker verified or written by a live lease owner. This '
    'may lag after marker-write/DB-ack crash; successors reconcile it from '
    'the resource. It never authorizes pull by itself.';

COMMENT ON COLUMN thread_cloud_sync_generations.required_lease_token IS
    'run_queue lease token that reserved required_generation. Diagnostic and '
    'recovery evidence; current ownership is always re-proved against '
    'run_queue rather than inferred from this value.';

COMMENT ON COLUMN thread_cloud_sync_generations.workspace_generation IS
    'Authoritative orchestrator workspace-binding generation whose durable '
    'workspace bytes are being pushed. A pending row may not be recovered '
    'against a different runtime incarnation.';

COMMENT ON COLUMN thread_cloud_sync_generations.sync_scope_sha256 IS
    'SHA-256 over the non-secret cloud destination descriptor (thread, mount '
    'identity/path/backend/WebDAV URL) plus workspace generation. It binds '
    'the counter to one exact source/destination pair without persisting '
    'credentials.';

COMMIT;
