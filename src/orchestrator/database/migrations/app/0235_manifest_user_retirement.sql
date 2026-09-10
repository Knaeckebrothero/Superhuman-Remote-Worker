-- migration:     0235_manifest_user_retirement.sql
-- description:   Let a retired principal's manifest history and shared Project
--                credentials outlive the account that created them.
-- depends-on:    0234_manifest_resources.sql
-- expected:      < 5s. Both tables are CREATEd by 0234 and hold no rows.
-- locks:         srw_workspace_instances and srw_resource_secrets only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone = 'UTC';

-- User deletion preserves completed manifest history and shared Project
-- credentials. RESTRICT still requires the application retirement transaction;
-- deleting a principal never cascades through definitions or execution receipts.
ALTER TABLE srw_workspace_instances ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE srw_workspace_instances
    ADD CONSTRAINT srw_workspace_instances_retired_owner_check CHECK (
        owner_id IS NOT NULL OR (
            status = 'Released' AND execution_id IS NULL AND pod_uid IS NULL
        )
    ) NOT VALID;

-- Project membership, not the credential creator, owns access to these secrets.
-- Personal Account secrets remain owned and are removed with the account.
ALTER TABLE srw_resource_secrets ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE srw_resource_secrets
    ADD CONSTRAINT srw_resource_secrets_retired_owner_check CHECK (
        owner_id IS NOT NULL OR scope_kind = 'Project'
    ) NOT VALID;

COMMIT;
