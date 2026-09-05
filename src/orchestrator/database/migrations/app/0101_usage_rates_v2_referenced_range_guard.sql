-- migration:     0101_usage_rates_v2_referenced_range_guard.sql
-- description:   Prevent closing a canonical rate before the end of any
--                retained publication plan event that references it.
-- depends-on:    0100_infrastructure_terminal_evidence_single_boundary.sql
-- expected:      < 30s; one partial index build plus bounded catalog DDL.
-- locks:         SHARE ROW EXCLUSIVE on usage_rates_v2 and publication events.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Hold both sides of the cross-table invariant stable from preflight through
-- trigger installation. Publication is still gated during this Slice 1
-- rollout, so the brief write lock does not interrupt customer traffic.
LOCK TABLE usage_rates_v2 IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE resource_publication_plan_events IN SHARE ROW EXCLUSIVE MODE;

CREATE INDEX resource_publication_plan_events_rate_reference_idx
    ON resource_publication_plan_events (canonical_rate_version_id, plan_id)
    WHERE canonical_rate_version_id IS NOT NULL;

DO $preflight$
DECLARE
    violating_rate UUID;
    violating_plan UUID;
BEGIN
    SELECT rate.id, plan.id
    INTO violating_rate, violating_plan
    FROM usage_rates_v2 AS rate
    JOIN resource_publication_plan_events AS event
      ON event.canonical_rate_version_id = rate.id
    JOIN resource_publication_plans AS plan
      ON plan.id = event.plan_id
    WHERE plan.state IN ('planned', 'published', 'conflict')
      AND (
          rate.effective_from > plan.period_start
          OR (rate.effective_to IS NOT NULL
              AND plan.period_end > rate.effective_to)
      )
    ORDER BY rate.id, plan.period_end, plan.id
    LIMIT 1;

    IF violating_rate IS NOT NULL THEN
        RAISE EXCEPTION
            'usage rate % does not cover retained publication plan %',
            violating_rate, violating_plan
            USING ERRCODE = '23514';
    END IF;
END;
$preflight$;

CREATE FUNCTION protect_usage_rate_v2_referenced_range()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    blocking_plan UUID;
BEGIN
    IF OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL THEN
        SELECT plan.id
        INTO blocking_plan
        FROM public.resource_publication_plan_events AS event
        JOIN public.resource_publication_plans AS plan
          ON plan.id = event.plan_id
        WHERE event.canonical_rate_version_id = OLD.id
          AND plan.state IN ('planned', 'published', 'conflict')
          AND plan.period_end > NEW.effective_to
        ORDER BY plan.period_end DESC, plan.id
        LIMIT 1;

        IF blocking_plan IS NOT NULL THEN
            RAISE EXCEPTION
                'usage rate % cannot close before retained publication plan % ends',
                OLD.id, blocking_plan
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

-- The unique trigger name is a required Slice 1 runtime capability marker.
CREATE TRIGGER usage_rates_v2_referenced_range_guard
BEFORE UPDATE OF effective_to ON usage_rates_v2
FOR EACH ROW
EXECUTE FUNCTION protect_usage_rate_v2_referenced_range();

COMMIT;
