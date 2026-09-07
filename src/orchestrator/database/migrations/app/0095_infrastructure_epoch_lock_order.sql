-- migration:     0095_infrastructure_epoch_lock_order.sql
-- description:   Acquire the metering control lock before inventory epoch row
--                locks so snapshot finalization and irreversible cutover use
--                one deadlock-free lock order.
-- depends-on:    0094_infrastructure_workspace_cutover_hardening.sql
-- expected:      < 5s. Adds two statement triggers to a low-cardinality table.
-- locks:         Brief ACCESS EXCLUSIVE locks on
--                resource_inventory_scope_epochs while the triggers are added.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- The 0094 row-level boundary validator takes control FOR SHARE after the
-- target epoch row is already locked. Cutover takes those locks in the opposite
-- order: control FOR UPDATE, then epoch FOR UPDATE. A complete-snapshot update
-- always names required_for_rollup and required_from in its SET list, so that
-- inversion is reachable during ordinary collection.
--
-- These BEFORE STATEMENT triggers acquire control first. The existing row-level
-- validator deliberately remains in place: its second FOR SHARE is re-entrant
-- and therefore cannot wait after an epoch row has been locked.
CREATE FUNCTION lock_inventory_epoch_boundary_statement()
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

CREATE TRIGGER resource_inventory_scope_epochs_boundary_insert_lock
BEFORE INSERT ON resource_inventory_scope_epochs
FOR EACH STATEMENT
EXECUTE FUNCTION lock_inventory_epoch_boundary_statement();

CREATE TRIGGER resource_inventory_scope_epochs_boundary_update_lock
BEFORE UPDATE OF required_for_rollup, required_from
ON resource_inventory_scope_epochs
FOR EACH STATEMENT
EXECUTE FUNCTION lock_inventory_epoch_boundary_statement();

COMMIT;
