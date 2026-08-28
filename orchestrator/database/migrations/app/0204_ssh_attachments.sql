-- migration:     0204_ssh_attachments.sql
-- description:   Audit record of user SSH attachments to session workspaces.
-- depends-on:    0203_threads_ssh_handle_idx.notx.sql
-- expected:     One new table plus two lookup indexes. No writes to existing rows.
-- locks:        ACCESS EXCLUSIVE on the new table, plus a brief
--               SHARE ROW EXCLUSIVE on each referenced table — public.threads,
--               public.users and public.user_ssh_keys — while their FK
--               enforcement triggers are installed. threads is hot and
--               trigger-heavy, so ordinary writes to it can queue briefly;
--               reads are unaffected and lock_timeout bounds the wait.
-- transactional: yes
-- rollout:      Written only by the ssh-gateway; inert until it ships.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE IF NOT EXISTS public.ssh_attachments (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id    uuid NOT NULL REFERENCES public.threads(id) ON DELETE CASCADE,
    user_id      uuid REFERENCES public.users(id) ON DELETE SET NULL,
    -- Revoking a key must not erase the history of what it did.
    ssh_key_id   uuid REFERENCES public.user_ssh_keys(id) ON DELETE SET NULL,
    handle       text NOT NULL,
    client_ip    inet,
    channels     text[] NOT NULL DEFAULT '{}',
    attached_at  timestamptz NOT NULL DEFAULT now(),
    detached_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_ssh_attachments_thread
    ON public.ssh_attachments (thread_id, attached_at DESC);
CREATE INDEX IF NOT EXISTS idx_ssh_attachments_user
    ON public.ssh_attachments (user_id, attached_at DESC);

COMMIT;
