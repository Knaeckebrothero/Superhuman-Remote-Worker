-- migration:     0097_infrastructure_lifecycle_head_lock_order.sql
-- description:   Acquire metering control before Pod lifecycle-head rows so
--                reconciliation and irreversible cutover share one lock order.
-- depends-on:    0096_infrastructure_legacy_barrier_hardening.sql
-- expected:      < 5s. Adds one statement trigger to a bounded state table.
-- locks:         Brief ACCESS EXCLUSIVE lock on resource_lifecycle_heads while
--                the trigger is added. Deploy with cutover disabled.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Opening or revising a Pod interval advances its lifecycle head before the
-- resource_intervals statement trigger acquires control. Cutover takes control
-- first and advances the same head after locking the open interval. Locking
-- control here, before INSERT/UPDATE can take a head row, removes that cycle.
CREATE FUNCTION serialize_resource_lifecycle_head_with_cutover()
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

CREATE TRIGGER resource_lifecycle_heads_cutover_serialization
BEFORE INSERT OR UPDATE ON resource_lifecycle_heads
FOR EACH STATEMENT
EXECUTE FUNCTION serialize_resource_lifecycle_head_with_cutover();

COMMIT;
