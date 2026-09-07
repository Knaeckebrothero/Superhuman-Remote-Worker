-- Drop the instruction-builder tables.
--
-- The instruction builder was removed in the builder -> sessions consolidation
-- (docs/features/builder_to_sessions_consolidation.md). Its frontend (PR 2),
-- backend endpoints + AI loop (PR 3), and model surface (PR 4) are already gone;
-- these two tables are the last vestige (negligible volume — dev had 1 session /
-- 2 messages of leftover test data, no pilot data).
--
-- builder_messages.session_id references builder_sessions(id) ON DELETE CASCADE,
-- so drop the child table first. Per-table indexes and the updated_at trigger
-- drop automatically with their tables.

DROP TABLE IF EXISTS builder_messages;
DROP TABLE IF EXISTS builder_sessions;
