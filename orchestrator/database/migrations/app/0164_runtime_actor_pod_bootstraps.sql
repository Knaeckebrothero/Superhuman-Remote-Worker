-- migration:     0164_runtime_actor_pod_bootstraps.sql
-- description:   Allow a thread-less, pod-scoped runtime actor bootstrap so a
--                warm pool agent can prove pod possession for a session that
--                did not exist when the pod was provisioned.
--                docs/issues/pool_attach_has_no_runtime_actor_identity.md
-- depends-on:    0161_runtime_actor_credentials.sql
-- expected:      milliseconds. One DROP NOT NULL on a small, short-TTL table.
-- locks:         ACCESS EXCLUSIVE on runtime_actor_bootstraps for the catalog
--                update only; no table rewrite (DROP NOT NULL is metadata).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- 0161 bound every bootstrap to one thread because the only issuer was a
-- provisioner creating a pod *for* that thread. A warm pool agent is
-- provisioned before any session exists, so it cannot hold a thread-bound
-- secret; K8s env is not patchable on a running pod. Making thread_id
-- nullable lets the same table carry both shapes:
--
--   thread_id NOT NULL -> dedicated session pod, bound at provision time.
--   thread_id NULL     -> pod-scoped, bound to a thread at attach time by
--                         `exchange_runtime_actor_pod_bootstrap`, which
--                         reads the binding from `agents.thread_id` rather
--                         than trusting the caller's claim.
--
-- The 0161 property is preserved either way: the pod proves possession of a
-- unique secret injected into that pod, and fleet-wide transport identity is
-- still never sufficient.
ALTER TABLE public.runtime_actor_bootstraps
    ALTER COLUMN thread_id DROP NOT NULL;

COMMENT ON COLUMN public.runtime_actor_bootstraps.thread_id IS
    'Session this bootstrap may be exchanged for. NULL means pod-scoped: the '
    'holder is a warm pool agent and the thread is resolved at attach time '
    'from the durable agents.thread_id binding, never from the caller.';

COMMIT;
