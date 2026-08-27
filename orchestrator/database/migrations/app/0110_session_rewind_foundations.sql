-- migration:     0110_session_rewind_foundations.sql
-- description:   Session rewind (docs/features/session_rewind.md, decided
--                2026-08-07): tombstone marker on thread_messages, the
--                thread_rewinds audit ledger, and the thread_turn_commits
--                turn→workspace-commit map. Nothing is ever deleted: a rewind
--                stamps rewound_at on the abandoned tail and live readers
--                filter on rewound_at IS NULL.
-- depends-on:    0102_storage_asset_foundations.sql
-- expected:      < 5s. ADD COLUMN is nullable/no-default (catalog-only);
--                both CREATE TABLEs are new.
-- locks:         Brief ACCESS EXCLUSIVE on thread_messages for the ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE thread_messages
    ADD COLUMN rewound_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN thread_messages.rewound_at IS
    'Set when a session rewind supersedes this row (seq >= the rewind''s '
    'from_seq). Live conversation readers filter rewound_at IS NULL; the row '
    'itself is never deleted. See docs/features/session_rewind.md.';

CREATE TABLE thread_rewinds (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id          UUID NOT NULL,
    from_seq           BIGINT NOT NULL,
    mode               TEXT NOT NULL CHECK (mode IN ('both', 'conversation', 'code')),
    actor              TEXT,
    swept_count        INTEGER NOT NULL DEFAULT 0,
    abandoned_sha      TEXT,
    restored_to_sha    TEXT,
    restore_commit_sha TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE thread_rewinds IS
    'One row per session rewind: the audit trail, un-tombstone metadata, and '
    'the workspace SHAs of the forward-restore. Append-only.';

CREATE INDEX idx_thread_rewinds_thread
    ON thread_rewinds (thread_id, created_at DESC);

CREATE TABLE thread_turn_commits (
    thread_id  UUID NOT NULL,
    seq        BIGINT NOT NULL,
    commit_sha TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, seq)
);

COMMENT ON TABLE thread_turn_commits IS
    'Workspace state after transcript seq <= N: written right after each '
    'per-turn auto-commit / compaction checkpoint commit succeeds. The '
    'restore target for a rewind to seq S is the row with the largest '
    'seq < S. seq 0 = the pre-first-message workspace.';

COMMIT;
