-- migration:     0117_run_queue_affinity.sql
-- description:   Warm-pod affinity for the stateless lane
--                (docs/features/stateless_agents.md §5.3.4): remember which pod
--                last held each unit's lease, so the general claim can give
--                that pod a short head start on its own thread instead of
--                racing every idle pod. A cold winner pays a full re-attach
--                (measured 7.4s on k3d, 2026-08-08) that the warm holder skips
--                entirely.
-- depends-on:    0116_events_seq_hwm.sql
-- expected:      < 1s. ADD COLUMN with no default (catalog-only) + one index
--                on a table that holds one row per live unit.
-- locks:         Brief ACCESS EXCLUSIVE on run_queue for the ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Durable across the lease lifecycle, unlike leased_by (which is cleared on
-- completion/release/steal): this is the pod that MOST RECENTLY held the
-- unit, which is exactly the pod whose process may still hold the attached
-- session. Written by the claim; cleared only by the reaper (a stolen lease
-- means that pod is unreachable, so a head start would just add latency).
ALTER TABLE run_queue ADD COLUMN last_leased_by TEXT;

COMMENT ON COLUMN run_queue.last_leased_by IS
    'Pod that most recently claimed this unit (set by the claim, never '
    'cleared on completion — that is the point). Feeds the affinity grace in '
    'the general claim: for affinity_grace_seconds after a unit is queued, '
    'only this pod (or any pod, once the grace lapses) may claim it, so the '
    'holder of the warm in-process session wins its own re-claims instead of '
    'racing cold pods. Soft optimization ONLY — correctness never depends on '
    'it, and the grace is bounded so a dead holder delays a unit by at most '
    'that window. Cleared by the reaper on a steal.';

-- The claim's ORDER BY is unchanged; this index serves the affinity predicate
-- for the (common) case of a pod scanning for its own units first.
CREATE INDEX idx_run_queue_affinity
    ON run_queue (last_leased_by, queued_at)
    WHERE state = 'queued';

COMMIT;
