-- migration:     0016_project_network_tier.sql
-- description:   Add network_tier to projects. The tier controls which
--                CIDRs a workspace pod can reach at the pod-network layer
--                (per-project egress policy via the workspace NetworkPolicy
--                per-tier templating in helm). See
--                docs/features/workspace_network_isolation.md §3.
--
--                Tiers shipped in PR 3:
--                  - 'internet-only' (default): public internet only;
--                    cluster CIDRs + home LAN + link-local are denied.
--                  - 'home-allowed': adds the home LAN (192.168.178.0/24)
--                    to the allowlist; intended for operator/homelab
--                    projects that need to reach internal services on
--                    the same physical network.
--
--                The set of named tiers is closed (CHECK constraint).
--                Adding a tier later means: bump the CHECK, add an entry
--                under workspace.networkPolicy.tiers in helm values, and
--                make the new label value selectable in cockpit.
-- depends-on:    0015_create_automations.sql
-- expected:      < 50ms. Column add with constant default; no table
--                rewrite (NOT NULL DEFAULT 'internet-only' is metadata-only
--                in PG ≥ 11). New index on a single column.
-- locks:         AccessExclusive on projects for the ALTER TABLE (fast,
--                metadata-only). The CHECK constraint is added with NOT
--                VALID first, then validated separately so concurrent
--                writes are not blocked during the scan.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

DO $$ BEGIN
    ALTER TABLE projects
        ADD COLUMN network_tier TEXT NOT NULL DEFAULT 'internet-only';
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Closed set of tier names. New tiers require a follow-up migration to
-- widen this constraint, ensuring the helm template + cockpit selector
-- stay in lockstep with the database.
DO $$ BEGIN
    ALTER TABLE projects
        ADD CONSTRAINT projects_network_tier_check
        CHECK (network_tier IN ('internet-only', 'home-allowed')) NOT VALID;
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE projects VALIDATE CONSTRAINT projects_network_tier_check;
EXCEPTION WHEN undefined_object THEN null;
END $$;

CREATE INDEX IF NOT EXISTS idx_projects_network_tier
    ON projects (network_tier)
    WHERE network_tier <> 'internet-only';

COMMENT ON COLUMN projects.network_tier IS
    'Workspace pod-network egress tier. Controls which CIDRs the project''s '
    'workspace pods can reach. The orchestrator emits this value as the '
    'srw.io/network-tier pod label; the matching helm NetworkPolicy '
    '(one per tier) enforces the allowlist. See '
    'docs/features/workspace_network_isolation.md §3.';

COMMIT;
