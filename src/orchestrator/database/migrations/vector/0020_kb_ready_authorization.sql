-- migration:     0020_kb_ready_authorization.sql
-- description:   Ready-authorization timestamp for backlog tickets. The officer
--                stamps a ticket `ready` to authorize dispatch; the auto-pull
--                tick claims it by creating a job that carries the ticket's id.
--                Under one-shot claims a ticket is claimed iff ANY job — in any
--                status, terminal included — carries that stamp with a
--                created_at NEWER than this column. Dispatch consumes
--                readiness; re-arming is an explicit officer act that moves
--                ready_at forward, so a completed or failed ticket parks until
--                someone looks at it instead of being re-dispatched forever.
--                The `ready` TAG alone cannot express that: it has no time, so
--                the tick could not tell "authorized, never dispatched" from
--                "dispatched, awaiting review".
--                docs/features/officer_backlog_pools.md §5.3 (B2).
-- depends-on:    0013_kb_backlog_ticket_types.sql
-- expected:      < 1s; ALTER TABLE ... ADD COLUMN with a NULL default takes no
--                table rewrite on PG11+.
-- locks:         brief ACCESS EXCLUSIVE on knowledge_index for the catalog
--                update; no rows read or rewritten.
-- transactional: YES.

ALTER TABLE knowledge_index
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ;

COMMENT ON COLUMN knowledge_index.ready_at IS
    'When this ticket was last authorized for dispatch (officer stamped `ready`). NULL means unauthorized: a ticket carrying the `ready` tag with no ready_at fails CLOSED and is not dispatchable, which is the deliberate outcome after a vault rebuild that lost the value. Compared against the newest claiming job''s created_at to implement one-shot claims.';

-- No index. The tick reaches these rows through idx_knowledge_backlog (the
-- partial per-project active-ticket index) narrowed by a `tags @>` containment
-- lookup on idx_knowledge_tags; ready_at is only ever read from rows that
-- filter already selected, never used as a leading predicate.
