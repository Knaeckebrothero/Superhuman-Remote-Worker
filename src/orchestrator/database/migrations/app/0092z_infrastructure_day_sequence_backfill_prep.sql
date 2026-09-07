-- migration:     0092z_infrastructure_day_sequence_backfill_prep.sql
-- description:   Prepare the immutable 0093 coverage-sequence backfill to
--                cross the Slice 0 sealed-day guard. This interstitial is a
--                no-op when 0093 or a later migration is already installed.
-- depends-on:    0092_inventory_invalid_watch_received_idx.notx.sql
-- expected:      < 1s; replaces one trigger function without scanning rows.
-- locks:         No table lock. The replacement is transactional and is not
--                visible before 0093 installs the permanent guard.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- 0093 was applied to early environments before this upgrade edge was found,
-- so its checksum is immutable. On a database still before 0093, the Slice 0
-- trigger rejects every UPDATE to a sealed row, including 0093's one-time
-- coverage_sequence 0 -> 1 backfill. Temporarily admit exactly that otherwise
-- identical update. The runner executes 0092z and 0093 in one transaction, so
-- no other session can observe this narrow guard before 0093 replaces it.
DO $migration$
BEGIN
    IF to_regclass('public.infra_usage_day_state') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'infra_usage_day_state'
              AND column_name = 'coverage_sequence'
       ) THEN
        EXECUTE $function$
CREATE OR REPLACE FUNCTION public.protect_infra_usage_day_state_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $body$
DECLARE
    old_row JSONB;
    new_row JSONB;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'open' THEN
            RAISE EXCEPTION
                'infrastructure usage day state must begin open'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'infrastructure usage day state cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'sealed' THEN
        old_row := to_jsonb(OLD);
        new_row := to_jsonb(NEW);
        IF old_row ? 'coverage_sequence'
           AND old_row ->> 'coverage_sequence' = '0'
           AND new_row ->> 'coverage_sequence' = '1'
           AND old_row - 'coverage_sequence'
                IS NOT DISTINCT FROM new_row - 'coverage_sequence' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'sealed infrastructure usage days are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.day <> OLD.day
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.state = 'open' AND NEW.state NOT IN ('open', 'sealing'))
       OR (OLD.state = 'sealing' AND NEW.state NOT IN ('sealing', 'sealed'))
    THEN
        RAISE EXCEPTION
            'infrastructure usage day state advances open to sealing to sealed'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$body$;
$function$;
    END IF;
END;
$migration$;

COMMIT;
