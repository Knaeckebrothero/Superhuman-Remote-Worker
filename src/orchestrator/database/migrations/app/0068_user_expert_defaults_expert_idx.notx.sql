-- migration:     0068_user_expert_defaults_expert_idx.notx.sql
-- description:   Reverse lookup for delete blockers on personal defaults.
-- depends-on:    0067_db_backed_default_expert_pointers.sql
-- transactional: no (CREATE INDEX CONCURRENTLY must run outside a transaction)

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_expert_defaults_expert ON user_expert_defaults (expert_id);
