-- LOAD PGVECTOR FIRST. `SET "hnsw.iterative_scan"` below is a function-level SET
-- clause, and PostgreSQL validates those through validate_option_array_item():
-- when the parameter is UNRECOGNISED (a placeholder custom GUC) that check
-- requires SUPERUSER and fails with `permission denied to set parameter`.
-- pgvector defines hnsw.* in its _PG_init(), which only runs once the library is
-- loaded into the session -- so in a fresh migration session the GUC is still a
-- placeholder and CREATE FUNCTION is denied for the non-superuser app role.
-- `LOAD 'vector'` cannot be used (non-superusers are refused access to the
-- library by name); touching any pgvector-typed value loads it just as well.
-- 0017 and its predecessors escaped this only because they were applied while
-- the app role was still superuser, before the CloudNativePG migration --
-- already-applied migrations are grandfathered, NEW ones are not. Any future
-- migration that creates a function with an hnsw.* SET clause needs this too.
DO $load_pgvector$ BEGIN PERFORM '[1]'::vector; END $load_pgvector$;

