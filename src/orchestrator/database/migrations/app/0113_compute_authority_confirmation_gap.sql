-- migration:     0113_compute_authority_confirmation_gap.sql
-- description:   Keep exact compute epoch authority incomplete until a later
--                complete LIST confirms every eligible interval.
-- depends-on:    0112_compute_epoch_rollover_authority.sql
-- expected:      < 10s while Slice 3 compute publication remains disabled.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on compute authority,
--                inventory epochs, and inventory coverage gaps.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE compute_metering_epoch_authorities,
           resource_inventory_scope_epochs,
           resource_inventory_coverage_gaps
    IN SHARE ROW EXCLUSIVE MODE;

-- Authorities created in the narrow 0112 -> 0113 upgrade window must receive
-- the same fail-closed gap as future authorities. A recovery authority cannot
-- be backfilled safely unless its predecessor has an exact retirement clock.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.compute_metering_epoch_authorities AS authority
        LEFT JOIN public.resource_inventory_scope_epochs AS predecessor
          ON predecessor.id = authority.predecessor_epoch_id
         AND predecessor.scope_id = authority.inventory_scope_id
        WHERE authority.authority_sequence > 1
          AND predecessor.retired_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'cannot backfill compute authority confirmation gap without predecessor retirement'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

INSERT INTO public.resource_inventory_coverage_gaps (
    scope_epoch_id, gap_start, reason, resolution_details
)
SELECT authority.inventory_scope_epoch_id,
       CASE WHEN authority.authority_sequence = 1
            THEN authority.effective_from ELSE predecessor.retired_at END,
       'compute-authority-awaiting-confirmation:' || authority.activation_key,
       pg_catalog.jsonb_build_object(
           'code', 'compute-authority-awaiting-confirmation',
           'activation_key', authority.activation_key,
           'authority_id', authority.id,
           'previous_authority_id', authority.previous_authority_id,
           'promotion_request_id', authority.promotion_request_id,
           'authority_effective_from', authority.effective_from,
           'backfilled_by_migration', TRUE
       )
FROM public.compute_metering_epoch_authorities AS authority
LEFT JOIN public.resource_inventory_scope_epochs AS predecessor
  ON predecessor.id = authority.predecessor_epoch_id
 AND predecessor.scope_id = authority.inventory_scope_id
WHERE NOT EXISTS (
    SELECT 1
    FROM public.resource_inventory_coverage_gaps AS gap
    WHERE gap.scope_epoch_id = authority.inventory_scope_epoch_id
      AND gap.reason =
          'compute-authority-awaiting-confirmation:' || authority.activation_key
);

CREATE FUNCTION record_compute_authority_confirmation_gap()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    authority_gap_start TIMESTAMPTZ;
BEGIN
    IF NEW.authority_sequence = 1 THEN
        authority_gap_start := NEW.effective_from;
    ELSE
        SELECT epoch.retired_at
        INTO authority_gap_start
        FROM public.resource_inventory_scope_epochs AS epoch
        WHERE epoch.id = NEW.predecessor_epoch_id
          AND epoch.scope_id = NEW.inventory_scope_id
        FOR SHARE;

        IF authority_gap_start IS NULL THEN
            RAISE EXCEPTION
                'compute authority confirmation gap requires predecessor retirement'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    INSERT INTO public.resource_inventory_coverage_gaps (
        scope_epoch_id, gap_start, reason, resolution_details
    ) VALUES (
        NEW.inventory_scope_epoch_id,
        authority_gap_start,
        'compute-authority-awaiting-confirmation:' || NEW.activation_key,
        pg_catalog.jsonb_build_object(
            'code', 'compute-authority-awaiting-confirmation',
            'activation_key', NEW.activation_key,
            'authority_id', NEW.id,
            'previous_authority_id', NEW.previous_authority_id,
            'promotion_request_id', NEW.promotion_request_id,
            'authority_effective_from', NEW.effective_from
        )
    );

    RETURN NEW;
END;
$$;

CREATE TRIGGER compute_epoch_authority_confirmation_gap
AFTER INSERT ON compute_metering_epoch_authorities
FOR EACH ROW EXECUTE FUNCTION record_compute_authority_confirmation_gap();

COMMENT ON FUNCTION record_compute_authority_confirmation_gap() IS
    'Opens a fail-closed coverage gap until a post-authority complete LIST confirms exact interval binding.';

COMMIT;
