-- migration:     0040_sudo_request_reply_subject_unique.sql
-- description:   Partial unique index on sudo_approval_requests.nats_reply_subject
--                — the insert-as-claim dedup slot for fan-out NATS sudo requests
--                (HA / M2 Layer 4,
--                docs/superpowers/specs/2026-06-28-orchestrator-m2-l4-nats-replica-safety-design.md).
--
--                NATS sudo requests fan out to BOTH orchestrator replicas (no
--                queue group), so on_sudo_request runs twice -> two approval
--                rows + two prompts + two NATS replies per request. The request
--                carries a unique NATS reply inbox, identical across replicas,
--                so it is the natural claim key: _insert_request now INSERTs
--                with ON CONFLICT DO NOTHING on this index and only the winner
--                proceeds.
--
--                Pre-existing duplicates (one extra per request created under
--                replicas:2) sharing a nats_reply_subject are collapsed first
--                (keep the lowest id) so the index can build. They are redundant
--                request-log artifacts; sudo_approval_requests has no inbound FK
--                references. NULL reply subjects (the vm_upgrade path) are
--                excluded by the partial predicate and stay unconstrained.
-- depends-on:    0039_drop_per_phase_account_model_defaults.sql
-- expected:      < 200ms. sudo_approval_requests is small + low-traffic.
-- locks:         Brief SHARE lock on sudo_approval_requests for the non-concurrent
--                index build (blocks writes for the build only); covered by
--                lock_timeout. Acceptable on a small table.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Collapse pre-existing duplicates sharing a reply subject, keeping the lowest
-- id, so the partial unique index can build.
DELETE FROM sudo_approval_requests a
USING sudo_approval_requests b
WHERE a.nats_reply_subject IS NOT NULL
  AND a.nats_reply_subject = b.nats_reply_subject
  AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sudo_request_reply_subject
    ON sudo_approval_requests (nats_reply_subject)
    WHERE nats_reply_subject IS NOT NULL;

COMMENT ON INDEX uq_sudo_request_reply_subject IS
    'At most one sudo_approval_requests row per NATS reply subject. The '
    'insert-as-claim dedup slot for fan-out NATS sudo requests (HA / M2-L4) — '
    'on_sudo_request claims it before acting so replicas:2 cannot double-insert '
    'or double-prompt. NULL reply subjects (vm_upgrade path) are unconstrained.';

COMMIT;
