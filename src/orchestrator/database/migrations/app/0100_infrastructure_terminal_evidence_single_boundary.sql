-- migration:     0100_infrastructure_terminal_evidence_single_boundary.sql
-- description:   Make a terminal interval's complete LIST boundary link
--                one-way even when two snapshots share its DB timestamp.
-- depends-on:    0099_infrastructure_terminal_evidence_equal_timestamp.sql
-- expected:      < 5s; function and row-trigger replacement only.
-- locks:         Brief SHARE ROW EXCLUSIVE lock on resource_intervals.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE OR REPLACE FUNCTION validate_resource_interval_snapshot_end_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    evidence_ok BOOLEAN;
    boundary_already_linked BOOLEAN;
BEGIN
    IF OLD.ended_at IS NULL
       OR (NEW.last_seen_at IS NOT DISTINCT FROM OLD.last_seen_at
           AND NEW.last_seen_snapshot_id
                IS NOT DISTINCT FROM OLD.last_seen_snapshot_id) THEN
        RETURN NEW;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.resource_inventory_snapshots snapshot
        WHERE snapshot.id = OLD.last_seen_snapshot_id
          AND snapshot.inventory_scope_id = OLD.inventory_scope_id
          AND snapshot.complete = TRUE
          AND snapshot.manifest_state IN ('sealed', 'items-expired')
          AND snapshot.received_at = OLD.ended_at
    ) INTO boundary_already_linked;

    IF boundary_already_linked THEN
        RAISE EXCEPTION
            'closed interval already has immutable terminal snapshot evidence'
            USING ERRCODE = '55000';
    END IF;

    SELECT TRUE INTO evidence_ok
    FROM public.resource_inventory_snapshots snapshot
    JOIN public.resource_inventory_snapshot_items item
      ON item.snapshot_id = snapshot.id
     AND item.source_kind = OLD.source_kind
     AND item.source_uid = OLD.source_uid
     AND item.valid_for_metering = TRUE
    WHERE OLD.end_time_source = 'app-db-received'
      AND OLD.end_reason IN ('not-applicable', 'terminal-or-unscheduled')
      AND OLD.last_seen_at <= OLD.ended_at
      AND NEW.last_seen_at = GREATEST(OLD.last_seen_at, OLD.ended_at)
      AND NEW.last_seen_snapshot_id = snapshot.id
      AND NEW.last_seen_snapshot_id
            IS DISTINCT FROM OLD.last_seen_snapshot_id
      AND snapshot.inventory_scope_id = OLD.inventory_scope_id
      AND snapshot.complete = TRUE
      AND snapshot.manifest_state IN ('sealed', 'items-expired')
      AND snapshot.received_at = OLD.ended_at;

    IF evidence_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'closed interval snapshot evidence does not match its terminal boundary'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER resource_intervals_snapshot_end_equal_timestamp_guard
    ON resource_intervals;

-- This distinct name is the runtime capability marker for the single-boundary
-- contract; the retained 0099 artifact alone cannot prove these semantics.
CREATE TRIGGER resource_intervals_snapshot_end_single_boundary_guard
BEFORE UPDATE OF last_seen_at, last_seen_snapshot_id ON resource_intervals
FOR EACH ROW
EXECUTE FUNCTION validate_resource_interval_snapshot_end_evidence();

COMMIT;
