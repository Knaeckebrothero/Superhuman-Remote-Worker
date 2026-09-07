-- migration:     0104_agent_metering_lock_order.sql
-- description:   Preserve complete duplicate-Pod convergence while enforcing
--                one advisory-lock order across agent/job/thread triggers.
-- depends-on:    0103_compute_metering_foundations.sql
-- expected:      < 30s. Function replacement is catalog-only after a brief
--                write fence on the three trigger source tables.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on agents, jobs, and threads.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Do not allow a writer to cross the three-function replacement with a mixed
-- lock protocol. Reads remain available; the migration runner retries a failed
-- deployment rather than waiting indefinitely behind a busy writer.
LOCK TABLE agents, jobs, threads IN SHARE ROW EXCLUSIVE MODE;

CREATE OR REPLACE FUNCTION public.converge_agent_metering_from_agent_row()
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
        -- Do not row-lock peers here. The outer DELETE already owns OLD's
        -- tuple lock, while a concurrent peer mutation owns its own tuple
        -- lock before its AFTER trigger can wait on the Pod advisory lock.
        -- Waiting for that peer row would invert those locks and deadlock;
        -- SKIP LOCKED, conversely, can leave the survivor permanently marked
        -- duplicate. The Pod advisory lock serializes changes for this UID,
        -- and a plain MVCC read lets this transaction converge committed
        -- peers without waiting. A concurrent peer mutation runs its own
        -- trigger after this transaction releases the UID lock and is the
        -- final-state repair.
        FOR peer_id IN
            SELECT agent.id FROM public.agents AS agent
            WHERE old_pod_uid IS NOT NULL
              AND NULLIF(btrim(agent.pod_uid), '') = old_pod_uid
            ORDER BY agent.id
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
    -- Peer discovery intentionally remains a non-locking MVCC read; see the
    -- DELETE path above for the row-lock/advisory-lock ordering contract.
    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.id <> NEW.id
          AND ((new_pod_uid IS NOT NULL
                AND NULLIF(btrim(agent.pod_uid), '') = new_pod_uid)
               OR (old_pod_uid IS NOT NULL
                AND NULLIF(btrim(agent.pod_uid), '') = old_pod_uid))
        ORDER BY agent.id
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id, 'agents-identity-peer'
        );
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.converge_agent_metering_from_job_row()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_job_id UUID;
    old_agent_id  UUID;
    new_agent_id  UUID;
    locked_uid    TEXT;
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

    -- One owner transition may converge more than one inconsistent/transitional
    -- agent row. Advisory locks live until transaction end, so acquiring them
    -- indirectly in agent UUID order can conflict with the lexical old/new UID
    -- order used by an agent-row trigger. Prelock the complete visible UID set
    -- lexically before touching any metering head. Concurrent agent mutations
    -- include their old UID in the same lock protocol and perform the final
    -- repair after this transaction when their uncommitted row was not visible.
    FOR locked_uid IN
        SELECT candidate.uid
        FROM (
            SELECT DISTINCT NULLIF(btrim(agent.pod_uid), '') AS uid
            FROM public.agents AS agent
            WHERE agent.current_job_id = target_job_id
               OR agent.id = old_agent_id
               OR agent.id = new_agent_id
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

CREATE OR REPLACE FUNCTION public.converge_agent_metering_from_thread_row()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_thread_id UUID;
    old_agent_id     UUID;
    new_agent_id     UUID;
    locked_uid       TEXT;
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

    -- Match the job-trigger lock contract: collect every currently visible Pod
    -- identity first and acquire the transaction locks in one lexical order.
    FOR locked_uid IN
        SELECT candidate.uid
        FROM (
            SELECT DISTINCT NULLIF(btrim(agent.pod_uid), '') AS uid
            FROM public.agents AS agent
            WHERE agent.thread_id = target_thread_id
               OR agent.id = old_agent_id
               OR agent.id = new_agent_id
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

COMMIT;
