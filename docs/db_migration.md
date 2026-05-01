# Database Schema Migration in a Live System

## The Problem

Every production database schema eventually needs a breaking change. Adding a nullable column is easy — the old code doesn't know about it and doesn't care. But renaming a column, changing a type, splitting a table, or tightening a constraint will break the running system the moment the migration executes, because the old code is still issuing queries against the old shape.

This is especially sharp in Kubernetes rolling deployments where old pods and new pods overlap. Even with a single-replica orchestrator, there's a window during rollout where:

1. New orchestrator starts, runs `ensure_schema()`, applies the migration
2. Old orchestrator is still running (hasn't received SIGTERM yet), issuing queries against the now-changed schema
3. Agent pods (2 replicas, rolled independently) may be running old code that expects the old schema

The overlap window is brief for the orchestrator (seconds) but can be minutes for agents, and there's no guarantee of deployment ordering between services.

According to Gartner, 83% of data migration projects fail outright or exceed budgets. The recurring causes: untested backups, missing lock timeouts, no environment isolation, and rolling deployments creating race conditions between old code and new schema.

## Current Approach

Our `ensure_schema()` method (`orchestrator/database/postgres.py`) re-applies the full `schema.sql` on every orchestrator startup. All DDL is idempotent:

- `CREATE TABLE IF NOT EXISTS` for tables
- `CREATE INDEX IF NOT EXISTS` for indexes
- `DO $$ BEGIN ... EXCEPTION WHEN duplicate_column THEN null; END $$` for adding columns
- `DO $$ BEGIN ... EXCEPTION WHEN undefined_object THEN null; END $$` for constraint changes

This works for **additive changes**: new tables, new columns (with defaults), new indexes, new enum values in CHECK constraints. The old code simply ignores what it doesn't know about.

## What Breaks

Changes that conflict with running code or existing data:

| Change | Why it breaks |
|--------|--------------|
| Rename column | Old code queries `SELECT old_name` → column not found |
| Change column type | Old code sends wrong type → cast error or silent truncation |
| Drop column | Old code queries `SELECT *` or names the column → error |
| Add `NOT NULL` without default | Existing rows violate constraint → migration fails |
| Tighten CHECK constraint | Existing rows may violate → migration fails |
| Split table (normalize) | Old code queries the original table shape → missing columns |
| Change PK/FK relationships | Cascading effects on INSERT/UPDATE from old code |

## The Expand-Contract Pattern

The standard solution is **expand-contract** (also called "parallel change," coined by Martin Fowler). Every breaking change is decomposed into a sequence of non-breaking steps, deployed across multiple releases:

```
Release 1 (expand):   Add new structure alongside old, dual-write, backfill
Release 2 (migrate):  New code reads from new structure exclusively
Release 3 (contract): Remove old structure once all code is updated
```

Each release is independently deployable and rollback-safe. At no point does running code face a schema it doesn't understand.

Tools like **pgroll** (Xata) and **Reshape** automate this pattern at the database level — they use versioned views and database triggers to keep old and new structures in sync, removing the need for application-level dual-write code. Both are PostgreSQL-specific.

### Example: Rename column `config_name` → `agent_config`

**Wrong approach** (single release):
```sql
ALTER TABLE jobs RENAME COLUMN config_name TO agent_config;
-- Instant breakage: every query referencing config_name fails
```

**Expand-contract approach**:

**Release 1 — Expand**: Add new column, dual-write
```sql
-- schema.sql migration block
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN agent_config VARCHAR(100);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Backfill existing data
UPDATE jobs SET agent_config = config_name WHERE agent_config IS NULL;

-- Trigger to keep columns in sync during dual-write window
CREATE OR REPLACE FUNCTION sync_config_name() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.agent_config IS NULL THEN
        NEW.agent_config := NEW.config_name;
    END IF;
    IF NEW.config_name IS NULL THEN
        NEW.config_name := NEW.agent_config;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_config_name ON jobs;
CREATE TRIGGER trg_sync_config_name
    BEFORE INSERT OR UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION sync_config_name();
```

Code change: Update all queries to write both columns, read from `agent_config`.

**Release 2 — Contract**: Once all pods are on Release 1+ code:
```sql
-- Remove sync trigger
DROP TRIGGER IF EXISTS trg_sync_config_name ON jobs;
DROP FUNCTION IF EXISTS sync_config_name();

-- Drop old column
ALTER TABLE jobs DROP COLUMN IF EXISTS config_name;
```

### Example: Change column type (`VARCHAR(50)` → `INTEGER` enum)

**Release 1 — Expand**:
```sql
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN status_code INTEGER;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Backfill
UPDATE jobs SET status_code = CASE status
    WHEN 'created' THEN 1
    WHEN 'processing' THEN 2
    WHEN 'completed' THEN 3
    -- ...
END WHERE status_code IS NULL;
```

Code: Write both `status` and `status_code`. Read from `status_code` where available, fall back to `status`.

**Release 2 — Contract**: Drop `status` column, rename `status_code` to `status`, update CHECK constraint.

### Example: Split table (normalize `jobs.datasources` JSONB → separate table)

**Release 1 — Expand**: Create new `job_datasources` table. Code writes to both JSONB and new table. Reads from new table with JSONB fallback.

**Release 2 — Contract**: Drop the JSONB column. All code reads from the new table.

## When to Use What

| Change type | Strategy | Releases needed |
|-------------|----------|----------------|
| Add nullable column | Direct `ALTER ADD` | 1 |
| Add column with default | Direct `ALTER ADD DEFAULT` | 1 |
| Add CHECK enum value | Direct constraint replacement | 1 |
| Add table/index | Direct `CREATE IF NOT EXISTS` | 1 |
| Rename column | Expand-contract with sync trigger | 2 |
| Change column type | Expand-contract with dual column | 2 |
| Drop column | Verify no code references, then `DROP IF EXISTS` | 1-2 |
| Add `NOT NULL` | Add default first, backfill, then add constraint | 2 |
| Split/merge tables | Expand-contract with dual writes | 2-3 |

## PostgreSQL Lock Safety

PostgreSQL DDL acquires locks that can block production queries. The critical danger is the **lock queue**: if your DDL is waiting for an ACCESS EXCLUSIVE lock, every subsequent query queues behind it, causing connection pool exhaustion within seconds.

### Lock types that matter

| Lock Level | Acquired By | Blocks Reads? | Blocks Writes? |
|---|---|---|---|
| ACCESS SHARE | `SELECT` | No | No |
| ROW EXCLUSIVE | `INSERT`/`UPDATE`/`DELETE` | No | No |
| SHARE UPDATE EXCLUSIVE | `CREATE INDEX CONCURRENTLY`, `VACUUM` | No | No |
| SHARE | `CREATE INDEX` (non-concurrent) | No | **Yes** |
| ACCESS EXCLUSIVE | Most `ALTER TABLE`, `DROP`, `TRUNCATE` | **Yes** | **Yes** |

### Safe DDL patterns

```sql
-- ALWAYS set lock_timeout before DDL to prevent queue pileup
SET lock_timeout TO '2s';

-- Add columns (safe since PG 11 for constant defaults — no table rewrite)
ALTER TABLE users ADD COLUMN status text DEFAULT 'active';

-- Create indexes without blocking writes
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);

-- Add foreign keys without blocking (two-phase: add NOT VALID, then validate)
ALTER TABLE orders ADD CONSTRAINT orders_user_fk
  FOREIGN KEY (user_id) REFERENCES users(id) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_user_fk;

-- Add NOT NULL safely (constraint-first, then promote)
ALTER TABLE users ADD CONSTRAINT users_email_nn
  CHECK (email IS NOT NULL) NOT VALID;
ALTER TABLE users VALIDATE CONSTRAINT users_email_nn;
ALTER TABLE users ALTER COLUMN email SET NOT NULL;
ALTER TABLE users DROP CONSTRAINT users_email_nn;
```

### Dangerous operations

- **`ALTER COLUMN TYPE`** — rewrites entire table, holds ACCESS EXCLUSIVE lock. Use the dual-column pattern instead.
- **`VACUUM FULL`** — rewrites table, blocks everything. Use regular `VACUUM` and tune autovacuum.
- **`SET NOT NULL` directly** — scans entire table under ACCESS EXCLUSIVE. Use the CHECK constraint promotion pattern above.
- **Non-concurrent `CREATE INDEX`** — blocks all writes for the entire build time.

### Retry on lock timeout

```sql
DO $$
DECLARE attempt INTEGER := 1;
BEGIN
    WHILE attempt <= 10 LOOP
        BEGIN
            SET lock_timeout = '2s';
            -- your DDL here
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            PERFORM pg_sleep(30);
            attempt := attempt + 1;
        END;
    END LOOP;
END $$;
```

**Note**: `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block. Run it as a standalone statement.

## Backfill Best Practices

When migrating data from old to new columns/tables:

- **Batch operations** in chunks of 1,000-10,000 rows with commits between batches. Never update millions of rows in a single transaction — it holds locks, bloats WAL, and stalls replication.
- **Use `ctid` ranges** for efficient batching without full table scans:
  ```sql
  UPDATE items SET new_col = old_col
  WHERE ctid IN (SELECT ctid FROM items WHERE new_col IS NULL LIMIT 5000);
  ```
- **Rate-limit backfill jobs** to avoid overwhelming the database. Add `pg_sleep(0.1)` between batches or use application-level staggering.
- **Verify counts** post-backfill: `SELECT COUNT(*) FROM items WHERE new_col IS NULL` should be zero.
- For very large tables, consider **offline backfill** from a read replica or database snapshot rather than running against the primary.

## Common Antipatterns

1. **Long-running transactions**: A single transaction updating millions of rows holds locks, bloats WAL, and stalls replication. Batch with commits between chunks.

2. **Mixing DDL and DML in one transaction**: The DDL acquires ACCESS EXCLUSIVE for the entire transaction duration, including the slow DML. Separate them.

3. **Deploying schema + code simultaneously**: Rolling deployments create a window where old code runs against the new schema. Always deploy schema changes separately, ensuring backward compatibility.

4. **No lock timeout**: Running DDL without `SET lock_timeout` means a blocked ALTER TABLE silently queues every subsequent query behind it, causing a full outage.

5. **Manual database changes**: Ad-hoc SQL against production with no version control, no audit trail, no rollback path. All changes should go through version-controlled migration scripts.

6. **Assuming rollback scripts will save you**: `DROP TABLE` and `DROP COLUMN` destroy data that no rollback script can recover. Roll forward to a fix, don't try to undo data loss.

7. **Not testing migrations against production-size data**: A migration that runs in 50ms on a dev database with 100 rows might hold a lock for 10 minutes on a production table with 50 million rows.

## Migration Frameworks

### Do we need one?

Frameworks like Alembic, Flyway, or Atlas provide version tracking, ordering, rollback, and conflict detection. Our current `schema.sql` approach works because:

- We have one schema file, one database, one team
- Changes are infrequent and predominantly additive
- The `DO $$ ... EXCEPTION ... $$` pattern is inherently idempotent
- We don't need rollback — we roll forward (fix and redeploy)

We'd want a migration framework when:
- Multiple developers are making concurrent schema changes
- We need guaranteed ordering (migration B depends on migration A)
- We need downgrade support for compliance or SLA reasons
- The schema file grows unwieldy with accumulated migration blocks

### Options for Python/PostgreSQL

| Tool | Approach | Strengths | Weaknesses |
|------|----------|-----------|------------|
| **Alembic** | Imperative (Python scripts) | Deep SQLAlchemy integration, autogenerate from models, Python-native | Tightly coupled to SQLAlchemy, autogenerate needs manual review |
| **Flyway** | Imperative (numbered SQL files) | Language-agnostic, simple mental model, broad DB support | No rollback in free tier, no safety linting |
| **Atlas** | Declarative (desired state) | Built-in linting, drift detection, migration testing, CI/CD integration | Newer tool, smaller ecosystem |
| **Sqitch** | Imperative (deploy/verify/revert) | Pure SQL, dependency graphs, rigorous verification | Steeper learning curve, 3 scripts per change |
| **pgroll** | Declarative (expand-contract) | Automated dual-write via views+triggers, zero app changes | PostgreSQL-only, newer project |

### CI safety linting

Regardless of framework, add **Squawk** to CI. It's a static analyzer for PostgreSQL migration SQL that catches:
- Non-concurrent index creation
- Missing `NOT VALID` on constraints
- Column drops without verification
- `ALTER COLUMN TYPE` without the dual-column pattern

```yaml
# GitHub Actions
- name: Lint migrations
  uses: sbdchd/squawk-action@v1
```

### Recommendation

We're moving to a versioned `migrations/` directory with a small hand-rolled runner — concrete design in [Proposed System for This Repo](#proposed-system-for-this-repo) below. Continue to follow expand-contract discipline for any breaking change regardless of mechanism. Pair Squawk with the cutover — it's zero-effort and catches the most dangerous mistakes.

## Proposed System for This Repo

We're moving from inline `DO $$` blocks inside `schema.sql` to a versioned migrations directory with a small hand-rolled runner. This section is the authoritative design from cutover onward. It borrows the boring parts (tracking schema, advisory locks, checksum semantics) from how mature tools (Flyway, golang-migrate, Atlas, pgmigrate) handle them, while keeping the implementation small enough to read in one sitting.

### Why hand-rolled (not Alembic)

This codebase uses raw `asyncpg` — there's no SQLAlchemy model layer for Alembic to introspect. Alembic's main value is autogenerate-from-models and revision-graph branching; with pure SQL we'd be using ~5% of the tool. A ~80-line Python runner over numbered SQL files matches the existing style, adds zero new dependencies, and stays readable. If we ever adopt SQLAlchemy ORM we revisit.

Sqitch is the other defensible pure-SQL choice (deploy/verify/revert with explicit dependencies), but its 3-script-per-change model and Perl runtime are heavier than what we need.

### Directory layout

```
orchestrator/database/
├── postgres.py
├── schema.sql               # frozen at cutover; kept as a readable reference
├── vector_schema.sql        # same, for the pgvector DB
├── migrations/
│   ├── app/
│   │   ├── 0001_initial.sql
│   │   ├── 0002_capabilities_array.sql
│   │   ├── 0003_jobs_priority_idx.notx.sql
│   │   └── ...
│   └── vector/
│       ├── 0001_initial.sql
│       └── ...
└── migrate.py               # the runner
```

After cutover, `schema.sql` and `vector_schema.sql` stop being edited. They stay in the repo as a snapshot of state at cutover — useful for onboarding and as a sanity reference. They are no longer applied at runtime.

### File naming

Pattern: `NNNN_short_snake_case_description.sql`, with the optional `.notx.sql` suffix for non-transactional migrations (see the Non-transactional section below).

```
0001_initial.sql
0002_capabilities_array.sql
0003_jobs_priority_idx.notx.sql
```

- **4-digit zero-padded sequence**. Matches Sentry (1079+ migrations) and PostHog (1000+) at production scale; sufficient at any pace we'll hit.
- **Single underscore** between number and description (matches golang-migrate, Atlas, Django). Reserve double underscore for Flyway.
- **Lowercase snake_case description**, ~50 chars max, verb-first (`add_`, `drop_`, `backfill_`, `rename_`).
- **No `.up.sql` / `.down.sql` split.** Forward-only — see "Rollback philosophy" below.

**Conflict resolution.** If two branches both land at the same number, the second to merge renumbers. CI enforces uniqueness with one liner per migrations subdirectory:

```bash
ls "$dir"/*.sql | xargs -n1 basename | awk -F_ '{print $1}' | sort | uniq -d | grep -q . && exit 1 || exit 0
```

**Insertion policy.** Always append. If you "need" to insert between `0007` and `0008`, write `0042_fix_thing_from_0007.sql` instead. Inserting between applied migrations breaks every downstream environment.

**Renaming policy.** Never rename a migration after it has been applied to any environment. The runner checksums file content and refuses checksum drift on applied rows. If a name is wrong, write a new migration that supersedes it.

### Why sequential, not timestamps

Surveyed projects split roughly:

- **Sequential 4–6 digit**: Sentry, PostHog, Mattermost — most consistent for one-team-one-branch workflows.
- **Timestamp 14-digit**: Discourse, GitLab, Plausible, Supabase — wins when ≥3 devs branch in parallel.
- **Unix-ms timestamp**: n8n — same idea, uglier.

For one developer on a linear `develop` branch, timestamps are pure noise. `0042_capabilities.sql` is easier to scan than `20260501123045_capabilities.sql`, and the collision risk that timestamps solve doesn't exist here. Switch to timestamps if/when the team grows past three concurrent contributors.

### Tracking table

Borrowed from Flyway's column set + Atlas's error forensics + golang-migrate's dirty-flag semantics:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename       TEXT         PRIMARY KEY,             -- '0002_capabilities_array.sql'
    checksum       TEXT         NOT NULL,                -- sha256 hex of file bytes
    applied_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    applied_by     TEXT         NOT NULL DEFAULT current_user,
    execution_ms   INTEGER      NOT NULL,
    success        BOOLEAN      NOT NULL DEFAULT TRUE,
    error          TEXT                                  -- nullable; populated on partial failure
);

CREATE INDEX IF NOT EXISTS schema_migrations_dirty_idx
    ON schema_migrations(filename) WHERE success = FALSE;
```

Both DBs (app + vector) get their own `schema_migrations`. The runner creates it on first run via `CREATE TABLE IF NOT EXISTS`.

Why these columns specifically:

- `checksum` (SHA-256 hex over raw bytes): mature tools split on this — Flyway uses CRC32, Atlas uses SHA-256, Alembic/golang-migrate/pgmigrate use nothing. SHA-256 is cheap and catches accidental edits to applied files.
- `success` boolean + `error` text: golang-migrate's "dirty bit" idea, written *on* the row instead of a separate sentinel. The partial index makes "is there a dirty migration?" a free query.
- `applied_by` / `applied_at` / `execution_ms`: forensics. When a migration runs slow on prod, we want the timing.

### Concurrency

The runner takes a Postgres advisory lock to serialize concurrent runners (e.g. two replicas booting at once during a rolling update):

```python
LOCK_ID = 0x5352575F4D4947  # "SRW_MIG" packed into int64
await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)
```

`pg_advisory_xact_lock` releases automatically when the transaction ends. Single-replica today; the lock is cheap insurance against a future scale-up. golang-migrate uses the same primitive; Flyway's `PostgreSQLAdvisoryLockTemplate` uses the same family.

### The runner

`orchestrator/database/migrate.py`, ~80 lines. Invoked from the orchestrator's FastAPI `lifespan`, replacing the current `await postgres_db.ensure_schema()` call.

```python
import asyncpg, hashlib, logging, time
from pathlib import Path

LOCK_ID = 0x5352575F4D4947  # "SRW_MIG"
log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename       TEXT PRIMARY KEY,
    checksum       TEXT NOT NULL,
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by     TEXT NOT NULL DEFAULT current_user,
    execution_ms   INTEGER NOT NULL,
    success        BOOLEAN NOT NULL DEFAULT TRUE,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS schema_migrations_dirty_idx
    ON schema_migrations(filename) WHERE success = FALSE;
"""

async def run_migrations(
    pool: asyncpg.Pool,
    migrations_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise RuntimeError(f"no migrations in {migrations_dir}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(DDL)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)

            # Refuse to proceed if a previous run left a dirty row.
            dirty = await conn.fetchrow(
                "SELECT filename, error FROM schema_migrations "
                "WHERE success = FALSE LIMIT 1")
            if dirty:
                raise RuntimeError(
                    f"dirty migration {dirty['filename']}: {dirty['error']!s}; "
                    f"manual repair required (DELETE the row after fixing)")

            applied = {
                r["filename"]: r["checksum"]
                for r in await conn.fetch(
                    "SELECT filename, checksum FROM schema_migrations "
                    "WHERE success = TRUE ORDER BY filename")
            }

            # Refuse silent edits to applied files.
            for path in files:
                if path.name in applied:
                    if applied[path.name] != _checksum(path.read_text()):
                        raise RuntimeError(
                            f"checksum changed: {path.name} "
                            f"(applied migrations are immutable)")

            # Refuse missing files (someone deleted an applied migration).
            stray = set(applied) - {p.name for p in files}
            if stray:
                raise RuntimeError(f"applied but missing on disk: {sorted(stray)}")

            pending = [p for p in files if p.name not in applied]
            if not pending:
                log.info("schema up to date (%d applied)", len(applied))
                return

            log.info("applying %d migration(s)", len(pending))
            for path in pending:
                sql = path.read_text()
                log.info("→ %s", path.name)
                t0 = time.monotonic()
                try:
                    await conn.execute(sql)
                except Exception as exc:
                    ms = int((time.monotonic() - t0) * 1000)
                    # Record failure on a fresh connection (current txn rolls back).
                    async with pool.acquire() as failconn:
                        await failconn.execute(
                            "INSERT INTO schema_migrations(filename, checksum, "
                            "execution_ms, success, error) "
                            "VALUES($1,$2,$3,FALSE,$4)",
                            path.name, _checksum(sql), ms, str(exc)[:8000])
                    raise
                ms = int((time.monotonic() - t0) * 1000)
                await conn.execute(
                    "INSERT INTO schema_migrations(filename, checksum, execution_ms) "
                    "VALUES($1,$2,$3)", path.name, _checksum(sql), ms)
                log.info("✓ %s (%d ms)", path.name, ms)

            if dry_run:
                raise _DryRunRollback()


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _DryRunRollback(Exception):
    """Marker — caller catches this to confirm a dry-run succeeded."""
```

What it handles:

| Concern | Mechanism |
|---------|-----------|
| Two replicas migrating at once | `pg_advisory_xact_lock` on a shared int64 ID |
| Modified-after-applied migration | SHA-256 checksum compared on every run, hard fail |
| Deleted applied migration on disk | Stray-file detection, hard fail |
| Mid-migration failure | Outer txn rolls back; failure row written on a fresh connection; subsequent runs refuse via dirty-row check |
| Stuck-migration debuggability | Filename logged before exec; `execution_ms` recorded |
| Dry-run | Forced rollback after success — proves the chain works without committing |

What it deliberately skips:

- **Down migrations.** Reversing destructive DDL (`DROP COLUMN`) is fiction. Roll forward for fixes; PITR for actual data recovery.
- **Branching / DAGs.** Lexicographic ordering on `NNNN_*.sql` is enough; we're not Alembic.
- **Module namespacing** (per-library `module_name` columns, à la pgdbm). Single project, single tenant.
- **Audit log table** (e.g. Migretti's `_migrations_log` for every action). The main table records who/when/how-long; that's sufficient.
- **Out-of-order tolerance.** Strict-sequential simplifies the mental model.
- **Non-transactional support in v1.** Add it the first time we actually need `CREATE INDEX CONCURRENTLY` (see below).

### Migration file template

Default (transactional):

```sql
-- migration:     0042_add_priority_to_jobs.sql
-- description:   Add priority column to jobs for dispatcher ordering.
-- depends-on:    0041_create_jobs_table.sql
-- expected:      < 5s on prod (~3.2M rows; PG 11+ does in-place ADD COLUMN with constant default)
-- locks:         AccessExclusiveLock on jobs (brief, retried)
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

DO $$
DECLARE
    max_attempts CONSTANT int := 30;
    cap_ms       CONSTANT bigint := 60000;
    base_ms      CONSTANT bigint := 10;
    delay_ms              bigint;
    done                  boolean := false;
BEGIN
    FOR i IN 1..max_attempts LOOP
        BEGIN
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority smallint NOT NULL DEFAULT 0;
            done := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            delay_ms := round(random() * least(cap_ms, base_ms * 2 ^ i));
            PERFORM pg_sleep(delay_ms::numeric / 1000);
        END;
    END LOOP;
    IF NOT done THEN
        RAISE EXCEPTION 'lock acquisition failed after % attempts', max_attempts;
    END IF;
END $$;

COMMIT;
```

Conventions:

1. **Header block** with `migration / description / depends-on / expected / locks / transactional` fields. Plain comments, structured enough for a future linter to parse.
2. **`BEGIN; ... COMMIT;`** wrapping. Lets `psql -f file.sql` reproduce a runner failure for debugging. The runner *also* wraps in a transaction; the explicit BEGIN/COMMIT is harmless inside that.
3. **`SET LOCAL`** for `lock_timeout` (2s), `statement_timeout` (15min), `idle_in_transaction_session_timeout` (5min). Per-transaction; can't leak. Matches GitLab / Strong Migrations / Doctolib defaults.
4. **Lock retry loop** for any `ALTER TABLE` that takes `AccessExclusiveLock`. Defaults: 30 attempts, exponential backoff capped at 60s, with jitter (PostgresAI canonical pattern).
5. **Idempotent DDL** (`IF EXISTS` / `IF NOT EXISTS`) as defense-in-depth. The tracking table is the primary one-shot guard, but a partial-failure rerun should succeed.
6. **One logical change per file.** Don't mix "add a table" with "drop a column" — easier to review, easier to time, easier to supersede.

### Non-transactional migrations

Some Postgres operations cannot run inside a transaction block ([CREATE INDEX docs](https://www.postgresql.org/docs/16/sql-createindex.html)):

| Statement | Reason |
|---|---|
| `CREATE INDEX CONCURRENTLY` | Multi-phase, requires its own visibility |
| `REINDEX CONCURRENTLY` | Same |
| `VACUUM`, `VACUUM FULL` | Needs to commit between phases |
| `CLUSTER` (without table arg) | Same |
| `ALTER SYSTEM` | Modifies postgresql.auto.conf |
| `CREATE DATABASE`, `DROP DATABASE` | Cluster-level |
| `ALTER TYPE ... ADD VALUE` (when used in same migration) | New value invisible until commit |

These migrations get the **`.notx.sql` suffix**. The runner detects the suffix and does not wrap them in a transaction. The header `transactional: NO` field must match the suffix; CI checks both.

```sql
-- migration:     0043_jobs_priority_idx.notx.sql
-- description:   Index priority for dispatcher ORDER BY.
-- depends-on:    0042_add_priority_to_jobs.sql
-- expected:      ~2 min build, no table lock
-- locks:         ShareUpdateExclusiveLock on jobs (concurrent-safe)
-- transactional: NO

SET lock_timeout      = '2s';
SET statement_timeout = '30min';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_priority_status
    ON jobs (priority DESC, status)
    WHERE status IN ('created', 'paused');

-- If a previous run failed mid-build, the index may exist as INVALID.
-- The runner's pre-flight (when added) drops invalid indexes by name
-- before retry — until then, check `pg_indexes` and drop manually.
```

When a single logical change needs both transactional DDL and a `CREATE INDEX CONCURRENTLY` — **split into two files**. Don't try to embed `COMMIT;` mid-file; that's how invalid indexes get left behind.

### Migration-file anti-patterns

These are file-content issues, distinct from the deploy-strategy antipatterns earlier in this doc. Pulled from Postgres docs and the GitLab / Lyft / Doctolib migration handbooks.

| Anti-pattern | Replacement |
|---|---|
| `ALTER COLUMN TYPE` rewriting the table | If the cast is binary-coercible (e.g. `varchar(50) → varchar(100)`), Postgres skips rewrite — verify with `EXPLAIN`. Otherwise: add new column, dual-write, backfill, swap. |
| `ALTER TABLE ... SET NOT NULL` directly | `ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID;` → backfill → `VALIDATE CONSTRAINT;` → `SET NOT NULL`. PG 12+ uses the validated check, taking AccessExclusiveLock only briefly. |
| `DELETE FROM big_table WHERE ...` in one txn | Batched loop with `LIMIT` + `ctid` ranges; commit per batch (which means the migration file is non-transactional). |
| `CREATE INDEX` non-concurrently on a hot table | `CREATE INDEX CONCURRENTLY` in a separate `.notx.sql` file. |
| Renaming a column in one migration | Expand-contract across releases (see Examples earlier in this doc). |
| `IF NOT EXISTS` masking logic errors | Only use it for idempotency on objects you *just* created in the same file. Never to "fix" diverged environments. |
| `ADD COLUMN ... NOT NULL DEFAULT volatile_expr()` on a big table | Volatile defaults still rewrite. Split: add nullable, backfill, set default, set not null. (Constant defaults are O(1) since PG 11.) |

### Testing & CI

Migration PRs run through a fixed gate before merge.

**1. Static lint via [Squawk](https://github.com/sbdchd/squawk).**

Squawk catches the high-impact lock and breakage rules. Enable as errors:
`require-concurrent-index-creation`, `ban-concurrent-index-creation-in-transaction`, `constraint-missing-not-valid`, `adding-not-nullable-field`, `adding-field-with-default`, `changing-column-type`, `disallowed-unique-constraint`, `adding-foreign-key-constraint`, `require-timeout-settings`, `renaming-column`, `renaming-table`, `ban-drop-column`, `ban-drop-table`.

Disable the stylistic ones (`prefer-bigint-over-int`, `prefer-text-field`) — too noisy.

**2. Dry-run against an ephemeral DB.**

CI spins up a fresh `postgres:16` service container, applies all migrations from scratch, and runs a smoke test (health endpoint plus a representative read). Catches migrations that pass Squawk but fail at runtime — most commonly mismatched constraint names or column references.

**3. Uniqueness check.**

CI fails if two migration files share a `NNNN_` prefix.

```yaml
# .github/workflows/db-migrations.yml
name: db-migrations
on:
  pull_request:
    paths: ['orchestrator/database/migrations/**.sql']

jobs:
  squawk:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: sbdchd/squawk-action@v2
        with:
          pattern: "orchestrator/database/migrations/**/*.sql"
          version: "latest"
          fail_on_violations: true

  dry-run:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: ci
        ports: ['5432:5432']
        options: >-
          --health-cmd="pg_isready" --health-interval=5s
          --health-timeout=3s --health-retries=10
    steps:
      - uses: actions/checkout@v4
      - run: pip install asyncpg
      - run: python -m orchestrator.database.migrate --dry-run
        env:
          DATABASE_URL: postgresql://postgres:ci@localhost:5432/postgres

  uniqueness:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: No duplicate migration prefixes
        run: |
          set -euo pipefail
          for d in orchestrator/database/migrations/*/; do
            if ls "$d"*.sql >/dev/null 2>&1; then
              dupes=$(ls "$d"*.sql | xargs -n1 basename | awk -F_ '{print $1}' | sort | uniq -d)
              if [ -n "$dupes" ]; then echo "duplicate prefix(es) in $d: $dupes"; exit 1; fi
            fi
          done
```

**4. Drift detection (deferred).**

Once stable on the new system, add a nightly job that runs `pg_dump --schema-only` from prod and compares to a checked-in snapshot generated by running all migrations against a fresh DB. Fail on non-empty diff. [Atlas's `atlas schema diff`](https://atlasgo.io/declarative/diff) is the polished tool if hand-rolling becomes painful.

**5. pgTAP assertions (optional, per migration).**

For changes that need post-migration invariants (column NOT NULL, view shape, representative query results), drop a `tests/db/*.sql` file with [pgTAP](https://pgtap.org/) assertions and run after the dry-run job. Don't try to test every row — assert structural invariants only.

### Rollback philosophy

Forward-only, with PITR for actual data-loss incidents. We do not maintain `down.sql` files. Rationale:

- **Reversing destructive DDL is fiction.** A `down.sql` for `DROP COLUMN customer_email` cannot bring back the dropped data. The only way to recover dropped data is point-in-time recovery from WAL.
- **Down migrations are migrations too.** Every down has the same risks (locks, rewrites, app-breakage windows) as its up. Maintaining doubled risk for the rare rollback path is a bad trade.
- **Industry consensus.** GitLab's migration style guide, Atlas's "Hard Truth about GitOps and DB Rollbacks", Stripe's online-migrations post, graphile/migrate — all roll-forward.

When you need to undo: write a new forward migration. When you need to recover lost data: PITR from WAL. Both managed Postgres (RDS/Cloud SQL) and self-hosted setups via [pgBackRest](https://pgbackrest.org/) or WAL-G expose this as routine.

### Transition plan

**Step 1 — Land the runner, no behavior change.**
Add `migrate.py` + empty `migrations/{app,vector}/` directories. `lifespan` calls the runner *after* the existing `ensure_schema()` (additive — no migrations to run, no-op). Deploy. Verify `schema_migrations` appears in both DBs.

**Step 2 — Snapshot current schema as `0001_initial.sql`.**
For each DB, `pg_dump --schema-only --no-owner --no-acl` against a freshly-applied `schema.sql`, checked in as `migrations/{app,vector}/0001_initial.sql`. On every existing environment, mark it applied once:

```sql
INSERT INTO schema_migrations(filename, checksum, execution_ms)
VALUES ('0001_initial.sql', '<sha256 of file>', 0);
```

This is the only manual step. Existing environments skip 0001 forever; fresh installs apply it like any other migration.

**Step 3 — Stop using `schema.sql` at runtime.**
Drop the `ensure_schema()` call from `lifespan`. Only the runner runs. Update `CLAUDE.md`: schema changes go in a new numbered file under `migrations/`. The next change after cutover ships as `0002_*.sql`.

**Step 4 — Verify cutover.**
Test on a clone of prod (`pg_dump | pg_restore` to a scratch DB; run migrations; smoke-test). Run `python init.py --force-reset` to confirm fresh installs work end-to-end through migrations alone.

### Operational runbook

**Adding a migration.**
1. Create `migrations/<db>/NNNN_short_description.sql` with next sequential number; add `.notx.sql` suffix only if needed.
2. Fill in the header block.
3. Run locally against a clone of test/prod (`pg_dump test_db | pg_restore -d local_test_db; psql local_test_db -f migrations/<db>/NNNN_*.sql`).
4. Push. CI runs Squawk + dry-run + uniqueness checks.
5. Merge to develop. Deploy. Runner picks it up on orchestrator startup.

**A migration fails at runtime.**
1. Orchestrator pod CrashLoops. Logs show the migration filename and the error.
2. The dirty row is in `schema_migrations` with `success=false` and the error text.
3. Fix root cause: usually edit the migration file. If the migration is partially applied (non-transactional), undo what got done by hand or write a follow-up migration.
4. `DELETE FROM schema_migrations WHERE filename = '0042_*.sql' AND success = FALSE;` to clear the dirty flag.
5. Redeploy.

**A migration's checksum drifted.**
Runner refuses to start with `checksum changed: 0042_*.sql`. Means someone edited a migration that was already applied somewhere. Either revert the edit, or write a new migration that supersedes the change (and accept that local dev DBs may need manual reconciliation).

### Open decisions (carry-forward)

- **Squawk config** lands alongside step 1 with the rule list above. Iterate on noise.
- **Atlas drift detection** is deferred until the nightly `pg_dump` diff becomes routine.
- **Concurrent migrations on a multi-replica orchestrator** are out of scope today; the advisory lock is in place for when we scale.
- **Non-transactional support in the runner** is deferred until the first migration that genuinely needs `CREATE INDEX CONCURRENTLY`.
- **pgTAP integration** is deferred to "first migration that needs structural assertions"; not blocking cutover.

## Deployment Checklist for Breaking Changes

1. **Classify the change**: Additive (safe, single release) or breaking (needs expand-contract)?
2. **Set `lock_timeout`** in all migration SQL. Never run DDL without it.
3. **Test against a production clone** — `pg_dump | pg_restore` to a test instance, run the migration, verify.
4. **Expand release**: Add new structure, dual-write trigger, backfill. Deploy. Verify all pods are on new code.
5. **Soak period**: Run both structures for at least one full deployment cycle. Monitor for data drift.
6. **Contract release**: Remove old structure. Deploy.
7. **Never combine expand and contract in the same release** — if you need to rollback the contract release, the expand structure must still be there.
8. **Verify backups are working** before any migration. An untested backup is not a backup.

## Real-World Incidents

**GitLab (Jan 2017)**: During a replication recovery, an engineer ran `rm -rf` on the production database directory. `pg_dump` backups had silently failed for months (wrong PG version). Only a 6-hour-old LVM snapshot survived. 18-hour outage, ~5,000 projects permanently lost. Lesson: test your backups, use WAL archiving for point-in-time recovery.

**Resend (Feb 2024)**: A migration command run from a developer laptop pointed to production and dropped all tables. 12-hour outage. Lesson: never run migrations from dev machines — use CI/CD with environment isolation and mandatory dry-run.

**Snowflake**: A backward-incompatible schema update caused a version mismatch across 10 regions, leading to a 13-hour outage. Lesson: every migration must be compatible with at least the `N-1` application version.

**Healthchecks.io (Apr 2025)**: Single-node Postgres segfault during routine workload took the service down. Backups existed but failover did not. Lesson: an HA replica with tested failover is part of the migration safety story, not a separate concern — every migration assumes the DB stays up.

**Clerk (Sept 2025)**: Automatic minor PG upgrade removed an O(n²) lock-manager path that had been an accidental rate limiter; the post-upgrade DB accepted connection storms it couldn't sustain. Lesson: load-test post-upgrade behavior before cutover; pin minor versions on managed DBs that auto-upgrade by default.

**"4-Hour ALTER TABLE" (2026)**: An `ALTER TABLE ADD COLUMN ... CHECK (...)` against an 84M-row table held AccessExclusiveLock for 4h12m — every read and write queued behind it. Lesson: validate CHECK constraints with `NOT VALID` then `VALIDATE CONSTRAINT` separately. This is exactly what Squawk's `constraint-missing-not-valid` rule catches.

## References

- [Stripe: Online Migrations at Scale](https://stripe.com/blog/online-migrations) — 4-step dual-write pattern with shadow reads
- [GitHub: Upgrading to MySQL 8.0](https://github.blog/engineering/infrastructure/upgrading-github-com-to-mysql-8-0/) — rolling replica upgrades across 1200+ hosts
- [Citus: 7 Tips for Dealing with Postgres Locks](https://www.citusdata.com/blog/2018/02/22/seven-tips-for-dealing-with-postgres-locks/)
- [Prisma: Expand-Contract Guide](https://www.prisma.io/dataguide/types/relational/expand-and-contract-pattern)
- [pgroll](https://github.com/xataio/pgroll) — automated expand-contract for PostgreSQL using versioned views
- [Reshape](https://github.com/fabianlindfors/reshape) — zero-downtime schema migrations for PostgreSQL
- [Squawk](https://github.com/sbdchd/squawk) — PostgreSQL migration SQL linter
- [GitLab: Postmortem of Database Outage](https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/)
- [GitLab Migration Style Guide](https://docs.gitlab.com/development/migration_style_guide/) — `with_lock_retries`, lock_timeout cycling, statement_timeout discipline
- [PostgresAI: Zero-downtime DDL with lock_timeout and retries](https://postgres.ai/blog/20210923-zero-downtime-postgres-schema-migrations-lock-timeout-and-retries) — canonical lock-retry loop with exponential backoff + jitter
- [Strong Migrations (ankane)](https://github.com/ankane/strong_migrations) — Rails-flavored anti-pattern catalog applicable to any tool
- [Doctolib: safe-pg-migrations / Stop worrying about Postgres locks](https://medium.com/doctolib/stop-worrying-about-postgresql-locks-in-your-rails-migrations-3426027e9cc9)
- [GoCardless: Zero-downtime Postgres migrations: the hard parts](https://gocardless.com/blog/zero-downtime-postgres-migrations-the-hard-parts/)
- [Atlas: Migration directory integrity](https://atlasgo.io/concepts/migration-directory-integrity) — go-modules-style merkle hash for migration dirs
- [Atlas: The Hard Truth about GitOps and DB Rollbacks](https://atlasgo.io/blog/2024/11/14/the-hard-truth-about-gitops-and-db-rollbacks)
- [Atlas: Versioned apply](https://atlasgo.io/versioned/apply) and [`atlas schema diff`](https://atlasgo.io/declarative/diff) for drift detection
- [Sqitch tutorial](https://sqitch.org/docs/manual/sqitchtutorial/) — deploy/verify/revert with explicit dependencies
- [golang-migrate Postgres driver source](https://github.com/golang-migrate/migrate/tree/master/database/postgres) — advisory-lock + dirty-flag pattern
- [yandex/pgmigrate](https://github.com/yandex/pgmigrate) — pure-SQL Python migrator, sequence-gap enforcement
- [Yoyo migrations](https://ollycope.com/software/yoyo/latest/) — Python migrator with audit log
- [graphile/migrate](https://github.com/graphile/migrate) — forward-only Postgres migration tool
- [pgTAP](https://pgtap.org/) — unit-testing framework for Postgres schema and queries
- [pgBackRest](https://pgbackrest.org/) — production-grade Postgres backup & PITR
- [PostgreSQL 16 — CREATE INDEX](https://www.postgresql.org/docs/16/sql-createindex.html), [VACUUM](https://www.postgresql.org/docs/16/sql-vacuum.html), [ALTER TYPE](https://www.postgresql.org/docs/16/sql-altertype.html), [ALTER SYSTEM](https://www.postgresql.org/docs/16/sql-altersystem.html) — authoritative list of statements that cannot run inside a transaction block
- [Decoupling DB migrations from server startup (pythonspeed.com)](https://pythonspeed.com/articles/schema-migrations-server-startup/) — when to run migrations as part of `lifespan` vs as a separate job
