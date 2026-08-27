-- migration:     0059_docker_workspace_leases.sql
-- description:   Durable, endpoint-keyed authority for the static Docker
--                workspace pool. Owner JSONB remains a context mirror; it is
--                deliberately not the occupancy authority because owner rows
--                can be permanently deleted while a host remains quarantined.
-- depends-on:    0058_canvases.sql
-- expected:      < 5s (new table plus a bounded legacy JSONB backfill)
-- locks:         Brief reads of jobs/threads; ACCESS EXCLUSIVE on new table only
-- transactional: yes
-- ============================================================================

CREATE TABLE IF NOT EXISTS docker_workspace_leases (
    host                 TEXT        NOT NULL,
    port                 INTEGER     NOT NULL,
    status               TEXT        NOT NULL,
    lease_id             UUID,
    owner_kind           TEXT,
    owner_id             UUID,
    trust_mode           TEXT        NOT NULL DEFAULT 'unattested',
    host_key_fingerprint TEXT,
    quarantine_reason    TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (host, port),
    CONSTRAINT ck_docker_workspace_lease_host
        CHECK (
            host = btrim(host)
            AND host <> ''
            AND char_length(host) <= 255
            AND host !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_docker_workspace_lease_port
        CHECK (port BETWEEN 1 AND 65535),
    CONSTRAINT ck_docker_workspace_lease_status
        CHECK (status IN ('ready', 'releasing', 'released', 'quarantined')),
    CONSTRAINT ck_docker_workspace_lease_owner_kind
        CHECK (owner_kind IS NULL OR owner_kind IN ('job', 'thread')),
    CONSTRAINT ck_docker_workspace_lease_owner_pair
        CHECK ((owner_kind IS NULL) = (owner_id IS NULL)),
    CONSTRAINT ck_docker_workspace_live_lease_shape
        CHECK (
            status NOT IN ('ready', 'releasing')
            OR (owner_kind IS NOT NULL AND owner_id IS NOT NULL AND lease_id IS NOT NULL)
        ),
    CONSTRAINT ck_docker_workspace_lease_trust_mode
        CHECK (trust_mode IN ('unattested', 'trusted_dev', 'attested')),
    CONSTRAINT ck_docker_workspace_lease_fingerprint
        CHECK (
            host_key_fingerprint IS NULL
            OR (
                host_key_fingerprint LIKE 'SHA256:%'
                AND char_length(host_key_fingerprint) <= 128
                AND host_key_fingerprint !~ '[[:space:]]'
            )
        ),
    CONSTRAINT ck_docker_workspace_attested_fingerprint
        CHECK (trust_mode <> 'attested' OR host_key_fingerprint IS NOT NULL)
);

-- An owner may accumulate quarantined/released audit rows, but it may have at
-- most one live execution lease. The endpoint PK is the cross-owner exclusion
-- boundary.
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX IF NOT EXISTS uq_docker_workspace_active_owner
    ON docker_workspace_leases (owner_kind, owner_id)
    WHERE owner_id IS NOT NULL AND status IN ('ready', 'releasing');

-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX IF NOT EXISTS uq_docker_workspace_lease_id
    ON docker_workspace_leases (lease_id)
    WHERE lease_id IS NOT NULL;

-- Existing JSONB rows predate a durable recreation attestation. Even a legacy
-- `released` marker is not proof that both the container and its persistent
-- home/system volume were recreated, so every discovered endpoint is imported
-- quarantined. Duplicate/conflicting owners collapse to the same endpoint PK
-- and remain unavailable until an explicit controller/bootstrap attests a
-- clean runtime. There is intentionally no FK to jobs/threads: quarantine must
-- survive permanent owner deletion.
WITH legacy AS (
    SELECT
        context->'workspace_container'->>'host' AS host,
        context->'workspace_container'->>'port' AS raw_port
    FROM jobs
    WHERE context->'workspace_container'->>'provisioner' = 'docker'

    UNION ALL

    SELECT
        metadata->'workspace_container'->>'host' AS host,
        metadata->'workspace_container'->>'port' AS raw_port
    FROM threads
    WHERE metadata->'workspace_container'->>'provisioner' = 'docker'
), normalized AS (
    SELECT
        btrim(host) AS host,
        CASE
            WHEN raw_port ~ '^[0-9]{1,5}$'
                 AND raw_port::INTEGER BETWEEN 1 AND 65535
                THEN raw_port::INTEGER
            ELSE 22
        END AS port,
        count(*) AS legacy_rows
    FROM legacy
    WHERE host IS NOT NULL
      AND btrim(host) <> ''
      AND char_length(btrim(host)) <= 255
      AND btrim(host) !~ '[[:cntrl:]]'
    GROUP BY btrim(host),
        CASE
            WHEN raw_port ~ '^[0-9]{1,5}$'
                 AND raw_port::INTEGER BETWEEN 1 AND 65535
                THEN raw_port::INTEGER
            ELSE 22
        END
)
INSERT INTO docker_workspace_leases (
    host,
    port,
    status,
    trust_mode,
    quarantine_reason
)
SELECT
    host,
    port,
    'quarantined',
    'unattested',
    CASE
        WHEN legacy_rows > 1 THEN 'legacy_conflicting_owners'
        ELSE 'legacy_recreation_attestation_required'
    END
FROM normalized
ON CONFLICT (host, port) DO NOTHING;

COMMENT ON TABLE docker_workspace_leases IS
    'Durable endpoint authority for pre-provisioned Docker workspaces. No owner FK by design: quarantine survives deleted jobs/threads.';
COMMENT ON COLUMN docker_workspace_leases.status IS
    'Only released inventory may be allocated; ready/releasing/quarantined remains occupied even after owner deletion.';
COMMENT ON COLUMN docker_workspace_leases.trust_mode IS
    'unattested, explicit same-trust trusted_dev, or controller/bootstrap attested. Existing rows are never promoted by configuration alone.';
COMMENT ON COLUMN docker_workspace_leases.host_key_fingerprint IS
    'Exact provisioner-attested Ed25519 SHA-256 identity for attested inventory; public-key metadata, never a private key.';
COMMENT ON COLUMN docker_workspace_leases.quarantine_reason IS
    'Operator-visible recovery reason. First discovery without explicit bootstrap attestation is permanent quarantine until a controller/manual recreation attests the endpoint.';
