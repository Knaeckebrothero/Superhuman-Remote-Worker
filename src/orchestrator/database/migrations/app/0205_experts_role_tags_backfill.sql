-- migration:     0205_experts_role_tags_backfill.sql
-- description:   Backfill the role tag on every DB-backed expert row: append
--                expert_type ('worker' | 'session') to experts.tags wherever
--                the array does not already carry it. U1 config unification
--                (B.4): a row's role is additive metadata in tags, so the tag
--                filters (GET /api/experts?type=X, the editor chips) never
--                hide a row for lacking it. The write side already normalises
--                new and edited rows through
--                src/core/expert_resolution.py::with_role_tag (WP4); this is
--                the one-shot catch-up for rows that predate it, with the same
--                semantics: append the role once, at the end, existing entries
--                untouched and in their original order, never a duplicate.
--                Deliberately narrower than the Python side otherwise —
--                pre-existing entries are not trimmed or de-duplicated here;
--                the write side does that on the row's next save. expert_type
--                stays the primary role (its CHECK constraint is unchanged);
--                tags is TEXT[] NOT NULL DEFAULT '{}' (0028), so no COALESCE
--                is needed. version / updated_at are left alone on purpose:
--                this is metadata catch-up, not an edit, so the cockpit's
--                optimistic-concurrency check and the managed rows'
--                seed_version comparison see nothing. Data only — no DDL, no
--                index — so schema_current.sql is unchanged.
-- depends-on:    0028_experts.sql, 0064_db_backed_default_expert_columns.sql
-- expected:      < 1s. One pass over public.experts, tens of rows (every
--                user/admin expert plus the managed seed rows). Idempotent:
--                a backfilled row no longer matches the WHERE guard, so a
--                re-run touches nothing.
-- locks:         ROW EXCLUSIVE on public.experts for the UPDATE plus row
--                locks on the matched rows only. No table rewrite.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

UPDATE public.experts
   SET tags = array_append(tags, expert_type::text)
 WHERE NOT (expert_type::text = ANY (tags));

COMMIT;
