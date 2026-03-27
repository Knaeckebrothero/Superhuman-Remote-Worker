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

Stay with the current `schema.sql` approach for now, but follow expand-contract discipline for any breaking change. If the migration block count in `schema.sql` grows past ~15-20, consider splitting into a `migrations/` directory with numbered files and a `schema_version` table. Pair with Squawk in CI immediately — it's zero-effort and catches the most dangerous mistakes.

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

## References

- [Stripe: Online Migrations at Scale](https://stripe.com/blog/online-migrations) — 4-step dual-write pattern with shadow reads
- [GitHub: Upgrading to MySQL 8.0](https://github.blog/engineering/infrastructure/upgrading-github-com-to-mysql-8-0/) — rolling replica upgrades across 1200+ hosts
- [Citus: 7 Tips for Dealing with Postgres Locks](https://www.citusdata.com/blog/2018/02/22/seven-tips-for-dealing-with-postgres-locks/)
- [Prisma: Expand-Contract Guide](https://www.prisma.io/dataguide/types/relational/expand-and-contract-pattern)
- [pgroll](https://github.com/xataio/pgroll) — automated expand-contract for PostgreSQL using versioned views
- [Reshape](https://github.com/fabianlindfors/reshape) — zero-downtime schema migrations for PostgreSQL
- [Squawk](https://github.com/sbdchd/squawk) — PostgreSQL migration SQL linter
- [GitLab: Postmortem of Database Outage](https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/)
