-- migration:     0069_automation_expert_id.sql
-- description:   Give automations an explicit DB-expert pointer. Bundled
--                experts remain in the legacy `expert` config-name column;
--                DB-backed worker experts use `expert_id` with `expert`
--                normalized to `worker_base`.
-- depends-on:    0067_db_backed_default_expert_pointers.sql
-- expected:      < 1s on the small automations control-plane table.
-- locks:         brief AccessExclusiveLock on automations for ADD COLUMN /
--                constraint validation and a bounded row update for legacy
--                UUID-shaped Cockpit selections.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE automations
    ADD COLUMN IF NOT EXISTS expert_id UUID;

-- The old Cockpit sent Expert.id through the text `expert` field. Recover
-- existing DB-backed *worker* selections when the referenced row still exists.
-- Session-expert UUIDs were never valid for an automation and intentionally
-- remain untouched so fire-time validation can report the type mismatch.
UPDATE automations AS automation
SET expert_id = experts.id,
    expert = 'worker_base'
FROM experts
WHERE automation.expert_id IS NULL
  AND automation.expert = experts.id::text
  AND experts.expert_type = 'worker';

DO $$ BEGIN
    ALTER TABLE automations
        ADD CONSTRAINT automations_expert_id_fkey
        FOREIGN KEY (expert_id) REFERENCES experts(id) ON DELETE RESTRICT
        NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE automations VALIDATE CONSTRAINT automations_expert_id_fkey;

DO $$ BEGIN
    ALTER TABLE automations
        ADD CONSTRAINT automations_expert_source_check
        CHECK (expert_id IS NULL OR expert = 'worker_base')
        NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE automations VALIDATE CONSTRAINT automations_expert_source_check;

-- The FK is consulted when an expert is deleted. This table is a small
-- control-plane catalog, so a normal transactional index is preferable to a
-- separate non-transactional migration here.
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX IF NOT EXISTS idx_automations_expert_id
    ON automations (expert_id)
    WHERE expert_id IS NOT NULL;

COMMENT ON COLUMN automations.expert_id IS
    'Pinned DB-backed worker expert. NULL means `expert` is a bundled config '
    'name, or worker_base should resolve the owner''s effective default at fire time.';

COMMIT;
