-- migration:     0023_pg_trgm_extension.sql
-- description:   Trigram matching for the KB exact/grep channel
--                (kb_retrieval_hardening_and_slice_d_additive.md WP6, D11).
--                pg_trgm is a trusted extension since PG13, so the app owner can
--                create it the same way 0001 created `vector`.
-- depends-on:    0022_kb_watermark_wedge_tracking.sql
-- expected:      Instant.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
