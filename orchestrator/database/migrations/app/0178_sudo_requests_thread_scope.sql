-- migration:     0178_sudo_requests_thread_scope.sql
-- description:   Let a sudo approval request belong to a session thread: VMs
--                provisioned for threads carry the thread uuid as their entity
--                id, and the NOT NULL job_id FK made every such request fail
--                closed with "internal error" (single-cluster VM plan, D10).
-- depends-on:    0176_managed_repository_authorities.sql
-- expected:      < 1s. Metadata-only: DROP NOT NULL and ADD COLUMN with no
--                default rewrite nothing; the table holds a few thousand rows.
-- locks:         ACCESS EXCLUSIVE on sudo_approval_requests, briefly.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.sudo_approval_requests
    ALTER COLUMN job_id DROP NOT NULL,
    ADD COLUMN thread_id uuid REFERENCES public.threads(id) ON DELETE CASCADE;

COMMIT;
