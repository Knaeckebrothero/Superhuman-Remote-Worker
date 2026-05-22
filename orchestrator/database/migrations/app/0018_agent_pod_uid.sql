-- migration:     0018_agent_pod_uid.sql
-- description:   Add pod_uid column to agents table. Used by the session
--                router (docs/features/direct_session_websockets.md) to set
--                K8s ownerReferences on per-session Service and Ingress
--                resources, so K8s GC cleans them up when the pod is deleted.
--
--                The value is self-reported by the agent at /api/agents/register;
--                agent_provisioner injects it via the Kubernetes downward API
--                (fieldRef metadata.uid) into the POD_UID env var.
--
--                Nullable because outside of K8s (local dev, tests) there is
--                no downward-API source and the column would be empty.
-- depends-on:    0017_jobs_cloud_diff.sql
-- expected:      < 50ms. Metadata-only column add; no table rewrite.
-- locks:         AccessExclusive on agents (fast, metadata-only).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE agents ADD COLUMN IF NOT EXISTS pod_uid TEXT;

COMMENT ON COLUMN agents.pod_uid IS
    'K8s-assigned metadata.uid of the agent pod, self-reported via the '
    'Kubernetes downward API. Used by the session router to set '
    'ownerReferences on per-session Service/Ingress resources so K8s GC '
    'tears them down when the pod is deleted. NULL outside of K8s.';

COMMIT;
