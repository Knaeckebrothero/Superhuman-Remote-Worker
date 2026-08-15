-- migration:     0161_runtime_actor_credentials.sql
-- description:   Opaque, runtime-bound actor grants for officer message
--                actions and sensitive knowledge writes (OC-02 / BP-09).
--                Access and refresh material is stored only as SHA-256
--                digests. Dedicated session pods exchange a unique
--                provision-time bootstrap; fleet-wide MCP transport identity
--                is never accepted as actor identity.
-- depends-on:    0160_officer_ticket_claim_uniqueness.notx.sql
-- expected:      < 1s (three empty tables and expiry indexes).
-- locks:         catalog locks only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE public.runtime_actor_grants (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    refresh_token_hash   BYTEA NOT NULL UNIQUE,
    caller_kind          TEXT NOT NULL,
    user_id              UUID REFERENCES public.users(id) ON DELETE SET NULL,
    project_id           UUID REFERENCES public.projects(id) ON DELETE CASCADE,
    project_role         TEXT,
    thread_id            UUID REFERENCES public.threads(id) ON DELETE CASCADE,
    officer_incarnation  INTEGER,
    refresh_expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_refreshed_at    TIMESTAMPTZ,
    CONSTRAINT runtime_actor_grants_kind_check
        CHECK (caller_kind IN ('worker', 'human', 'conference', 'officer')),
    CONSTRAINT runtime_actor_grants_role_check
        CHECK (project_role IS NULL OR project_role IN
               ('admin', 'owner', 'editor', 'viewer')),
    CONSTRAINT runtime_actor_grants_incarnation_check
        CHECK (officer_incarnation IS NULL OR officer_incarnation >= 0),
    CONSTRAINT runtime_actor_grants_officer_shape_check
        CHECK (caller_kind != 'officer' OR
               (project_id IS NOT NULL AND thread_id IS NOT NULL AND
                officer_incarnation IS NOT NULL))
);

CREATE TABLE public.runtime_actor_access_tokens (
    token_hash     BYTEA PRIMARY KEY,
    grant_id       UUID NOT NULL REFERENCES public.runtime_actor_grants(id)
                       ON DELETE CASCADE,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);

CREATE TABLE public.runtime_actor_bootstraps (
    token_hash     BYTEA PRIMARY KEY,
    thread_id      UUID NOT NULL REFERENCES public.threads(id) ON DELETE CASCADE,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);

CREATE INDEX idx_runtime_actor_grants_refresh_expiry
    ON public.runtime_actor_grants (refresh_expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_runtime_actor_access_expiry
    ON public.runtime_actor_access_tokens (expires_at);
CREATE INDEX idx_runtime_actor_bootstrap_expiry
    ON public.runtime_actor_bootstraps (expires_at);

COMMENT ON TABLE public.runtime_actor_grants IS
    'Server-derived runtime identities. Opaque refresh credentials are hashed; '
    'every authorization re-checks current post/membership state.';
COMMENT ON TABLE public.runtime_actor_access_tokens IS
    'Short-lived opaque access credentials for runtime actor authorization.';
COMMENT ON TABLE public.runtime_actor_bootstraps IS
    'Short-lived per-pod bootstrap credentials used only by dedicated session '
    'registration; never shared with stateless workers.';

COMMIT;
