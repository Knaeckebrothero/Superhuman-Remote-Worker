-- migration:     0003_infrastructure_usage_events_v2.sql
-- description:   Nullable typed infrastructure fields on the canonical audit
--                ledger, validated-on-write v2 contracts, append-only
--                enforcement, and statement-batched dirty-day tracking.
-- depends-on:    0002_usage_events.sql
-- expected:      < 2s. Nullable columns and NOT VALID checks are catalog-only;
--                the dirty table starts empty and is bootstrapped by 0004.
-- locks:         Brief ACCESS EXCLUSIVE locks on usage_events while adding
--                nullable columns/check metadata and installing triggers.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- PostgreSQL round(numeric, scale) breaks exact .5 ties away from zero. The
-- metering contract uses Decimal ROUND_HALF_EVEN, so cost validation needs a
-- database implementation with the same result for positive and negative
-- correction deltas.
CREATE OR REPLACE FUNCTION round_half_even_v2(
    input_value NUMERIC,
    decimal_scale INTEGER
)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
DECLARE
    scale_factor NUMERIC;
    shifted_value NUMERIC;
    integral_part NUMERIC;
    fractional_part NUMERIC;
BEGIN
    IF decimal_scale < 0 OR decimal_scale > 38 THEN
        RAISE EXCEPTION 'decimal_scale must be between 0 and 38'
            USING ERRCODE = '22023';
    END IF;

    scale_factor := power(10::NUMERIC, decimal_scale);
    shifted_value := input_value * scale_factor;
    integral_part := trunc(shifted_value);
    fractional_part := abs(shifted_value - integral_part);

    IF fractional_part > 0.5
       OR (fractional_part = 0.5 AND mod(abs(integral_part), 2) = 1) THEN
        integral_part := integral_part + sign(shifted_value);
    END IF;

    RETURN integral_part / scale_factor;
END;
$$;

-- All additions are nullable so the existing point-event LLM/workspace ledger
-- remains byte-for-byte immutable and valid. Infrastructure v2 writers must
-- satisfy the checks below; no migration backfills legacy rows.
ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS period_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS period_end TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS measurement_basis TEXT,
    ADD COLUMN IF NOT EXISTS cost_domain TEXT,
    ADD COLUMN IF NOT EXISTS resource_class TEXT,
    ADD COLUMN IF NOT EXISTS attribution_scope TEXT,
    ADD COLUMN IF NOT EXISTS measurement_algorithm TEXT,
    ADD COLUMN IF NOT EXISTS source_capacity_value NUMERIC,
    ADD COLUMN IF NOT EXISTS source_capacity_unit TEXT,
    ADD COLUMN IF NOT EXISTS source_cluster TEXT,
    ADD COLUMN IF NOT EXISTS source_kind TEXT,
    ADD COLUMN IF NOT EXISTS source_uid TEXT,
    ADD COLUMN IF NOT EXISTS source_lifecycle_id UUID,
    ADD COLUMN IF NOT EXISTS source_interval_id UUID,
    ADD COLUMN IF NOT EXISTS event_kind TEXT,
    ADD COLUMN IF NOT EXISTS corrects_source TEXT,
    ADD COLUMN IF NOT EXISTS corrects_source_id TEXT,
    ADD COLUMN IF NOT EXISTS corrects_unit TEXT,
    ADD COLUMN IF NOT EXISTS corrects_ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS correction_group_id UUID,
    ADD COLUMN IF NOT EXISTS correction_reason TEXT,
    ADD COLUMN IF NOT EXISTS correction_actor_id UUID,
    ADD COLUMN IF NOT EXISTS discovered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS payload_hash TEXT;

-- NOT VALID avoids scanning the retained partition set while holding the
-- column-add lock. PostgreSQL still enforces these checks on every new row.
-- 0004 validates the historical set in a separate transaction.
DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'usage_events'::REGCLASS
          AND conname = 'usage_events_period_bounds_v2_check'
    ) THEN
        ALTER TABLE usage_events
            ADD CONSTRAINT usage_events_period_bounds_v2_check CHECK (
                (period_start IS NULL) = (period_end IS NULL)
                AND (
                    period_start IS NULL
                    OR period_end > period_start
                )
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'usage_events'::REGCLASS
          AND conname = 'usage_events_infra_v2_contract_check'
    ) THEN
        ALTER TABLE usage_events
            ADD CONSTRAINT usage_events_infra_v2_contract_check CHECK (
                source NOT IN (
                    'infra-allocation-v2',
                    'infra-allocation-correction-v2'
                )
                OR (
                    period_start IS NOT NULL
                    AND period_end IS NOT NULL
                    AND ts = period_start
                    AND period_end <= date_trunc(
                        'day', period_start, 'UTC'
                    ) + INTERVAL '1 day'
                    AND measurement_basis IS NOT NULL
                    AND cost_domain IS NOT NULL
                    AND resource_class IS NOT NULL
                    AND resource_class <> ''
                    AND attribution_scope IS NOT NULL
                    AND measurement_algorithm IS NOT NULL
                    AND measurement_algorithm <> ''
                    AND source_capacity_value IS NOT NULL
                    AND source_capacity_value NOT IN (
                        'NaN'::NUMERIC,
                        'Infinity'::NUMERIC,
                        '-Infinity'::NUMERIC
                    )
                    AND abs(source_capacity_value)
                        < 100000000000000000000::NUMERIC
                    AND source_capacity_value = trunc(source_capacity_value, 18)
                    AND source_capacity_value >= 0
                    AND source_capacity_value = trunc(source_capacity_value)
                    AND source_capacity_unit IS NOT NULL
                    AND source_capacity_unit <> ''
                    AND source_cluster IS NOT NULL
                    AND source_cluster <> ''
                    AND source_kind IS NOT NULL
                    AND source_uid IS NOT NULL
                    AND source_uid <> ''
                    AND source_lifecycle_id IS NOT NULL
                    AND source_interval_id IS NOT NULL
                    AND event_kind IS NOT NULL
                    AND payload_hash IS NOT NULL
                    AND payload_hash ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(details) = 'object'
                    AND category <> ''
                    AND resource <> ''
                    AND unit <> ''
                    AND cost_domain IN (
                        'workload-allocation', 'physical-asset',
                        'idle', 'overhead'
                    )
                    AND attribution_scope IN (
                        'customer', 'shared-platform', 'unknown'
                    )
                    AND (
                        (
                            attribution_scope = 'customer'
                            AND ref_kind IS NOT NULL
                            AND ref_kind IN ('job', 'thread')
                            AND ref_id IS NOT NULL
                            AND user_id IS NOT NULL
                        )
                        OR (
                            attribution_scope IN (
                                'shared-platform', 'unknown'
                            )
                            AND user_id IS NULL
                            AND project_id IS NULL
                        )
                    )
                    AND (
                        (
                            source_kind = 'pod'
                            AND category = 'compute'
                            AND measurement_basis = 'scheduler-request'
                            AND resource_class = 'kubernetes-pod'
                            AND cost_domain = 'workload-allocation'
                            AND (
                                (unit = 'vcpu-hour'
                                    AND source_capacity_unit = 'millicore')
                                OR (unit = 'gib-hour'
                                    AND source_capacity_unit = 'byte')
                            )
                        )
                        OR (
                            source_kind = 'vmi'
                            AND category = 'compute'
                            AND measurement_basis = 'guest-provisioned'
                            AND resource_class = 'virtual-machine'
                            AND cost_domain = 'workload-allocation'
                            AND (
                                (unit = 'vcpu-hour'
                                    AND source_capacity_unit = 'millicore')
                                OR (unit = 'gib-hour'
                                    AND source_capacity_unit = 'byte')
                            )
                        )
                        OR (
                            source_kind = 'pvc'
                            AND category = 'storage'
                            AND measurement_basis = 'claim-requested'
                            AND resource_class = 'persistent-volume-claim'
                            AND cost_domain = 'workload-allocation'
                            AND (
                                (unit = 'gib-hour'
                                    AND source_capacity_unit = 'byte')
                                OR (unit = 'claim-hour'
                                    AND source_capacity_unit = 'instance'
                                    AND source_capacity_value = 1)
                            )
                        )
                        OR (
                            source_kind = 'volume'
                            AND category = 'storage'
                            AND measurement_basis = 'volume-provisioned'
                            AND resource_class = 'persistent-volume'
                            AND cost_domain = 'physical-asset'
                            AND (
                                (unit = 'gib-hour'
                                    AND source_capacity_unit = 'byte')
                                OR (unit = 'volume-hour'
                                    AND source_capacity_unit = 'instance'
                                    AND source_capacity_value = 1)
                            )
                        )
                    )
                    AND (
                        (rate_usd IS NULL AND cost_usd IS NULL)
                        OR (
                            rate_usd IS NOT NULL
                            AND rate_usd NOT IN (
                                'NaN'::NUMERIC,
                                'Infinity'::NUMERIC,
                                '-Infinity'::NUMERIC
                            )
                            AND abs(rate_usd)
                                < 100000000000000000000::NUMERIC
                            AND rate_usd = trunc(rate_usd, 18)
                            AND rate_usd >= 0
                            AND cost_usd IS NOT NULL
                            AND cost_usd NOT IN (
                                'NaN'::NUMERIC,
                                'Infinity'::NUMERIC,
                                '-Infinity'::NUMERIC
                            )
                            AND abs(cost_usd)
                                < 100000000000000000000::NUMERIC
                            AND cost_usd = trunc(cost_usd, 18)
                            AND CASE
                                WHEN quantity IN (
                                    'NaN'::NUMERIC,
                                    'Infinity'::NUMERIC,
                                    '-Infinity'::NUMERIC
                                ) OR rate_usd IN (
                                    'NaN'::NUMERIC,
                                    'Infinity'::NUMERIC,
                                    '-Infinity'::NUMERIC
                                ) OR cost_usd IN (
                                    'NaN'::NUMERIC,
                                    'Infinity'::NUMERIC,
                                    '-Infinity'::NUMERIC
                                ) THEN FALSE
                                ELSE cost_usd = round_half_even_v2(
                                    quantity * rate_usd, 18
                                )
                            END
                        )
                    )
                    AND quantity NOT IN (
                        'NaN'::NUMERIC,
                        'Infinity'::NUMERIC,
                        '-Infinity'::NUMERIC
                    )
                    AND abs(quantity) < 100000000000000000000::NUMERIC
                    AND quantity = trunc(quantity, 18)
                )
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'usage_events'::REGCLASS
          AND conname = 'usage_events_event_kind_v2_check'
    ) THEN
        ALTER TABLE usage_events
            ADD CONSTRAINT usage_events_event_kind_v2_check CHECK (
                (
                    source = 'infra-allocation-v2'
                    AND event_kind IN ('usage', 'late-usage')
                    AND quantity >= 0
                    AND corrects_source IS NULL
                    AND corrects_source_id IS NULL
                    AND corrects_unit IS NULL
                    AND corrects_ts IS NULL
                    AND correction_group_id IS NULL
                    AND correction_reason IS NULL
                    AND correction_actor_id IS NULL
                    AND (
                        (event_kind = 'usage' AND discovered_at IS NULL)
                        OR (
                            event_kind = 'late-usage'
                            AND discovered_at IS NOT NULL
                            AND discovered_at >= period_end
                        )
                    )
                )
                OR (
                    source = 'infra-allocation-correction-v2'
                    AND event_kind = 'correction'
                    AND corrects_source = 'infra-allocation-v2'
                    AND corrects_source_id IS NOT NULL
                    AND corrects_source_id <> ''
                    AND corrects_unit IS NOT NULL
                    AND corrects_unit = unit
                    AND corrects_ts IS NOT NULL
                    AND corrects_ts = period_start
                    AND correction_group_id IS NOT NULL
                    AND correction_reason IS NOT NULL
                    AND correction_reason <> ''
                    AND correction_actor_id IS NOT NULL
                    AND (
                        discovered_at IS NULL
                        OR discovered_at >= period_end
                    )
                )
                OR (
                    source NOT IN (
                        'infra-allocation-v2',
                        'infra-allocation-correction-v2'
                    )
                    AND event_kind IS NULL
                    AND corrects_source IS NULL
                    AND corrects_source_id IS NULL
                    AND corrects_unit IS NULL
                    AND corrects_ts IS NULL
                    AND correction_group_id IS NULL
                    AND correction_reason IS NULL
                    AND correction_actor_id IS NULL
                    AND discovered_at IS NULL
                )
            ) NOT VALID;
    END IF;
END;
$constraints$;

-- One row per UTC day, not one row per event. Revision is a change token: the
-- rollup records the revision it applied, so a concurrent higher revision stays
-- dirty and is rebuilt on the next pass.
CREATE TABLE IF NOT EXISTS usage_rollup_dirty_days (
    day         DATE PRIMARY KEY,
    revision    BIGINT NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_rollup_dirty_days_revision_check CHECK (revision > 0)
);

CREATE OR REPLACE FUNCTION mark_usage_rollup_dirty_days_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    INSERT INTO public.usage_rollup_dirty_days (day, revision, updated_at)
    SELECT
        (inserted.ts AT TIME ZONE 'UTC')::DATE,
        1,
        statement_timestamp()
    FROM inserted_usage_events AS inserted
    GROUP BY (inserted.ts AT TIME ZONE 'UTC')::DATE
    ON CONFLICT (day) DO UPDATE
    SET revision = public.usage_rollup_dirty_days.revision + 1,
        updated_at = EXCLUDED.updated_at;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS usage_events_rollup_dirty_days ON usage_events;
CREATE TRIGGER usage_events_rollup_dirty_days
AFTER INSERT ON usage_events
REFERENCING NEW TABLE AS inserted_usage_events
FOR EACH STATEMENT
EXECUTE FUNCTION mark_usage_rollup_dirty_days_v2();

-- Corrections are additive events. Updating/deleting an accepted audit row
-- would bypass its hash, correction provenance, and dirty-day revision.
CREATE OR REPLACE FUNCTION reject_usage_event_mutation_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'usage_events is append-only; publish a typed correction instead'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS usage_events_append_only_v2 ON usage_events;
CREATE TRIGGER usage_events_append_only_v2
BEFORE UPDATE OR DELETE ON usage_events
FOR EACH ROW
EXECUTE FUNCTION reject_usage_event_mutation_v2();

COMMENT ON TABLE usage_rollup_dirty_days IS
    'Monotonic per-UTC-day audit change tokens for repeatable v2 daily rebuilds.';
COMMENT ON COLUMN usage_events.period_start IS
    'Typed infrastructure half-open segment start; NULL for legacy point events.';
COMMENT ON COLUMN usage_events.payload_hash IS
    'Lowercase SHA-256 of the versioned canonical typed event payload.';
COMMENT ON FUNCTION round_half_even_v2(NUMERIC, INTEGER) IS
    'Immutable Decimal ROUND_HALF_EVEN equivalent for v2 NUMERIC contracts.';

COMMIT;
