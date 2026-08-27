-- migration:     0052_cloud_ro_mounts.sql
-- description:   Per-mount read-only reader grants for protected cloud mode.
--                One row per protected session mount: the srw-reader-<user>
--                account, the serialized grant handle to revoke (NC group->
--                folder read ACL / OC Space Viewer permission id), and the
--                encrypted per-provision credential. The reconciler
--                (services/ro_reader_reconciler.py) sweeps status='active' rows
--                whose thread is gone and revokes the grant — never trust
--                revoke-on-teardown alone (design §8.1.4).
-- depends-on:    0001_initial.sql
-- expected:      < 1s (one empty CREATE TABLE on a fresh DB).
-- locks:         Brief ACCESS EXCLUSIVE (brand-new object).
-- transactional: yes
-- ============================================================================

CREATE TABLE IF NOT EXISTS cloud_ro_mounts (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id    UUID         NOT NULL,
    user_id      UUID         NOT NULL,
    backend      TEXT         NOT NULL,
    reader_id    TEXT         NOT NULL,
    grant_handle TEXT         NOT NULL,
    credentials  TEXT,                     -- encrypted app-password (NC); NULL for OC bearer
    webdav_url   TEXT         NOT NULL,
    auth_kind    TEXT         NOT NULL,
    status       TEXT         NOT NULL DEFAULT 'active',  -- active | revoked
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);

COMMENT ON TABLE cloud_ro_mounts IS
    'Per-mount read-only reader grants for protected cloud mode. One row per '
    'protected session mount; the reconciler revokes active grants whose thread '
    'is gone. Credentials are encrypted at rest (postgres._encrypt_optional).';

-- One live grant per thread; a re-provision upserts in place.
-- These indexes are built on a brand-new, empty table inside this transactional
-- migration: CONCURRENTLY cannot run in a transaction and buys nothing on zero
-- rows (docs/db_migration.md: "Add table/index -> direct CREATE IF NOT EXISTS").
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX IF NOT EXISTS cloud_ro_mounts_thread_idx ON cloud_ro_mounts (thread_id);
-- Reconciler sweep scans active rows.
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX IF NOT EXISTS cloud_ro_mounts_status_idx ON cloud_ro_mounts (status);
-- User-deletion GC.
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX IF NOT EXISTS cloud_ro_mounts_user_idx ON cloud_ro_mounts (user_id);
