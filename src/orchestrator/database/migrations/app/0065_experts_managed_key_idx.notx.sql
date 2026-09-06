-- migration:     0065_experts_managed_key_idx.notx.sql
-- description:   Stable uniqueness for insert-only managed expert seeds.
-- depends-on:    0064_db_backed_default_expert_columns.sql
-- transactional: no (CREATE INDEX CONCURRENTLY must run outside a transaction)

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_experts_managed_key ON experts (managed_key) WHERE managed_key IS NOT NULL;
