-- migration:     0163_officer_first_default.sql
-- description:   Flip the ratified worker-question default from user_direct to
--                officer_first: new posts get it from the column DEFAULT,
--                existing posts still sitting on user_direct are moved onto it.
--                Resolves the "Commissioning default" open question in
--                docs/features/officer_message_routing.md.
-- depends-on:    0162_officer_ticket_claims.sql
-- expected:      < 1s. Metadata-only SET DEFAULT plus a full UPDATE of
--                project_officers (one row per project; dev: hundreds).
-- locks:         AccessExclusiveLock on project_officers for the SET DEFAULT
--                (brief, retried), RowExclusive for the UPDATE.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- 1. New posts. The insert sites all spell `INSERT INTO project_officers
--    (project_id) VALUES ($1)` and lean entirely on this DEFAULT, so changing
--    it here is the whole story for projects created from now on.
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
            ALTER TABLE public.project_officers
                ALTER COLUMN communication_policy SET DEFAULT
                '{"worker_messages":"officer_first","officer_response_minutes":15}'::jsonb;
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

-- 2. Existing posts. 0157 materialised user_direct onto every backfilled row,
--    so the SET DEFAULT above would otherwise leave every project that exists
--    today on the old behaviour.
--
--    Scoped to rows that are on user_direct or missing the key: an explicit
--    officer_and_user is a deliberate non-default choice and is left alone.
--    user_direct and "never touched" are indistinguishable on the row, and the
--    legate asked for the flip — a project that wants direct routing back
--    re-picks it in the officer tab, which PATCHes the row and is then immune
--    to any future default change.
--
--    Safe on projects without an officer: the resolver collapses to
--    user_direct whenever the post is vacant or the link is stale
--    (message_routing.resolve_job_routing, spec §2.1), so this only takes
--    effect where a live officer is actually commissioned.
UPDATE public.project_officers
   SET communication_policy =
           communication_policy || '{"worker_messages":"officer_first"}'::jsonb,
       updated_at = now()
 WHERE COALESCE(communication_policy->>'worker_messages', 'user_direct')
       = 'user_direct';

COMMENT ON COLUMN public.project_officers.communication_policy IS
    'Legate-owned worker-message routing policy (officer_message_routing). '
    'Row-only: resolved server-side per message, never mirrored into thread '
    'metadata, not writable by the officer runtime. Defaults to officer_first '
    'since 0163; effective policy is still user_direct while the post is '
    'vacant.';

COMMIT;
