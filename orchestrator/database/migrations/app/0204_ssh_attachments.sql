-- migration:     0204_ssh_attachments.sql
-- description:   Audit record of user SSH attachments to session workspaces.
-- depends-on:    0203_threads_ssh_handle_idx.notx.sql
-- expected:     One new table plus two lookup indexes. No writes to existing rows.
-- locks:        ACCESS EXCLUSIVE on the new table, plus a brief
--               SHARE ROW EXCLUSIVE on each referenced table — public.threads,
--               public.users and public.user_ssh_keys — while their FK
--               enforcement triggers are installed. threads is hot and
--               trigger-heavy, so ordinary writes to it can queue briefly;
--               reads are unaffected. The CREATE TABLE is retried up to 10
--               times with a 1s backoff on lock_timeout, mirroring 0202:
--               an unretried 2s timeout would abort the whole transactional
--               pass and fail boot.
-- transactional: yes
-- rollout:      Written only by the ssh-gateway; inert until it ships.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Installing the FK to threads takes SHARE ROW EXCLUSIVE on it (see -- locks:
-- above), which conflicts with the ROW EXCLUSIVE ordinary writes hold.
-- threads is hot, so a straight CREATE TABLE can lose the race against
-- lock_timeout; retry it exactly like 0202 retries its ALTER TABLE, rather
-- than letting one timeout abort the whole transactional migration pass.
-- Each failed attempt is caught by this block's own implicit savepoint, so
-- a partially-installed table from a failed attempt is rolled back before
-- the next CREATE TABLE IF NOT EXISTS is tried -- it never sees a
-- half-built table left behind by the previous attempt.
DO $$
DECLARE
    attempt int := 0;
BEGIN
    LOOP
        BEGIN
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
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            attempt := attempt + 1;
            IF attempt >= 10 THEN
                RAISE;
            END IF;
            PERFORM pg_sleep(1);
        END;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_ssh_attachments_thread
    ON public.ssh_attachments (thread_id, attached_at DESC);
CREATE INDEX IF NOT EXISTS idx_ssh_attachments_user
    ON public.ssh_attachments (user_id, attached_at DESC);

COMMIT;
