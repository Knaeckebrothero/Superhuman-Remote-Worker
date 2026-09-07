-- Inventory item byte accounting must be independent of PostgreSQL's TOAST
-- representation. INSERT ... RETURNING can expose an expanded JSONB datum while
-- a later table read exposes its compressed on-disk form; pg_column_size()
-- therefore produced different values for the same row and tripped the
-- ingest-ticket staged_bytes fence for normal Pod payloads.

SET LOCAL timezone = 'UTC';

CREATE OR REPLACE FUNCTION resource_inventory_snapshot_item_size_bytes(
    source_kind TEXT,
    source_uid TEXT,
    revision_hash TEXT,
    normalized_item JSONB,
    item_error JSONB
)
RETURNS BIGINT
LANGUAGE SQL
IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT 64::BIGINT
         + octet_length(source_kind)::BIGINT
         + octet_length(source_uid)::BIGINT
         + COALESCE(octet_length(revision_hash), 0)::BIGINT
         + octet_length(normalized_item::TEXT)::BIGINT
         + COALESCE(octet_length(item_error::TEXT), 0)::BIGINT
$$;

COMMENT ON FUNCTION resource_inventory_snapshot_item_size_bytes(
    TEXT, TEXT, TEXT, JSONB, JSONB
) IS
    'Deterministic logical payload bytes for snapshot bounds; never physical TOAST size.';
