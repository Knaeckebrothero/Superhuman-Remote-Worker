-- migration:     0045_drop_capability_grant_audit.sql
-- description:   Drop the write-only capability_grant_audit table (created by
--                0030). set_grant/delete_grant inserted a row per grant
--                change, but nothing ever read it — no SELECT, endpoint, or
--                UI. The INSERTs and the audit-only reason/actor plumbing
--                (DB methods, GrantSet.reason, cockpit "Reason" field) were
--                removed in the same change. capability_grants itself (live
--                grant state) is unaffected. Re-add as a read-backed feature
--                if a compliance ask materializes. QW-6 / gate G1, decided
--                2026-07-02 — docs/features/database_roadmap.md.
-- depends-on:    0030_capability_grants.sql
-- expected:      < 1s.
-- locks:         ACCESS EXCLUSIVE on capability_grant_audit only (drops its
--                idx_grant_audit_scope index with it).
-- transactional: yes

DROP TABLE IF EXISTS capability_grant_audit;
