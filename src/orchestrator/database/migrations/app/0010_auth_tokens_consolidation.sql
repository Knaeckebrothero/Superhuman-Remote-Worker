-- migration:     0010_auth_tokens_consolidation.sql
-- description:   Consolidate mcp_tokens + the dormant pre-KC auth_tokens
--                into a single auth_tokens table that handles both legacy
--                MCP tokens (kind='mcp') and the new Personal Access Tokens
--                for n8n / automation (kind='api').
--
--                PR 3 of docs/features/auth_bff_and_api_tokens.md.
--
--                The dormant pre-Keycloak `auth_tokens` (verification +
--                password_reset rows) is dropped first to free the name;
--                its DB-layer methods have zero callers and the table
--                hasn't been written to since the Keycloak cutover. We
--                then rename `mcp_tokens` to `auth_tokens` and bolt on:
--                  - `kind` ('mcp' for everything pre-existing, 'api' for
--                    new PATs). DEFAULT 'mcp' fills existing rows, then we
--                    drop the default so new rows must set kind explicitly.
--                  - `scopes TEXT[]` — PAT action-scopes (e.g.
--                    `{jobs:read,chat:write}`). Separate from the existing
--                    `scope TEXT` column which stays load-bearing for MCP
--                    rows ('user'/'all'/'project:<uuid>' semantics).
--                  - `last_four CHAR(4)` — last 4 chars of the plaintext
--                    token, shown in the UI as `ak_…vC2`. NULL for legacy
--                    MCP rows; new tokens (both kinds) write it.
--                  - `last_used_ip INET` — fed by the validator on each
--                    Bearer hit.
--                  - `superseded_by UUID` — rotate sets the old row's
--                    pointer to the new row; cleanup loop revokes the old
--                    after a 24h grace window.
--
--                Old `idx_mcp_tokens_*` indexes ride along the rename
--                automatically (Postgres renames them implicitly). Indexes
--                created here cover the new columns.
-- depends-on:    0009_srw_sessions.sql
-- expected:      < 1s. Two DROPs, one RENAME, ADD COLUMNs and indexes —
--                all metadata-only. Existing mcp_tokens rows (5-ish in
--                prod) get the kind='mcp' default applied; no row rewrite.
-- locks:         AccessExclusiveLock briefly on each ALTER. No concurrent
--                writers (token writes are sub-ms POST endpoints).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Free the `auth_tokens` name. The pre-KC verification/password-reset
-- table has zero live callers since Keycloak took over; its row shape
-- (email + token + token_type) is structurally unrelated to the consolidated
-- table we're about to create.
DROP TABLE IF EXISTS auth_tokens;

-- Promote mcp_tokens to the consolidated home. Constraints and the unique
-- index on token_hash come along automatically.
ALTER TABLE mcp_tokens RENAME TO auth_tokens;

-- Backfill `kind` for the existing rows (everything pre-migration came in
-- via the MCP flow). After the rewrite we drop the default so the API path
-- has to be explicit.
ALTER TABLE auth_tokens
    ADD COLUMN kind         TEXT NOT NULL DEFAULT 'mcp'
        CHECK (kind IN ('mcp', 'api')),
    ADD COLUMN scopes       TEXT[],
    ADD COLUMN last_four    CHAR(4),
    ADD COLUMN last_used_ip INET,
    ADD COLUMN superseded_by UUID REFERENCES auth_tokens(id) ON DELETE SET NULL;

ALTER TABLE auth_tokens
    ALTER COLUMN kind DROP DEFAULT;

-- Hot path for /api/api-keys + /api/mcp-tokens listings: "active tokens
-- for this user of this kind, newest first". Partial index keeps it small.
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_kind_active
    ON auth_tokens (user_id, kind, created_at DESC)
    WHERE revoked_at IS NULL;

-- Rotation grace cleanup needs to find "rows whose successor is N hours
-- old"; an index on superseded_by covers it cheaply.
CREATE INDEX IF NOT EXISTS idx_auth_tokens_superseded_by
    ON auth_tokens (superseded_by)
    WHERE superseded_by IS NOT NULL;

COMMENT ON TABLE auth_tokens IS
    'Bearer-auth tokens: kind=mcp (legacy Claude Code/CLI flow) and '
    'kind=api (PATs for n8n/automation). See docs/features/auth_bff_and_api_tokens.md §3.';
COMMENT ON COLUMN auth_tokens.kind IS
    'mcp=legacy MCP token (srw_<32-byte> prefix; scope column carries '
    'user/all/project:<uuid>). api=PAT (ak_<43-char> prefix; scopes column '
    'carries jobs:read / chat:write / etc).';
COMMENT ON COLUMN auth_tokens.scopes IS
    'Action-level scopes for kind=api rows. NULL for kind=mcp. Validator '
    'currently runs permissive — any scope grants any endpoint until PR 4 '
    'wires per-endpoint @require_scope decorators.';
COMMENT ON COLUMN auth_tokens.last_four IS
    'Last 4 chars of the plaintext token. Surfaced in UI as ak_…vC2 / '
    'srw_…vC2 hints. NULL on rows created before this migration.';
COMMENT ON COLUMN auth_tokens.superseded_by IS
    'Set by /rotate. Old row keeps validating for a 24h grace window so '
    'an automation can roll over without an outage; cleanup loop revokes.';

COMMIT;
