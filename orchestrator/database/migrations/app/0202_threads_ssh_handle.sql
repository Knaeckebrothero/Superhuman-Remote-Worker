-- migration:     0202_threads_ssh_handle.sql
-- description:   Short public handle addressing a session over SSH.
-- depends-on:    0201_user_ssh_keys.sql
-- expected:     One nullable column on threads. No row rewrites; existing rows
--               are backfilled lazily by the application on first read.
-- locks:        Brief ACCESS EXCLUSIVE on threads to attach the column.
--               Retried, because threads is hot and carries several triggers.
-- transactional: yes
-- rollout:      Inert until the ssh-gateway ships.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

DO $$
DECLARE
    attempt int := 0;
BEGIN
    LOOP
        BEGIN
            ALTER TABLE public.threads ADD COLUMN IF NOT EXISTS ssh_handle text;
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

COMMIT;
