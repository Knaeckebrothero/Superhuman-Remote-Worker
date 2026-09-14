-- migration:     0232_thread_cloud_sync_push_ownership.sql
-- description:   Commit-then-effects for stateless turns
--                (knowledge-base/knowledge/features/stateless_turn_resilience.md
--                step 4a). The turn-end cloud push used to run INSIDE the
--                run_queue critical section because its only write fence was
--                the queue lease itself: the slot was held for the whole push
--                (88–574 s measured) against a 10–20 s model call, and a
--                shutdown that cut the push parked an already-answered turn
--                (dev thread ad7eb761). These columns give the push its own
--                fence and its own durable progress so the unit can complete
--                the moment the answer is durable and the push continues —
--                or is adopted by the thread's next claim — off-slot:
--                `push_owner_token` is bumped by every hand-off/adoption and
--                checked before every remote write (a stale owner fails the
--                fence; conditional WebDAV writes are the hard stop for the
--                one PUT already in flight); `push_progress` records each
--                landed file so a successor resumes instead of re-uploading;
--                `push_heartbeat_at` tells an operator whether the owner is
--                alive; the remaining columns are diagnostics. All nullable
--                or constant-default: catalog-only on PG 11+.
-- depends-on:    0123_thread_cloud_sync_baselines.sql
-- expected:      < 1s. Eight ADD COLUMNs (no rewrite) on a table holding one
--                row per (thread, mount).
-- locks:         Brief ACCESS EXCLUSIVE on thread_cloud_sync_generations.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '60s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.thread_cloud_sync_generations
    ADD COLUMN IF NOT EXISTS push_owner_token   BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS push_owner_pod     TEXT,
    ADD COLUMN IF NOT EXISTS push_owner_pod_uid TEXT,
    ADD COLUMN IF NOT EXISTS push_heartbeat_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS push_progress      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS push_started_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS push_failed_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS push_error         TEXT;

-- No CHECK constraint on push_progress: its object shape and the 4 MiB size
-- cap are enforced by shared.cloud_sync_generations.normalize_push_progress on
-- every write and read (a NOT VALID + VALIDATE pair would need two
-- transactions for no protection the code does not already give).

COMMENT ON COLUMN public.thread_cloud_sync_generations.push_owner_token IS
    'Fencing token for the turn-end push once the run_queue unit has '
    'completed. Bumped (max over the thread + 1) by every hand-off and by '
    'every adoption; the writer re-checks it before each remote write, so a '
    'stale owner stops at its next write. 0 = no push has ever been handed off.';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_owner_pod IS
    'Pod that currently owns the pending push (diagnostics).';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_owner_pod_uid IS
    'UID of that pod, so a same-named replacement cannot be mistaken for it.';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_heartbeat_at IS
    'Renewed by the push owner while transmitting; stale (> 90 s) means the '
    'owner died and the pending generation waits for adoption by the next claim.';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_progress IS
    'Durable per-file progress of the PENDING generation: '
    '{"planned": N, "files": {path: {sha256, size, remote_etag, state}}} '
    'where state is uploaded | deleted. A successor seeds its delta from these '
    'entries and re-transmits only what is missing. Reset when a new '
    'generation is armed.';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_started_at IS
    'When the pending push was first handed off (diagnostics).';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_failed_at IS
    'When the current owner last reported a terminal push failure; success '
    'is still acknowledged_generation + the cloud-side marker.';
COMMENT ON COLUMN public.thread_cloud_sync_generations.push_error IS
    'The failure message that goes with push_failed_at (bounded).';

COMMIT;
