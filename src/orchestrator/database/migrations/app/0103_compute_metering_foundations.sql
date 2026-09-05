-- migration:     0103_compute_metering_foundations.sql
-- description:   Slice 3 compute activation, non-publishable shadow evidence,
--                and durable agent Pod identity/binding convergence.
-- depends-on:    0102_storage_asset_foundations.sql
-- expected:      < 30s. New tables are empty; the agent-state backfill is one
--                bounded pass over the small agents table. Existing-table
--                locks are limited to trigger installation.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on resource_intervals,
--                agents, jobs, and threads while triggers are installed.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Agent Pods, on-demand IDE Pods, and VM compute may be introduced after the
-- one-way workspace cutover. Each product class therefore gets an independent
-- forward-only boundary. Shadow observations can never become publishable
-- merely because an older resource class is already active.
CREATE TABLE compute_metering_activation (
    activation_key TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'disabled',
    activated_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT compute_metering_activation_key_check CHECK (
        activation_key IN (
            'agent_pod', 'ide_workspace_pod', 'workspace_vm'
        )
    ),
    CONSTRAINT compute_metering_activation_state_check CHECK (
        (state IN ('disabled', 'shadow') AND activated_at IS NULL)
        OR (state = 'active' AND activated_at IS NOT NULL
            AND activated_at = date_trunc('day', activated_at, 'UTC'))
    )
);

INSERT INTO compute_metering_activation (activation_key)
VALUES ('agent_pod'), ('ide_workspace_pod'), ('workspace_vm');

CREATE FUNCTION protect_compute_metering_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compute activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'compute activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.activation_key IS DISTINCT FROM OLD.activation_key
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'compute activation identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'disabled' AND NEW.state = 'shadow'
       AND NEW.activated_at IS NULL THEN
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.state = 'shadow' AND NEW.state = 'active'
       AND NEW.activated_at IS NOT NULL
       AND NEW.activated_at = date_trunc('day', NEW.activated_at, 'UTC')
       AND NEW.activated_at > statement_timestamp() THEN
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state
       AND NEW.activated_at IS NOT DISTINCT FROM OLD.activated_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'compute activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER compute_metering_activation_one_way
BEFORE INSERT OR UPDATE OR DELETE ON compute_metering_activation
FOR EACH ROW EXECUTE FUNCTION protect_compute_metering_activation();

-- Database backstop against shadow-history back-billing. Ordinary Slice 1
-- workspace Pods retain their existing cutover semantics. Only IDE rows that
-- explicitly carry details.product_class=ide-session use the new IDE boundary.
CREATE FUNCTION enforce_resource_interval_compute_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    required_key     TEXT;
    activation_state TEXT;
    activation_time  TIMESTAMPTZ;
BEGIN
    IF NEW.source_kind = 'pod'
       AND NEW.category = 'compute'
       AND NEW.resource = 'agent_pod' THEN
        required_key := 'agent_pod';
    ELSIF NEW.source_kind = 'pod'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_pod'
          AND NEW.details->>'product_class' = 'ide-session' THEN
        required_key := 'ide_workspace_pod';
    ELSIF NEW.source_kind = 'vmi'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_vm' THEN
        required_key := 'workspace_vm';
    ELSE
        RETURN NEW;
    END IF;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_time
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = required_key
    FOR SHARE;

    IF activation_state IS DISTINCT FROM 'active'
       OR activation_time IS NULL
       OR statement_timestamp() < activation_time
       OR NEW.started_at < activation_time THEN
        RAISE EXCEPTION
            'compute product class % is not active at its clamped boundary',
            required_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_compute_activation_guard
BEFORE INSERT ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_resource_interval_compute_activation();

-- One row per inventory item and activation key. Adapters write explicit
-- not-applicable rows, making count equality an item-for-item shadow proof.
-- There is deliberately no resource_interval/publication-plan relationship.
CREATE TABLE compute_shadow_observations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    activation_key      TEXT NOT NULL
        REFERENCES compute_metering_activation(activation_key)
        ON DELETE RESTRICT,
    snapshot_id         UUID NOT NULL,
    inventory_scope_id  UUID NOT NULL,
    source_kind         TEXT NOT NULL,
    source_uid          TEXT NOT NULL,
    resource            TEXT NOT NULL,
    product_class       TEXT NOT NULL,
    cpu_millicores      BIGINT,
    memory_bytes        BIGINT,
    attribution_scope   TEXT NOT NULL,
    owner_kind          TEXT,
    owner_id            UUID,
    user_id             UUID,
    project_id          UUID,
    disposition         TEXT NOT NULL,
    reason_code         TEXT NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT compute_shadow_observations_snapshot_fkey
        FOREIGN KEY (snapshot_id, inventory_scope_id)
        REFERENCES resource_inventory_snapshots(id, inventory_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT compute_shadow_observations_item_uq
        UNIQUE (snapshot_id, activation_key, source_kind, source_uid),
    CONSTRAINT compute_shadow_observations_identity_check CHECK (
        source_uid <> '' AND length(source_uid) <= 256
        AND resource <> '' AND length(resource) <= 128
        AND product_class ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND ((activation_key = 'agent_pod'
                AND source_kind = 'pod'
                AND resource = 'agent_pod')
             OR (activation_key = 'ide_workspace_pod'
                AND source_kind = 'pod'
                AND resource = 'workspace_pod'
                AND product_class = 'ide-session')
             OR (activation_key = 'workspace_vm'
                AND source_kind = 'vmi'
                AND resource = 'workspace_vm'))
    ),
    CONSTRAINT compute_shadow_observations_capacity_check CHECK (
        (cpu_millicores IS NULL OR cpu_millicores >= 0)
        AND (memory_bytes IS NULL OR memory_bytes >= 0)
        AND ((disposition = 'eligible-unpriced'
                AND cpu_millicores IS NOT NULL AND memory_bytes IS NOT NULL)
             OR (disposition <> 'eligible-unpriced'))
    ),
    CONSTRAINT compute_shadow_observations_attribution_check CHECK (
        attribution_scope IN ('customer', 'shared-platform', 'unknown')
        AND ((attribution_scope = 'customer'
                AND owner_kind IN ('job', 'thread')
                AND owner_id IS NOT NULL AND user_id IS NOT NULL)
             OR (attribution_scope = 'shared-platform'
                AND owner_kind = 'platform' AND owner_id IS NULL
                AND user_id IS NULL AND project_id IS NULL)
             OR (attribution_scope = 'unknown'
                AND owner_kind IS NULL AND owner_id IS NULL
                AND user_id IS NULL AND project_id IS NULL))
        AND disposition IN (
            'eligible-unpriced', 'not-applicable', 'invalid',
            'identity-ambiguous'
        )
    )
);

CREATE INDEX compute_shadow_observations_latest_idx
    ON compute_shadow_observations (
        activation_key, inventory_scope_id, source_kind,
        source_uid, observed_at DESC
    );
CREATE INDEX compute_shadow_observations_retention_idx
    ON compute_shadow_observations (observed_at, id);

CREATE FUNCTION protect_compute_shadow_observation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_state   TEXT;
    activation_state TEXT;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'compute shadow observations are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.manifest_state
    INTO snapshot_state
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_ingest_tickets AS ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control AS control ON control.singleton = TRUE
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    SELECT activation.state
    INTO activation_state
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE;

    IF snapshot_state IS DISTINCT FROM 'staging'
       OR activation_state NOT IN ('shadow', 'active') THEN
        RAISE EXCEPTION 'compute shadow observation fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER compute_shadow_observations_immutable
BEFORE INSERT OR UPDATE OR DELETE ON compute_shadow_observations
FOR EACH ROW EXECUTE FUNCTION protect_compute_shadow_observation_mutation();

-- Mutable current head for each agent record. A missing agent is retained as a
-- tombstone so deletion is itself a durable transition. pod_uid is an observed
-- hint, not trusted identity: missing and duplicate values remain explicit.
CREATE TABLE agent_metering_pod_identity_state (
    agent_id             UUID PRIMARY KEY,
    agent_present        BOOLEAN NOT NULL,
    pod_uid              TEXT,
    hostname             TEXT,
    identity_state       TEXT NOT NULL,
    attribution_scope    TEXT NOT NULL,
    owner_kind           TEXT,
    owner_id             UUID,
    user_id              UUID,
    project_id           UUID,
    reason_code          TEXT NOT NULL,
    transition_source    TEXT NOT NULL,
    revision             BIGINT NOT NULL,
    effective_at         TIMESTAMPTZ NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT agent_metering_pod_identity_state_identity_check CHECK (
        revision > 0
        AND (pod_uid IS NULL OR (pod_uid <> '' AND length(pod_uid) <= 256))
        AND (hostname IS NULL OR (hostname <> '' AND length(hostname) <= 255))
        AND identity_state IN ('valid', 'missing', 'duplicate')
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND transition_source ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND ((agent_present AND identity_state IN ('valid', 'duplicate')
                AND pod_uid IS NOT NULL)
             OR (agent_present AND identity_state = 'missing'
                AND pod_uid IS NULL)
             OR (NOT agent_present AND identity_state = 'missing'))
    ),
    CONSTRAINT agent_metering_pod_identity_state_attribution_check CHECK (
        attribution_scope IN ('customer', 'shared-platform', 'unknown')
        AND ((attribution_scope = 'customer'
                AND agent_present AND identity_state = 'valid'
                AND owner_kind IN ('job', 'thread')
                AND owner_id IS NOT NULL AND user_id IS NOT NULL)
             OR (attribution_scope = 'shared-platform'
                AND agent_present AND identity_state = 'valid'
                AND owner_kind IS NULL AND owner_id IS NULL
                AND user_id IS NULL AND project_id IS NULL)
             OR (attribution_scope = 'unknown'
                AND owner_kind IS NULL AND owner_id IS NULL
                AND user_id IS NULL AND project_id IS NULL))
    )
);

CREATE INDEX agent_metering_pod_identity_state_pod_uid_idx
    ON agent_metering_pod_identity_state (pod_uid, agent_id)
    WHERE agent_present AND pod_uid IS NOT NULL;
CREATE INDEX agent_metering_pod_identity_state_owner_idx
    ON agent_metering_pod_identity_state (owner_kind, owner_id)
    WHERE attribution_scope = 'customer';

-- Append-only journal. It deliberately has no FK to agents/jobs/threads so
-- deleting mutable application rows cannot erase historical attribution.
CREATE TABLE agent_metering_binding_events (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id             UUID NOT NULL,
    revision             BIGINT NOT NULL,
    agent_present        BOOLEAN NOT NULL,
    pod_uid              TEXT,
    hostname             TEXT,
    identity_state       TEXT NOT NULL,
    attribution_scope    TEXT NOT NULL,
    owner_kind           TEXT,
    owner_id             UUID,
    user_id              UUID,
    project_id           UUID,
    reason_code          TEXT NOT NULL,
    transition_source    TEXT NOT NULL,
    effective_at         TIMESTAMPTZ NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT agent_metering_binding_events_agent_revision_uq
        UNIQUE (agent_id, revision),
    CONSTRAINT agent_metering_binding_events_identity_check CHECK (
        revision > 0
        AND (pod_uid IS NULL OR (pod_uid <> '' AND length(pod_uid) <= 256))
        AND (hostname IS NULL OR (hostname <> '' AND length(hostname) <= 255))
        AND identity_state IN ('valid', 'missing', 'duplicate')
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND transition_source ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    CONSTRAINT agent_metering_binding_events_attribution_check CHECK (
        attribution_scope IN ('customer', 'shared-platform', 'unknown')
        AND ((attribution_scope = 'customer'
                AND agent_present AND identity_state = 'valid'
                AND owner_kind IN ('job', 'thread')
                AND owner_id IS NOT NULL AND user_id IS NOT NULL)
             OR (attribution_scope = 'shared-platform'
                AND agent_present AND identity_state = 'valid'
                AND owner_kind IS NULL AND owner_id IS NULL
                AND user_id IS NULL AND project_id IS NULL)
             OR (attribution_scope = 'unknown'
                AND owner_kind IS NULL AND owner_id IS NULL
                AND user_id IS NULL AND project_id IS NULL))
    )
);

CREATE INDEX agent_metering_binding_events_pod_time_idx
    ON agent_metering_binding_events (pod_uid, effective_at, id)
    WHERE pod_uid IS NOT NULL;
CREATE INDEX agent_metering_binding_events_owner_time_idx
    ON agent_metering_binding_events (
        owner_kind, owner_id, effective_at, id
    ) WHERE attribution_scope = 'customer';

CREATE FUNCTION protect_agent_metering_identity_state_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent metering identity state cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.revision <> 1 THEN
            RAISE EXCEPTION 'agent metering identity state must begin at revision 1'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision <> OLD.revision + 1
       OR NEW.effective_at < OLD.effective_at
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'agent metering identity state transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_metering_pod_identity_state_one_way
BEFORE INSERT OR UPDATE OR DELETE ON agent_metering_pod_identity_state
FOR EACH ROW EXECUTE FUNCTION protect_agent_metering_identity_state_mutation();

CREATE FUNCTION append_agent_metering_binding_event()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    INSERT INTO public.agent_metering_binding_events (
        agent_id, revision, agent_present, pod_uid, hostname,
        identity_state, attribution_scope, owner_kind, owner_id,
        user_id, project_id, reason_code, transition_source, effective_at
    ) VALUES (
        NEW.agent_id, NEW.revision, NEW.agent_present, NEW.pod_uid, NEW.hostname,
        NEW.identity_state, NEW.attribution_scope, NEW.owner_kind, NEW.owner_id,
        NEW.user_id, NEW.project_id, NEW.reason_code, NEW.transition_source,
        NEW.effective_at
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER agent_metering_pod_identity_state_journal
AFTER INSERT OR UPDATE ON agent_metering_pod_identity_state
FOR EACH ROW EXECUTE FUNCTION append_agent_metering_binding_event();

CREATE FUNCTION protect_agent_metering_binding_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'agent metering binding events are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER agent_metering_binding_events_append_only
BEFORE UPDATE OR DELETE ON agent_metering_binding_events
FOR EACH ROW EXECUTE FUNCTION protect_agent_metering_binding_event_mutation();

-- Recompute one agent from mutually agreeing app-DB state. A valid unique Pod
-- with no owner is shared platform. A non-null but non-mutual owner claim is a
-- conflict and therefore unknown, never customer usage.
CREATE FUNCTION converge_agent_metering_binding(
    target_agent_id UUID,
    requested_transition_source TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    agent_row          RECORD;
    current_row        RECORD;
    job_row            RECORD;
    thread_row         RECORD;
    agent_found        BOOLEAN;
    initial_pod_uid    TEXT;
    normalized_pod_uid TEXT;
    normalized_host    TEXT;
    duplicate_count    BIGINT;
    next_present       BOOLEAN;
    next_identity      TEXT;
    next_scope         TEXT;
    next_owner_kind    TEXT;
    next_owner_id      UUID;
    next_user_id       UUID;
    next_project_id    UUID;
    next_reason        TEXT;
    transition_at      TIMESTAMPTZ;
BEGIN
    IF requested_transition_source IS NULL
       OR requested_transition_source !~ '^[a-z0-9][a-z0-9._-]{0,63}$' THEN
        RAISE EXCEPTION 'agent metering transition source is invalid'
            USING ERRCODE = '22023';
    END IF;

    -- Every convergence path uses Pod identity then agent identity lock order.
    -- Re-read after either wait so a job/thread trigger cannot overwrite a
    -- newer registration transition with the agent row it saw before waiting.
    SELECT agent.id, agent.pod_uid, agent.hostname,
           agent.current_job_id, agent.thread_id
    INTO agent_row
    FROM public.agents AS agent
    WHERE agent.id = target_agent_id;
    agent_found := FOUND;
    IF agent_found THEN
        initial_pod_uid := NULLIF(btrim(agent_row.pod_uid), '');
        IF initial_pod_uid IS NOT NULL
           AND length(initial_pod_uid) <= 256 THEN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'srw-agent-metering-pod:' || initial_pod_uid,
                    0
                )
            );
        END IF;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'srw-agent-metering-agent:' || target_agent_id::TEXT,
            0
        )
    );
    SELECT agent.id, agent.pod_uid, agent.hostname,
           agent.current_job_id, agent.thread_id
    INTO agent_row
    FROM public.agents AS agent
    WHERE agent.id = target_agent_id;
    agent_found := FOUND;

    IF NOT agent_found THEN
        SELECT * INTO current_row
        FROM public.agent_metering_pod_identity_state
        WHERE agent_id = target_agent_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RETURN;
        END IF;
        normalized_pod_uid := current_row.pod_uid;
        normalized_host := current_row.hostname;
        next_present := FALSE;
        next_identity := 'missing';
        next_scope := 'unknown';
        next_owner_kind := NULL;
        next_owner_id := NULL;
        next_user_id := NULL;
        next_project_id := NULL;
        next_reason := 'agent-row-deleted';
    ELSE
        normalized_pod_uid := NULLIF(btrim(agent_row.pod_uid), '');
        IF normalized_pod_uid IS NOT NULL
           AND length(normalized_pod_uid) > 256 THEN
            normalized_pod_uid := NULL;
        END IF;
        normalized_host := NULLIF(btrim(agent_row.hostname), '');
        IF normalized_host IS NOT NULL AND length(normalized_host) > 255 THEN
            normalized_host := NULL;
        END IF;
        next_present := TRUE;
        next_owner_kind := NULL;
        next_owner_id := NULL;
        next_user_id := NULL;
        next_project_id := NULL;

        IF normalized_pod_uid IS NULL THEN
            next_identity := 'missing';
            next_scope := 'unknown';
            next_reason := 'missing-pod-uid';
        ELSE
            -- The caller already holds the observed Pod-identity lock. Agent
            -- row triggers additionally pre-lock both old and new UIDs, so
            -- simultaneous re-registrations cannot form a peer-state cycle.
            SELECT count(*) INTO duplicate_count
            FROM public.agents AS peer
            WHERE NULLIF(btrim(peer.pod_uid), '') = normalized_pod_uid;

            IF duplicate_count > 1 THEN
                next_identity := 'duplicate';
                next_scope := 'unknown';
                next_reason := 'duplicate-pod-uid';
            ELSIF agent_row.current_job_id IS NOT NULL
                  AND agent_row.thread_id IS NOT NULL THEN
                next_identity := 'valid';
                next_scope := 'unknown';
                next_reason := 'dual-owner-conflict';
            ELSIF agent_row.current_job_id IS NOT NULL THEN
                SELECT job.id, job.user_id, job.project_id,
                       job.status, job.assigned_agent_id
                INTO job_row
                FROM public.jobs AS job
                WHERE job.id = agent_row.current_job_id;
                IF FOUND
                   AND job_row.status = 'processing'
                   AND job_row.assigned_agent_id = target_agent_id
                   AND job_row.user_id IS NOT NULL THEN
                    next_identity := 'valid';
                    next_scope := 'customer';
                    next_owner_kind := 'job';
                    next_owner_id := job_row.id;
                    next_user_id := job_row.user_id;
                    next_project_id := job_row.project_id;
                    next_reason := 'job-mutual-binding';
                ELSE
                    next_identity := 'valid';
                    next_scope := 'unknown';
                    next_reason := 'job-binding-conflict';
                END IF;
            ELSIF agent_row.thread_id IS NOT NULL THEN
                SELECT thread.id, thread.user_id, thread.project_id,
                       thread.status, thread.agent_id
                INTO thread_row
                FROM public.threads AS thread
                WHERE thread.id = agent_row.thread_id;
                IF FOUND
                   AND thread_row.status IN ('active', 'awaiting_user')
                   AND thread_row.agent_id = target_agent_id
                   AND thread_row.user_id IS NOT NULL THEN
                    next_identity := 'valid';
                    next_scope := 'customer';
                    next_owner_kind := 'thread';
                    next_owner_id := thread_row.id;
                    next_user_id := thread_row.user_id;
                    next_project_id := thread_row.project_id;
                    next_reason := 'thread-mutual-binding';
                ELSE
                    next_identity := 'valid';
                    next_scope := 'unknown';
                    next_reason := 'thread-binding-conflict';
                END IF;
            ELSE
                next_identity := 'valid';
                next_scope := 'shared-platform';
                next_reason := 'unbound-agent';
            END IF;
        END IF;
    END IF;

    SELECT * INTO current_row
    FROM public.agent_metering_pod_identity_state
    WHERE agent_id = target_agent_id
    FOR UPDATE;

    IF NOT FOUND THEN
        transition_at := clock_timestamp();
        INSERT INTO public.agent_metering_pod_identity_state (
            agent_id, agent_present, pod_uid, hostname, identity_state,
            attribution_scope, owner_kind, owner_id, user_id, project_id,
            reason_code, transition_source, revision, effective_at
        ) VALUES (
            target_agent_id, next_present, normalized_pod_uid, normalized_host,
            next_identity, next_scope, next_owner_kind, next_owner_id,
            next_user_id, next_project_id, next_reason,
            requested_transition_source, 1, transition_at
        );
        RETURN;
    END IF;

    IF current_row.agent_present IS NOT DISTINCT FROM next_present
       AND current_row.pod_uid IS NOT DISTINCT FROM normalized_pod_uid
       AND current_row.hostname IS NOT DISTINCT FROM normalized_host
       AND current_row.identity_state IS NOT DISTINCT FROM next_identity
       AND current_row.attribution_scope IS NOT DISTINCT FROM next_scope
       AND current_row.owner_kind IS NOT DISTINCT FROM next_owner_kind
       AND current_row.owner_id IS NOT DISTINCT FROM next_owner_id
       AND current_row.user_id IS NOT DISTINCT FROM next_user_id
       AND current_row.project_id IS NOT DISTINCT FROM next_project_id
       AND current_row.reason_code IS NOT DISTINCT FROM next_reason THEN
        RETURN;
    END IF;

    -- statement_timestamp() is fixed before any advisory/row-lock wait. A
    -- concurrent statement may therefore resume after a newer revision while
    -- still carrying an older timestamp. Sample the wall clock only after the
    -- current head is locked and clamp it to the durable head so revisions can
    -- never move effective_at or updated_at backwards.
    transition_at := GREATEST(clock_timestamp(), current_row.effective_at);
    UPDATE public.agent_metering_pod_identity_state
    SET agent_present = next_present,
        pod_uid = normalized_pod_uid,
        hostname = normalized_host,
        identity_state = next_identity,
        attribution_scope = next_scope,
        owner_kind = next_owner_kind,
        owner_id = next_owner_id,
        user_id = next_user_id,
        project_id = next_project_id,
        reason_code = next_reason,
        transition_source = requested_transition_source,
        revision = current_row.revision + 1,
        effective_at = transition_at,
        updated_at = transition_at
    WHERE agent_id = target_agent_id;
END;
$$;

CREATE FUNCTION converge_agent_metering_from_agent_row()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    old_pod_uid TEXT;
    new_pod_uid TEXT;
    locked_uid  TEXT;
    peer_id     UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        old_pod_uid := NULLIF(btrim(OLD.pod_uid), '');
    ELSE
        new_pod_uid := NULLIF(btrim(NEW.pod_uid), '');
        IF TG_OP = 'UPDATE' THEN
            old_pod_uid := NULLIF(btrim(OLD.pod_uid), '');
        END IF;
    END IF;

    -- A row update is already visible to its own trigger but not to a
    -- concurrent peer. Lock both sides in lexical order before touching any
    -- metering head; this prevents old-UID peer convergence deadlocks.
    FOR locked_uid IN
        SELECT candidate.uid
        FROM (
            SELECT old_pod_uid AS uid
            UNION
            SELECT new_pod_uid AS uid
        ) AS candidate
        WHERE candidate.uid IS NOT NULL AND length(candidate.uid) <= 256
        ORDER BY candidate.uid
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'srw-agent-metering-pod:' || locked_uid,
                0
            )
        );
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        PERFORM public.converge_agent_metering_binding(
            OLD.id, 'agents-delete'
        );
        FOR peer_id IN
            SELECT agent.id FROM public.agents AS agent
            WHERE old_pod_uid IS NOT NULL
              AND NULLIF(btrim(agent.pod_uid), '') = old_pod_uid
            ORDER BY agent.id
            FOR NO KEY UPDATE OF agent SKIP LOCKED
        LOOP
            PERFORM public.converge_agent_metering_binding(
                peer_id, 'agents-delete-peer'
            );
        END LOOP;
        RETURN OLD;
    END IF;

    PERFORM public.converge_agent_metering_binding(
        NEW.id,
        CASE WHEN TG_OP = 'INSERT' THEN 'agents-insert' ELSE 'agents-update' END
    );
    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.id <> NEW.id
          AND ((new_pod_uid IS NOT NULL
                AND NULLIF(btrim(agent.pod_uid), '') = new_pod_uid)
               OR (old_pod_uid IS NOT NULL
                AND NULLIF(btrim(agent.pod_uid), '') = old_pod_uid))
        ORDER BY agent.id
        FOR NO KEY UPDATE OF agent SKIP LOCKED
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id, 'agents-identity-peer'
        );
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_metering_agents_insert
AFTER INSERT ON agents
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_agent_row();
CREATE TRIGGER agent_metering_agents_update
AFTER UPDATE OF pod_uid, hostname, current_job_id, thread_id ON agents
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_agent_row();
CREATE TRIGGER agent_metering_agents_delete
AFTER DELETE ON agents
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_agent_row();

CREATE FUNCTION converge_agent_metering_from_job_row()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_job_id UUID;
    old_agent_id  UUID;
    new_agent_id  UUID;
    peer_id       UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_job_id := OLD.id;
        old_agent_id := OLD.assigned_agent_id;
    ELSIF TG_OP = 'INSERT' THEN
        target_job_id := NEW.id;
        new_agent_id := NEW.assigned_agent_id;
    ELSE
        target_job_id := NEW.id;
        old_agent_id := OLD.assigned_agent_id;
        new_agent_id := NEW.assigned_agent_id;
    END IF;

    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.current_job_id = target_job_id
           OR agent.id = old_agent_id
           OR agent.id = new_agent_id
        ORDER BY agent.id
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id,
            CASE WHEN TG_OP = 'INSERT' THEN 'jobs-insert'
                 WHEN TG_OP = 'DELETE' THEN 'jobs-delete'
                 ELSE 'jobs-update' END
        );
    END LOOP;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_metering_jobs_insert
AFTER INSERT ON jobs
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_job_row();
CREATE TRIGGER agent_metering_jobs_update
AFTER UPDATE OF status, assigned_agent_id, user_id, project_id ON jobs
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_job_row();
CREATE TRIGGER agent_metering_jobs_delete
AFTER DELETE ON jobs
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_job_row();

CREATE FUNCTION converge_agent_metering_from_thread_row()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_thread_id UUID;
    old_agent_id     UUID;
    new_agent_id     UUID;
    peer_id          UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_thread_id := OLD.id;
        old_agent_id := OLD.agent_id;
    ELSIF TG_OP = 'INSERT' THEN
        target_thread_id := NEW.id;
        new_agent_id := NEW.agent_id;
    ELSE
        target_thread_id := NEW.id;
        old_agent_id := OLD.agent_id;
        new_agent_id := NEW.agent_id;
    END IF;

    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.thread_id = target_thread_id
           OR agent.id = old_agent_id
           OR agent.id = new_agent_id
        ORDER BY agent.id
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id,
            CASE WHEN TG_OP = 'INSERT' THEN 'threads-insert'
                 WHEN TG_OP = 'DELETE' THEN 'threads-delete'
                 ELSE 'threads-update' END
        );
    END LOOP;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_metering_threads_insert
AFTER INSERT ON threads
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_thread_row();
CREATE TRIGGER agent_metering_threads_update
AFTER UPDATE OF status, agent_id, user_id, project_id ON threads
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_thread_row();
CREATE TRIGGER agent_metering_threads_delete
AFTER DELETE ON threads
FOR EACH ROW EXECUTE FUNCTION converge_agent_metering_from_thread_row();

-- Seed current state and revision 1 journal rows for agents that predate this
-- migration. The convergence function is idempotent and safe on replay.
SELECT converge_agent_metering_binding(id, 'migration-backfill')
FROM agents
ORDER BY id;

COMMENT ON TABLE compute_metering_activation IS
    'Forward-only activation boundaries for agent Pods, IDE workspace Pods, and VMI compute.';
COMMENT ON TABLE compute_shadow_observations IS
    'Immutable non-publishable per-item compute shadow classifications; no ledger relationship.';
COMMENT ON TABLE agent_metering_pod_identity_state IS
    'Current converged agent Pod identity and attribution head, including missing/duplicate ambiguity and deletion tombstones.';
COMMENT ON TABLE agent_metering_binding_events IS
    'Append-only revisions of mutually validated agent Pod identity and job/thread attribution.';

COMMIT;
