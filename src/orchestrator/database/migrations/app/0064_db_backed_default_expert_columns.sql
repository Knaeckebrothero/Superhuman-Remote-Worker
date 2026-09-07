-- migration:     0064_db_backed_default_expert_columns.sql
-- description:   Add stable managed-seed identity to experts and allow the
--                platform-owned seed rows to exist without a human owner.
-- depends-on:    0028_experts.sql
-- expected:      < 1s (metadata-only nullable columns + small-table validate).
-- locks:         brief AccessExclusiveLock on experts.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Managed platform experts deliberately have no human owner. Human-authored
-- and admin-global experts continue to require one through the check below.
-- This broadens the column contract intentionally; older clients never relied
-- on owner_id being non-null for rows they could not yet create or observe.
-- squawk-ignore ban-drop-not-null
ALTER TABLE experts ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE experts ADD COLUMN IF NOT EXISTS managed_key VARCHAR(100);
ALTER TABLE experts ADD COLUMN IF NOT EXISTS seed_version INTEGER;

DO $$ BEGIN
    ALTER TABLE experts ADD CONSTRAINT experts_managed_owner_check CHECK (
        (managed_key IS NULL AND owner_id IS NOT NULL)
        OR
        (managed_key IS NOT NULL AND owner_id IS NULL AND is_global = TRUE)
    ) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE experts VALIDATE CONSTRAINT experts_managed_owner_check;

-- The typed pointer FKs introduced later in the same transactional pass need
-- this exact composite uniqueness before non-transactional migrations run.
-- experts is a small control-plane catalog and id is already the primary key,
-- so this validates trivially; the brief index lock is an intentional exception
-- to the normal CONCURRENTLY rule forced by the runner's tx-first ordering.
-- squawk-ignore require-concurrent-index-creation
DO $$ BEGIN
    ALTER TABLE experts ADD CONSTRAINT uq_experts_id_type
        UNIQUE (id, expert_type);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN experts.managed_key IS
    'Stable platform seed identity. Managed rows are global, ownerless and non-deletable.';

COMMIT;
