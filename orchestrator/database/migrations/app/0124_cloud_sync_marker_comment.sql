-- migration:     0124_cloud_sync_marker_comment.sql
-- description:   Keep the immutable 0123 baseline-column documentation
--                version-neutral as the resource marker protocol evolves.
-- depends-on:    0123_thread_cloud_sync_baselines.sql
-- expected:      < 1s; COMMENT only, no table rewrite.
-- locks:         ShareUpdateExclusiveLock on thread_cloud_sync_generations.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

COMMENT ON COLUMN thread_cloud_sync_generations.baseline_sha256 IS
    'SHA-256 of the canonical compact JSON baseline. The resource commit '
    'marker binds this digest so marker-write/DB-ack recovery can acknowledge '
    'without replaying the already committed delta.';

COMMIT;
