-- User deletion preserves completed manifest history and shared Project
-- credentials. RESTRICT still requires the application retirement transaction;
-- deleting a principal never cascades through definitions or execution receipts.
ALTER TABLE srw_workspace_instances ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE srw_workspace_instances
    ADD CONSTRAINT srw_workspace_instances_retired_owner_check CHECK (
        owner_id IS NOT NULL OR (
            status = 'Released' AND execution_id IS NULL AND pod_uid IS NULL
        )
    );

-- Project membership, not the credential creator, owns access to these secrets.
-- Personal Account secrets remain owned and are removed with the account.
ALTER TABLE srw_resource_secrets ALTER COLUMN owner_id DROP NOT NULL;
ALTER TABLE srw_resource_secrets
    ADD CONSTRAINT srw_resource_secrets_retired_owner_check CHECK (
        owner_id IS NOT NULL OR scope_kind = 'Project'
    );
