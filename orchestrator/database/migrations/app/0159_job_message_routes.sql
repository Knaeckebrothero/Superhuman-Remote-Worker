-- migration:     0159_job_message_routes.sql
-- description:   Durable route ledger for officer-aware worker messages
--                (officer_message_routing.md §3). One row per logical
--                worker→human message that has routing/control state: the
--                policy snapshot frozen at send time, the delivery state
--                machine, both deadlines (officer SLA + total blocking
--                timeout), and the actor audit trail. message_log stays the
--                delivery/audit record; this row is the control state the
--                blocking-send transaction, the officer inbox tools, and the
--                leader-gated reconciler all CAS against.
-- depends-on:    0158_repair_unified_tool_group_markers.sql
-- expected:      < 1s. One CREATE TABLE + trigger + four indexes on an empty
--                table. No existing table is touched.
-- locks:         None beyond catalog locks for the new objects.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE public.job_message_routes (
    route_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL
                        REFERENCES public.jobs(id) ON DELETE CASCADE,
    -- Nullable: user_direct routes exist for project-less jobs too (the
    -- total blocking timeout applies to every blocking send).
    project_id      UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    -- The job-message thread key — message_log.thread_id's shape (short hex),
    -- NOT a persistent-session UUID.
    thread_id       VARCHAR(64) NOT NULL,
    originating_message_id UUID
                        REFERENCES public.message_log(id) ON DELETE SET NULL,
    -- Frozen at send: {worker_messages, officer_response_minutes, applied,
    -- reason, purpose, resolved_at}. Later project-policy edits never
    -- retarget a waiting question (officer_message_routing.md §2.1).
    policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- §3 state machine:
    --   pending_officer -> resolved_by_officer
    --                   -> escalated_to_user -> resolved_by_user
    --                   -> timed_out
    --   pending_both    -> resolved_by_officer | resolved_by_user
    --   user_direct     -> resolved_by_user
    --   any pre-delivery state -> delivery_failed -> fallback or timed_out
    state           TEXT NOT NULL,
    blocking        BOOLEAN NOT NULL DEFAULT FALSE,
    -- The commissioned incarnation the route was addressed to. A later
    -- recommission (different thread) never adopts this route (§5.1).
    officer_thread_id UUID
                        REFERENCES public.threads(id) ON DELETE SET NULL,
    -- len(post.incarnations) at snapshot time — a monotonic incarnation
    -- counter recorded on transitions for audit.
    officer_incarnation INTEGER,
    -- created_at + policy_snapshot.officer_response_minutes; only blocking
    -- pending_officer routes carry one (§5.2 deadline 1).
    officer_deadline TIMESTAMPTZ,
    -- When the user actually received this thread (initial delivery for
    -- user_direct/pending_both, escalation delivery otherwise). NULL =
    -- delivery still owed; the reconciler's repair leg keys on it.
    user_delivery_at TIMESTAMPTZ,
    resolved_by_kind TEXT,
    resolved_by_id  TEXT,
    resolved_at     TIMESTAMPTZ,
    -- created_at + communication.blocking_timeout_hours (§5.2 deadline 2);
    -- blocking routes only.
    total_deadline  TIMESTAMPTZ,
    -- Append-only actor audit: [{at, from, to, actor_kind, actor_id,
    -- officer_incarnation, note}] — §7 requires actor kind/id on EVERY
    -- transition, not only the final one.
    transitions     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT job_message_routes_state_check CHECK (state IN (
        'pending_officer', 'pending_both', 'user_direct',
        'escalated_to_user', 'resolved_by_officer', 'resolved_by_user',
        'timed_out', 'delivery_failed'
    )),
    CONSTRAINT job_message_routes_policy_is_object
        CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    CONSTRAINT job_message_routes_transitions_is_array
        CHECK (jsonb_typeof(transitions) = 'array')
);

COMMENT ON TABLE public.job_message_routes IS
    'Control state for officer-aware worker messages '
    '(officer_message_routing.md §3). message_log keeps the canonical '
    'thread; this ledger carries the per-message policy snapshot, delivery '
    'state, officer SLA + total blocking deadlines, and the CAS surface the '
    'reply lanes and the leader-gated reconciler resolve through exactly '
    'once.';
COMMENT ON COLUMN public.job_message_routes.thread_id IS
    'Job message thread key (message_log.thread_id shape). One canonical '
    'thread — the route never forks the conversation.';
COMMENT ON COLUMN public.job_message_routes.policy_snapshot IS
    'Routing policy frozen at send time. Changing the project setting later '
    'never retargets a waiting question.';
COMMENT ON COLUMN public.job_message_routes.state IS
    'pending_officer/pending_both/user_direct -> resolved_by_officer | '
    'resolved_by_user | escalated_to_user | timed_out; pre-delivery states '
    'may pass through delivery_failed. CAS-only transitions.';
COMMENT ON COLUMN public.job_message_routes.transitions IS
    'Append-only [{at, from, to, actor_kind, actor_id, officer_incarnation, '
    'note}] — actor identity on every transition (spec §7).';

CREATE TRIGGER update_job_message_routes_updated_at
BEFORE UPDATE ON public.job_message_routes
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();

-- Reply-lane lookup: newest route for a (job, thread).
CREATE INDEX idx_job_message_routes_job_thread
    ON public.job_message_routes (job_id, thread_id, created_at DESC);

-- Reconciler leg 1: officer-SLA expiry scan.
CREATE INDEX idx_job_message_routes_officer_sla
    ON public.job_message_routes (officer_deadline)
    WHERE state = 'pending_officer' AND blocking;

-- Reconciler leg 2: total blocking timeout scan over every open state.
CREATE INDEX idx_job_message_routes_total_deadline
    ON public.job_message_routes (total_deadline)
    WHERE blocking AND state IN (
        'pending_officer', 'pending_both', 'user_direct',
        'escalated_to_user', 'delivery_failed'
    );

-- Officer inbox / sitrep: open routes per project, oldest first.
CREATE INDEX idx_job_message_routes_open_project
    ON public.job_message_routes (project_id, created_at)
    WHERE state IN (
        'pending_officer', 'pending_both', 'escalated_to_user',
        'delivery_failed'
    );

COMMIT;
