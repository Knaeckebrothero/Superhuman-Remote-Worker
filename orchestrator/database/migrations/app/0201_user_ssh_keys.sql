-- migration:     0201_user_ssh_keys.sql
-- description:   User-registered SSH public keys for workspace SSH access.
-- depends-on:    0200_pinned_agent_recycle_authority.sql
-- expected:     One new table plus a lookup index. No writes to existing rows.
-- locks:        ACCESS EXCLUSIVE on the new table only.
-- transactional: yes
-- rollout:      Inert until the ssh-gateway ships; the table is only read by
--               the internal ssh-targets endpoint and cockpit key CRUD.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE IF NOT EXISTS public.user_ssh_keys (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    name                text NOT NULL,
    key_type            text NOT NULL,
    public_key          text NOT NULL,
    fingerprint_sha256  text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_used_at        timestamptz,
    disabled_at         timestamptz,
    -- Global, not per-user. A public key identifies exactly one account, so
    -- the gateway can map a presented fingerprint to one user with no ambiguity.
    CONSTRAINT user_ssh_keys_fingerprint_sha256_key UNIQUE (fingerprint_sha256),
    CONSTRAINT user_ssh_keys_fingerprint_shape
        CHECK (fingerprint_sha256 ~ '^SHA256:[A-Za-z0-9+/]{43}$'),
    CONSTRAINT user_ssh_keys_name_present CHECK (length(btrim(name)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_user_ssh_keys_user
    ON public.user_ssh_keys (user_id);

COMMIT;
