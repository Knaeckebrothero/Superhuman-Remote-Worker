-- migration:     0099_infrastructure_terminal_evidence_equal_timestamp.sql
-- description:   Link terminal LIST evidence when the prior sighting and
--                immutable close boundary share the same DB timestamp.
-- depends-on:    0098_infrastructure_terminal_evidence.sql
-- expected:      < 5s; function and row-trigger replacement only.
-- locks:         Brief SHARE ROW EXCLUSIVE lock on resource_intervals.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- 0098 deliberately admitted only an advancing last_seen_at update. A LIST
-- item can already have been observed at the exact terminal receipt timestamp,
-- in which case only its durable snapshot link needs to change. Preserve the
-- same one-time evidence shape while admitting that equal-timestamp case.
CREATE OR REPLACE FUNCTION validate_resource_interval_snapshot_end_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    evidence_ok BOOLEAN;
BEGIN
    IF OLD.ended_at IS NULL
       OR (NEW.last_seen_at IS NOT DISTINCT FROM OLD.last_seen_at
           AND NEW.last_seen_snapshot_id
                IS NOT DISTINCT FROM OLD.last_seen_snapshot_id) THEN
        RETURN NEW;
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

CREATE OR REPLACE FUNCTION protect_resource_interval_revision_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_end_link BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'resource interval revisions are retained and cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    snapshot_end_link := OLD.ended_at IS NOT NULL
        AND OLD.end_time_source = 'app-db-received'
        AND OLD.end_reason IN ('not-applicable', 'terminal-or-unscheduled')
        AND OLD.last_seen_at <= OLD.ended_at
        AND NEW.last_seen_at = GREATEST(OLD.last_seen_at, OLD.ended_at)
        AND NEW.last_seen_snapshot_id IS NOT NULL
        AND NEW.last_seen_snapshot_id
            IS DISTINCT FROM OLD.last_seen_snapshot_id;

    IF (to_jsonb(NEW)
            - 'ended_at' - 'end_time_source' - 'end_uncertainty_us'
            - 'end_reason' - 'last_seen_at' - 'last_confirmed_at'
            - 'last_seen_snapshot_id' - 'materialized_through' - 'updated_at')
       <> (to_jsonb(OLD)
            - 'ended_at' - 'end_time_source' - 'end_uncertainty_us'
            - 'end_reason' - 'last_seen_at' - 'last_confirmed_at'
            - 'last_seen_snapshot_id' - 'materialized_through' - 'updated_at') THEN
        RAISE EXCEPTION
            'event-affecting interval revision fields are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.last_seen_at < OLD.last_seen_at
       OR NEW.last_confirmed_at < OLD.last_confirmed_at
       OR NEW.materialized_through < OLD.materialized_through
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.last_seen_snapshot_id IS NOT NULL
           AND NEW.last_seen_snapshot_id IS NULL) THEN
        RAISE EXCEPTION
            'interval liveness and materialization cursors are monotonic'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.ended_at IS NOT NULL THEN
        IF NEW.ended_at IS DISTINCT FROM OLD.ended_at
           OR NEW.end_time_source IS DISTINCT FROM OLD.end_time_source
           OR NEW.end_uncertainty_us IS DISTINCT FROM OLD.end_uncertainty_us
           OR NEW.end_reason IS DISTINCT FROM OLD.end_reason
           OR NEW.last_confirmed_at IS DISTINCT FROM OLD.last_confirmed_at
           OR (NOT snapshot_end_link AND (
                NEW.last_seen_at IS DISTINCT FROM OLD.last_seen_at
                OR NEW.last_seen_snapshot_id
                    IS DISTINCT FROM OLD.last_seen_snapshot_id))
        THEN
            RAISE EXCEPTION
                'closed interval evidence and end metadata are immutable'
                USING ERRCODE = '55000';
        END IF;
    ELSIF NEW.ended_at IS NULL THEN
        IF NEW.end_time_source IS NOT NULL
           OR NEW.end_uncertainty_us IS NOT NULL
           OR NEW.end_reason IS NOT NULL THEN
            RAISE EXCEPTION
                'open intervals cannot carry end metadata'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER resource_intervals_snapshot_end_evidence_guard
    ON resource_intervals;

-- The new name is an explicit runtime capability marker for 0099; checking
-- the old 0098 trigger cannot prove equal-timestamp semantics are installed.
CREATE TRIGGER resource_intervals_snapshot_end_equal_timestamp_guard
BEFORE UPDATE OF last_seen_at, last_seen_snapshot_id ON resource_intervals
FOR EACH ROW
EXECUTE FUNCTION validate_resource_interval_snapshot_end_evidence();

COMMIT;
