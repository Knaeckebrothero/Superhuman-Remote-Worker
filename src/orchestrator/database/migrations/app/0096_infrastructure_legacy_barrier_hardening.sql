-- migration:     0096_infrastructure_legacy_barrier_hardening.sql
-- description:   Serialize every legacy workspace insert before row mutation,
--                reject closed-row inserts after cutover, and freeze the
--                durable legacy-drain completion timestamp.
-- depends-on:    0095_infrastructure_epoch_lock_order.sql
-- expected:      < 5s while cutover is disabled. Adds two small triggers and
--                replaces one trigger function without scanning table data.
-- locks:         Brief ACCESS EXCLUSIVE locks on workspace_intervals and
--                infra_metering_control. Deploy with cutover disabled.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Obtain control before INSERT can lock/create a workspace row. The existing
-- row trigger then rechecks the current state under a re-entrant SHARE lock.
CREATE FUNCTION lock_legacy_workspace_insert_statement()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    control_exists BOOLEAN;
BEGIN
    SELECT TRUE INTO control_exists
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF control_exists IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'infra metering control row is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_legacy_workspace_cutover_barrier()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_state TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.ended_at IS NOT NULL
           AND NEW.ended_at IS DISTINCT FROM OLD.ended_at THEN
            RAISE EXCEPTION 'closed legacy workspace end is immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    SELECT cutover_state INTO current_state
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF current_state IS NULL OR current_state <> 'disabled' THEN
        RAISE EXCEPTION 'legacy workspace inserts are disabled by metering cutover'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workspace_intervals_cutover_insert_lock
BEFORE INSERT ON workspace_intervals
FOR EACH STATEMENT
EXECUTE FUNCTION lock_legacy_workspace_insert_statement();

-- 0093 already freezes legacy_drained_at while the state stays preparing and
-- after it is active. Cover the omitted ready-to-activate -> active edge with a
-- separate monotonic guard, avoiding any rewrite of the applied state machine.
CREATE FUNCTION protect_infra_metering_legacy_drain_completion()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.legacy_drained_at IS NOT NULL
       AND NEW.legacy_drained_at IS DISTINCT FROM OLD.legacy_drained_at THEN
        RAISE EXCEPTION 'legacy drain completion is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER infra_metering_control_legacy_drain_immutable
BEFORE UPDATE OF legacy_drained_at ON infra_metering_control
FOR EACH ROW
EXECUTE FUNCTION protect_infra_metering_legacy_drain_completion();

COMMIT;
