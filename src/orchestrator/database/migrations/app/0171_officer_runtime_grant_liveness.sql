-- migration:     0171_officer_runtime_grant_liveness.sql
-- description:   Bind long-lived Officer runtime grants to the authoritative
--                persistent agent and add acknowledged refresh rotation.
--                Sleeping Officers are renewed by the server watchdog. The
--                exact current live Officer grant may also be recovered by
--                that watchdog, but its bearer is forced to rotate on the
--                next credential refresh. Workers retain the fixed 0161
--                lifetime contract.
-- depends-on:    0170_project_status_validate.sql
-- expected:      < 1s on the small runtime-grant ledger. Columns are nullable
--                or constant-default metadata additions; indexes are bounded
--                by the number of live/retained grants.
-- locks:         ACCESS EXCLUSIVE for the catalog changes and index creation.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.runtime_actor_grants
    -- This is deliberately a provenance snapshot, not an agents FK. Agent
    -- rows are operational/GC state while grants are retained audit state.
    -- The agent-delete trigger below revokes live authority before the
    -- operational row disappears and leaves this UUID intact.
    ADD COLUMN agent_id UUID,
    ADD COLUMN credential_generation BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN previous_refresh_token_hash BYTEA,
    ADD COLUMN previous_refresh_valid_until TIMESTAMPTZ,
    ADD COLUMN refresh_handoff_ciphertext TEXT,
    ADD COLUMN refresh_handoff_acknowledged_at TIMESTAMPTZ,
    ADD COLUMN last_maintenance_at TIMESTAMPTZ,
    ADD COLUMN refresh_rotation_required BOOLEAN NOT NULL DEFAULT FALSE,
    ADD CONSTRAINT runtime_actor_grants_generation_check
        CHECK (credential_generation > 0),
    ADD CONSTRAINT runtime_actor_grants_previous_refresh_shape_check
        CHECK ((previous_refresh_token_hash IS NULL) =
               (previous_refresh_valid_until IS NULL)
           AND (previous_refresh_token_hash IS NULL) =
               (refresh_handoff_ciphertext IS NULL)
           AND (refresh_handoff_acknowledged_at IS NULL OR
                previous_refresh_token_hash IS NOT NULL));

CREATE UNIQUE INDEX uq_runtime_actor_grants_previous_refresh_hash
    ON public.runtime_actor_grants (previous_refresh_token_hash)
    WHERE previous_refresh_token_hash IS NOT NULL;

CREATE INDEX idx_runtime_actor_grants_officer_binding
    ON public.runtime_actor_grants
       (project_id, thread_id, officer_incarnation, created_at DESC, id DESC)
    WHERE caller_kind = 'officer' AND revoked_at IS NULL;

CREATE UNIQUE INDEX uq_runtime_actor_grants_live_officer_agent
    ON public.runtime_actor_grants (agent_id)
    WHERE caller_kind = 'officer'
      AND revoked_at IS NULL
      AND agent_id IS NOT NULL;

CREATE FUNCTION public.enforce_officer_runtime_agent_binding()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.caller_kind = 'officer'
       AND OLD.agent_id IS NOT NULL
       AND NEW.agent_id IS DISTINCT FROM OLD.agent_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_agent_provenance',
            MESSAGE = 'Officer runtime grant agent provenance is immutable';
    END IF;
    IF NEW.caller_kind = 'officer'
       AND NEW.agent_id IS NULL
       AND NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_officer_agent_binding',
            MESSAGE = 'Officer runtime grants require an authoritative agent binding',
            HINT = 'Drain pre-0171 orchestrator replicas before serving Officer attach or refresh traffic.';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.caller_kind = 'officer'
       AND OLD.refresh_rotation_required
       AND NEW.refresh_rotation_required
       AND NEW.last_refreshed_at IS DISTINCT FROM OLD.last_refreshed_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_recovery_rotation',
            MESSAGE = 'Recovered Officer runtime grants require refresh rotation',
            HINT = 'Drain pre-0171 orchestrator replicas before serving Officer refresh traffic.';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.caller_kind = 'officer'
       AND OLD.refresh_handoff_ciphertext IS NOT NULL
       AND NEW.last_refreshed_at IS DISTINCT FROM OLD.last_refreshed_at
       AND NEW.last_maintenance_at IS NOT DISTINCT FROM OLD.last_maintenance_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_rotation_acknowledgement',
            MESSAGE = 'Officer refresh handoffs require acknowledged rotation semantics',
            HINT = 'Drain pre-0171 orchestrator replicas before serving Officer refresh traffic.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_runtime_actor_grants_officer_agent_binding
BEFORE INSERT OR UPDATE ON public.runtime_actor_grants
FOR EACH ROW EXECUTE FUNCTION public.enforce_officer_runtime_agent_binding();

CREATE FUNCTION public.revoke_runtime_actor_grants_on_agent_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Preserve the UUID snapshot while making the deleted runtime's access
    -- and refresh authority unusable even to a pre-0171 application replica.
    UPDATE public.runtime_actor_grants
       SET revoked_at = COALESCE(revoked_at, statement_timestamp())
     WHERE caller_kind = 'officer'
       AND agent_id = OLD.id
       AND revoked_at IS NULL;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_agents_revoke_runtime_actor_grants
BEFORE DELETE ON public.agents
FOR EACH ROW EXECUTE FUNCTION public.revoke_runtime_actor_grants_on_agent_delete();

COMMENT ON COLUMN public.runtime_actor_grants.agent_id IS
    'Immutable authoritative persistent-agent UUID snapshot for Officer '
    'grants. It intentionally has no agents FK so revoked-grant audit '
    'provenance survives agent deletion. NULL is only a pre-0171 grant '
    'awaiting unambiguous current-incarnation adoption.';
COMMENT ON COLUMN public.runtime_actor_grants.credential_generation IS
    'Server-owned refresh rotation generation. It is not exposed in model '
    'schemas or audit payloads.';
COMMENT ON COLUMN public.runtime_actor_grants.previous_refresh_token_hash IS
    'Predecessor refresh digest used only to re-deliver one encrypted, '
    'unacknowledged rotation or during its bounded acknowledged overlap.';
COMMENT ON COLUMN public.runtime_actor_grants.refresh_handoff_ciphertext IS
    'APP_ENCRYPTION_KEY-protected current refresh bearer retained only for '
    'idempotent ambiguous-response recovery; plaintext is never persisted.';
COMMENT ON COLUMN public.runtime_actor_grants.refresh_handoff_acknowledged_at IS
    'First presentation of the rotated current bearer. This acknowledgement '
    'starts the bounded predecessor overlap.';
COMMENT ON COLUMN public.runtime_actor_grants.last_maintenance_at IS
    'Last server watchdog or credential-bearing maintenance of this grant.';
COMMENT ON COLUMN public.runtime_actor_grants.refresh_rotation_required IS
    'Server-only handoff fence set when the watchdog recovers an expired '
    'current Officer grant. The next refresh rotates the bearer and clears it.';
COMMENT ON FUNCTION public.enforce_officer_runtime_agent_binding() IS
    '0171 mixed-version fence: old replicas fail safely instead of minting or '
    'refreshing an unbound Officer authority, rewriting agent provenance, or '
    'bypassing acknowledged recovery rotation.';
COMMENT ON FUNCTION public.revoke_runtime_actor_grants_on_agent_delete() IS
    'Revokes live Officer authority before operational agent deletion while '
    'retaining the immutable agent UUID snapshot for grant audit provenance.';

COMMIT;
