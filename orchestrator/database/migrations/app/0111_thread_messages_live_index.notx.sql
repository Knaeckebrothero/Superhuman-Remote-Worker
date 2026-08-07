-- migration:     0111_thread_messages_live_index.notx.sql
-- description:   Partial index matching the live-reader shape introduced in
--                0110: every conversation read now filters
--                rewound_at IS NULL, and rewound rows are expected to stay a
--                small minority of the table.
-- depends-on:    0110_session_rewind_foundations.sql
-- expected:      Minutes on a large thread_messages; CONCURRENTLY, no lock.
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO (.notx — CREATE INDEX CONCURRENTLY cannot run in a txn)

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_thread_messages_thread_seq_live
    ON thread_messages (thread_id, seq)
    WHERE rewound_at IS NULL;
