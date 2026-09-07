-- migration:     0078_project_knowledge_repo.sql
-- description:   Admit role='knowledge' in project_repositories and enforce at
--                most one knowledge repo per project. Step 1 of
--                docs/features/knowledge_base_repo_separation.md §4: the KB
--                vault moves out of the agent's workspace checkout into a
--                dedicated Gitea repo, so a project needs somewhere to name
--                that repo.
--
--                Vocabulary widening only — no existing row changes role, and
--                nothing yet writes 'knowledge'. Resolution (§5) prefers a
--                role='knowledge' repo and falls back to role='jobs', so every
--                project that predates this migration keeps resolving to the
--                same repo, same path, same reindexer behaviour as today. No
--                backfill, no note movement, no dual-read.
-- depends-on:    0077_message_log_session_thread_ids.sql
-- expected:      < 1s. project_repositories is a small control-plane catalog
--                (a handful of rows per project); both the constraint
--                revalidation and the partial index build are trivial scans.
-- locks:         Brief AccessExclusiveLock on project_repositories for the
--                constraint swap, ShareLock for the index build.
-- transactional: yes. The CHECK swap MUST be atomic: DROP and ADD in one
--                transaction leaves no window in which an unconstrained role
--                could be written. CONCURRENTLY would force .notx.sql, and
--                .notx files must be a single statement (see .squawk.toml) —
--                it would split one logical change across two files to buy
--                nothing on a table this small.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Postgres cannot alter a CHECK in place, so drop and re-add. `valid_repo_role`
-- is the name minted by 0001_initial.sql:453 and is what the live schema still
-- carries (orchestrator/database/schema_current.sql). Re-adding under the same
-- name keeps the vocabulary discoverable where every reader already looks.
-- This is a relaxation: every existing row ('jobs' | 'source' | 'reference')
-- satisfies the wider predicate, so VALIDATE cannot fail on live data.
ALTER TABLE project_repositories DROP CONSTRAINT IF EXISTS valid_repo_role;
ALTER TABLE project_repositories
    ADD CONSTRAINT valid_repo_role
    CHECK (role IN ('jobs', 'source', 'reference', 'knowledge'))
    NOT VALID;
-- Squawk's usual advice — validate in a *separate* transaction — cannot apply
-- to a constraint swap: the DROP and the ADD have to be atomic or there is a
-- window in which `role` is unconstrained entirely. We are already holding the
-- AccessExclusiveLock the DROP took, so the split would buy no lock relief,
-- and the scan it guards against is a small control-plane catalog.
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE project_repositories VALIDATE CONSTRAINT valid_repo_role;

-- One knowledge repo per project, mirroring uq_project_jobs_repo
-- (0001_initial.sql:460). Resolution returns *the* knowledge repo, and this
-- index is what makes that well-defined instead of order-dependent.
-- Small control-plane catalog, so a normal transactional index is preferable
-- to a separate non-transactional migration here (0069 idiom).
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_knowledge_repo
    ON project_repositories (project_id) WHERE role = 'knowledge';

COMMENT ON COLUMN project_repositories.role IS
    'jobs = the per-project job/output repo (one per project); source = a '
    'working code repo; reference = read-only context; knowledge = the '
    'dedicated KB vault repo (one per project). Projects without a knowledge '
    'repo fall back to the jobs repo for KB resolution.';

COMMIT;
