-- migration:     0123_thread_cloud_sync_baselines.sql
-- description:   Durable content/ETag baseline for delta-only stateless cloud
--                generation commits. Prevents force-all recovery from
--                overwriting unrelated cloud-side edits.
-- depends-on:    0122_thread_cloud_sync_generations.sql
-- expected:      < 1s while the S2 tier gate keeps this table empty.
-- locks:         AccessExclusiveLock on thread_cloud_sync_generations.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE thread_cloud_sync_generations
    ADD COLUMN baseline_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN baseline_sha256 CHAR(64) NOT NULL
        DEFAULT '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
    ADD CONSTRAINT thread_cloud_sync_baseline_manifest_shape
        CHECK (
            jsonb_typeof(baseline_manifest) = 'object'
            AND octet_length(baseline_manifest::text) <= 4194304
        ),
    ADD CONSTRAINT thread_cloud_sync_baseline_digest_shape
        CHECK (baseline_sha256 ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN thread_cloud_sync_generations.baseline_manifest IS
    'Queue-fenced turn-start baseline keyed by mount-relative path. Each '
    'entry contains the SHA-256 of durable workspace bytes and the WebDAV '
    'ETag observed after pull. A successor compares against this baseline '
    'and replays only locally changed/new/deleted paths; it never force-PUTs '
    'untouched files over concurrent cloud edits.';

COMMENT ON COLUMN thread_cloud_sync_generations.baseline_sha256 IS
    'SHA-256 of the canonical compact JSON baseline. Resource marker v2 binds '
    'this digest so marker-write/DB-ack recovery can acknowledge without '
    'replaying the already committed delta.';

COMMENT ON COLUMN thread_cloud_sync_generations.mount_id IS
    'Stable logical cloud destination key derived from non-secret source/path '
    'identity; never the replace-on-edit thread_mounts row UUID. Legacy session '
    'folders use the reserved value legacy-session.';

COMMIT;
