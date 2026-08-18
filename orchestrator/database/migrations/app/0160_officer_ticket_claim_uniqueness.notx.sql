-- migration:     0160_officer_ticket_claim_uniqueness.notx.sql
-- description:   Fail-closed backstop against double-claiming a backlog ticket.
--                The officer's auto-pull tick stamps context.ticket_note_id on
--                the job it spawns for a ticket. Claim check + capacity count +
--                INSERT now share one advisory-locked transaction, but the lock
--                is per-officer-thread and leadership is an optimization rather
--                than a guarantee (dual-leader windows are real and documented
--                in-tree), so correctness cannot rest on it alone. This index
--                makes a racing second claim fail its INSERT instead of putting
--                two jobs on one ticket.
--
--                Scoped to (project_id, ticket) and to NON-TERMINAL jobs, both
--                deliberately:
--                  * project_id, because note ids are slugs unique only within
--                    a project — two projects may each hold a
--                    `feature-dark-mode`, and a global index would let one
--                    project's claim block the other's.
--                  * non-terminal, because one-shot claims are enforced by
--                    comparing ready_at against the newest claim's created_at,
--                    NOT by this index. A ticket the officer reviews and
--                    re-readies legitimately gets a second job; what must never
--                    exist is two claims IN FLIGHT at once.
--                docs/features/officer_backlog_pools.md §5.3 (B3).
-- depends-on:    0159_job_message_routes.sql
-- expected:      seconds; CONCURRENTLY build over the jobs table, and the
--                partial predicate keeps the index to claim-bearing rows only
--                (zero at deploy time — nothing stamps the key yet).
-- locks:         no ACCESS EXCLUSIVE — CREATE INDEX CONCURRENTLY takes
--                SHARE UPDATE EXCLUSIVE and does not block reads or writes.
-- transactional: NO (CREATE INDEX CONCURRENTLY cannot run inside one). ONE
--                statement only, deliberately: a multi-statement file is sent
--                as a simple query, which Postgres wraps in an implicit
--                transaction block, and CONCURRENTLY refuses to run there. The
--                COMMENT ON INDEX that would normally document this lives in
--                the header above instead of costing a second migration.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_jobs_active_ticket_claim
    ON jobs (project_id, ((context ->> 'ticket_note_id')))
 WHERE context ? 'ticket_note_id'
   AND status NOT IN ('completed', 'failed', 'cancelled');
