-- migration:     0133_thread_session_durable_state.sql
-- description:   Durable per-thread state that must survive a stateless
--                persistent-session claim handoff: the interactive task list,
--                memory-extraction cursor, and cloud citation anchors.
-- depends-on:    0132_jobs_verification_uniq.notx.sql
-- expected:      < 1s. Three new empty tables and their primary keys only.
-- locks:         AccessExclusiveLock on the new tables only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE thread_session_tasks (
    thread_id   UUID        NOT NULL
        REFERENCES threads(id) ON DELETE CASCADE,
    task_number INTEGER     NOT NULL,
    description TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',
    priority     TEXT        NOT NULL DEFAULT 'medium',
    notes        TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (thread_id, task_number),
    CONSTRAINT thread_session_tasks_number_positive
        CHECK (task_number > 0),
    CONSTRAINT thread_session_tasks_description_nonempty
        CHECK (btrim(description) <> ''),
    CONSTRAINT thread_session_tasks_status_value
        CHECK (status IN ('pending', 'in_progress', 'completed')),
    CONSTRAINT thread_session_tasks_priority_value
        CHECK (priority IN ('high', 'medium', 'low')),
    CONSTRAINT thread_session_tasks_completion_shape
        CHECK (
            (status = 'completed' AND completed_at IS NOT NULL)
            OR (status <> 'completed' AND completed_at IS NULL)
        )
);

COMMENT ON TABLE thread_session_tasks IS
    'Authoritative persistent-session checklist keyed by thread. The runtime '
    'hydrates it on every attach; task_number is rendered as task_<N> and is '
    'allocated while the parent thread row is locked.';

CREATE TABLE thread_session_runtime_state (
    thread_id              UUID        PRIMARY KEY
        REFERENCES threads(id) ON DELETE CASCADE,
    memory_extraction_turn INTEGER     NOT NULL DEFAULT 0,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT thread_session_memory_cursor_nonnegative
        CHECK (memory_extraction_turn >= 0)
);

COMMENT ON TABLE thread_session_runtime_state IS
    'Small durable cursors for persistent-session work that is otherwise '
    'process-local. memory_extraction_turn prevents a successor claim from '
    'repeating interval extraction already claimed by its predecessor.';

CREATE TABLE thread_cloud_citation_anchors (
    thread_id      UUID        NOT NULL
        REFERENCES threads(id) ON DELETE CASCADE,
    workspace_path TEXT        NOT NULL,
    anchor          JSONB       NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (thread_id, workspace_path),
    CONSTRAINT thread_cloud_anchor_path_nonempty
        CHECK (btrim(workspace_path) <> ''),
    CONSTRAINT thread_cloud_anchor_object
        CHECK (jsonb_typeof(anchor) = 'object')
);

COMMENT ON TABLE thread_cloud_citation_anchors IS
    'Latest cloud provenance/version anchor per logical workspace path. A '
    'WebDAV read commits this metadata before returning; later claimants '
    'hydrate it before citation registration.';

COMMIT;
