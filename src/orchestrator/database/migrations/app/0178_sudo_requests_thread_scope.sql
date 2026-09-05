-- migration:     0178_sudo_requests_thread_scope.sql
-- description:   Let a sudo approval request belong to a session thread: VMs
--                provisioned for threads carry the thread uuid as their entity
--                id, and the NOT NULL job_id FK made every such request fail
--                closed with "internal error" (single-cluster VM plan, D10).
-- depends-on:    0177_managed_repository_thread_detach.sql
-- expected:      < 1s. Metadata-only: DROP NOT NULL, nullable ADD COLUMN, and
--                NOT VALID FK avoid a table scan or rewrite.
-- locks:         ACCESS EXCLUSIVE on sudo_approval_requests, briefly.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Thread VMs deliberately have no jobs row. The one-entity CHECK introduced
-- next preserves ownership while broadening this column contract.
-- squawk-ignore ban-drop-not-null
ALTER TABLE public.sudo_approval_requests ALTER COLUMN job_id DROP NOT NULL;

ALTER TABLE public.sudo_approval_requests
    ADD COLUMN thread_id uuid;

ALTER TABLE public.sudo_approval_requests
    ADD CONSTRAINT sudo_approval_requests_thread_id_fkey
    FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE
    NOT VALID;

COMMIT;
