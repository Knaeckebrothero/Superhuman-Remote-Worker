-- migration:     0191_stateless_input_deliveries.sql
-- description:   Admit durable event input on stateless session turns with
--                exact queue-lease fencing.
-- depends-on:    0190_managed_repository_legacy_reconciliation.sql
-- expected:      < 1s. Adds nullable provenance columns, adopts only genuine
--                unclaimed/unadmitted stateless wake rows, assigns those old
--                NULL-turn inputs fresh executable turn identities, and
--                installs three trigger fences. No queue-row rewrite.
-- locks:         SHARE ROW EXCLUSIVE on threads -> run_queue -> deliveries
--                serializes the historical lane backfill with prior-release
--                writers. Catalog changes then briefly upgrade the latter two
--                tables to ACCESS EXCLUSIVE. A lock-timeout rolls back every
--                change; quiesce writers and follow the standard dirty-row
--                recovery runbook before retrying the migration.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Lock the complete authority prefix before adding/backfilling the lane
-- snapshot. Without the threads lock, an execution_lane UPDATE can commit
-- between the backfill SELECT and trigger installation, leaving a delivery
-- permanently stamped for the wrong lane. The order matches runtime admission.
LOCK TABLE public.threads IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE public.run_queue IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE public.thread_input_deliveries IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE public.thread_input_deliveries
    ADD COLUMN execution_lane TEXT NOT NULL DEFAULT 'pinned',
    ADD COLUMN owner_run_queue_lease_token BIGINT,
    ADD COLUMN owner_executor TEXT,
    ADD COLUMN owner_executor_pod_uid TEXT;

-- 0174 had a pinned-only execution protocol even though its orchestrator-side
-- persistence branch could write an UNCLAIMED role=event wake for an already
-- stateless thread.  The current thread lane is therefore not historical
-- provenance for every old row: an admitted/settled pinned receipt may have
-- legitimately outlived a later pinned -> stateless transition.  Keep the new
-- column's DEFAULT 'pinned' for all terminal or owner-bearing history.
--
-- The only genuine predecessor shape eligible for one-time stateless adoption
-- is an unclaimed, unadmitted, live officer_wake event.  Anything else pending
-- on a currently stateless thread has no safe execution authority to infer.
DO $$
DECLARE
    ambiguous_count BIGINT;
BEGIN
    SELECT count(*)
      INTO ambiguous_count
      FROM public.thread_input_deliveries AS delivery
      JOIN public.threads AS thread
        ON thread.id = delivery.thread_id
      JOIN public.thread_messages AS message
        ON message.id = delivery.message_id
     WHERE thread.execution_lane = 'stateless'
       AND delivery.state IN ('persisted', 'owned', 'queued', 'deferred')
       AND NOT (
           delivery.state = 'persisted'
           AND delivery.claim_generation = 0
           AND delivery.owner_agent_id IS NULL
           AND delivery.owner_pod_uid IS NULL
           AND delivery.owner_runtime_generation IS NULL
           AND delivery.source = 'officer_wake'
           AND message.role = 'event'
           AND message.turn_number IS NULL
           AND message.rewound_at IS NULL
           AND btrim(COALESCE(message.content, '')) <> ''
       );

    IF ambiguous_count > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'stateless_input_delivery_history_ambiguous',
            MESSAGE = 'Pre-0191 stateless input history is ambiguous',
            DETAIL = format(
                '%s pending row(s) retain claimed or unsupported pinned authority',
                ambiguous_count
            ),
            HINT = 'Resolve the reported history through its owning thread lifecycle before retrying.';
    END IF;
END;
$$;

UPDATE public.thread_input_deliveries AS delivery
   SET execution_lane = 'stateless'
  FROM public.threads AS thread,
       public.thread_messages AS message
 WHERE thread.id = delivery.thread_id
   AND message.id = delivery.message_id
   AND thread.execution_lane = 'stateless'
   AND delivery.state = 'persisted'
   AND delivery.claim_generation = 0
   AND delivery.owner_agent_id IS NULL
   AND delivery.owner_pod_uid IS NULL
   AND delivery.owner_runtime_generation IS NULL
   AND delivery.source = 'officer_wake'
   AND message.role = 'event'
   AND message.turn_number IS NULL
   AND message.rewound_at IS NULL
   AND btrim(COALESCE(message.content, '')) <> '';

-- The predecessor durable branch did not assign a turn number because the
-- pinned runtime used to claim it later.  Stateless execution requires an
-- exact positive identity before queue injection.  These rows have never
-- crossed provider admission, so allocate fresh identities after every turn
-- already known to the thread, ordered by immutable transcript sequence.
WITH eligible AS (
    SELECT delivery.message_id,
           delivery.thread_id,
           GREATEST(
               thread.total_turns,
               COALESCE((
                   SELECT max(existing.turn_number)
                     FROM public.thread_messages AS existing
                    WHERE existing.thread_id = thread.id
               ), 0)
           ) AS base_turn,
           row_number() OVER (
               PARTITION BY delivery.thread_id
               ORDER BY message.seq, delivery.delivery_id
           ) AS turn_offset
      FROM public.thread_input_deliveries AS delivery
      JOIN public.threads AS thread
        ON thread.id = delivery.thread_id
      JOIN public.thread_messages AS message
        ON message.id = delivery.message_id
     WHERE delivery.execution_lane = 'stateless'
       AND delivery.state = 'persisted'
       AND delivery.claim_generation = 0
       AND delivery.owner_agent_id IS NULL
       AND delivery.owner_pod_uid IS NULL
       AND delivery.owner_runtime_generation IS NULL
       AND delivery.source = 'officer_wake'
       AND message.role = 'event'
       AND message.turn_number IS NULL
       AND message.rewound_at IS NULL
       AND btrim(COALESCE(message.content, '')) <> ''
), assigned AS (
    UPDATE public.thread_messages AS message
       SET turn_number = (eligible.base_turn + eligible.turn_offset)::integer
      FROM eligible
     WHERE message.id = eligible.message_id
    RETURNING eligible.thread_id,
              (eligible.base_turn + eligible.turn_offset)::integer AS assigned_turn
), maxima AS (
    SELECT thread_id, max(assigned_turn) AS assigned_turn
      FROM assigned
     GROUP BY thread_id
)
UPDATE public.threads AS thread
   SET total_turns = GREATEST(thread.total_turns, maxima.assigned_turn)
  FROM maxima
 WHERE thread.id = maxima.thread_id;

ALTER TABLE public.thread_input_deliveries
    DROP CONSTRAINT thread_input_deliveries_owner_shape,
    DROP CONSTRAINT thread_input_deliveries_claim_shape,
    ADD CONSTRAINT thread_input_deliveries_lane_check
        CHECK (execution_lane IN ('pinned', 'stateless')) NOT VALID,
    ADD CONSTRAINT thread_input_deliveries_owner_shape CHECK (
        (
            execution_lane = 'pinned'
            AND owner_run_queue_lease_token IS NULL
            AND owner_executor IS NULL
            AND owner_executor_pod_uid IS NULL
            AND (
                (owner_agent_id IS NULL AND owner_pod_uid IS NULL
                    AND owner_runtime_generation IS NULL)
                OR
                (owner_agent_id IS NOT NULL AND owner_pod_uid IS NOT NULL
                    AND owner_runtime_generation IS NOT NULL)
            )
        )
        OR
        (
            execution_lane = 'stateless'
            AND owner_agent_id IS NULL
            AND owner_pod_uid IS NULL
            AND owner_runtime_generation IS NULL
            AND (
                (
                    claim_generation = 0
                    AND owner_run_queue_lease_token IS NULL
                    AND owner_executor IS NULL
                    AND owner_executor_pod_uid IS NULL
                )
                OR
                (
                    claim_generation > 0
                    AND owner_run_queue_lease_token IS NOT NULL
                    AND owner_run_queue_lease_token > 0
                    AND owner_executor IS NOT NULL
                    AND btrim(owner_executor) <> ''
                    AND owner_executor_pod_uid IS NOT NULL
                    AND btrim(owner_executor_pod_uid) <> ''
                )
            )
        )
    ) NOT VALID,
    ADD CONSTRAINT thread_input_deliveries_claim_shape CHECK (
        (
            execution_lane = 'pinned'
            AND (
                (claim_generation = 0 AND owner_agent_id IS NULL)
                OR (claim_generation > 0 AND owner_agent_id IS NOT NULL)
            )
        )
        OR execution_lane = 'stateless'
    ) NOT VALID;

ALTER TABLE public.run_queue
    ADD COLUMN input_delivery_capable_lease_token BIGINT;

CREATE FUNCTION public.require_input_delivery_lane_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_lane TEXT;
    message_role TEXT;
BEGIN
    SELECT thread.execution_lane
      INTO current_lane
      FROM public.threads AS thread
     WHERE thread.id = NEW.thread_id
     FOR NO KEY UPDATE;

    IF current_lane IS NULL OR NEW.execution_lane IS DISTINCT FROM current_lane THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'input_delivery_lane_mismatch',
            MESSAGE = 'Input delivery lane does not match its owning thread',
            HINT = 'Retry from a lane-aware input-delivery writer.';
    END IF;

    IF current_lane = 'stateless' THEN
        SELECT message.role
          INTO message_role
          FROM public.thread_messages AS message
         WHERE message.id = NEW.message_id
           AND message.thread_id = NEW.thread_id;
        IF message_role IS DISTINCT FROM 'event'
           OR NEW.source IS DISTINCT FROM 'officer_wake' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'stateless_input_delivery_event_only',
                MESSAGE = 'Stateless durable input authority is reserved for server events';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_input_delivery_lane_authority
BEFORE INSERT OR UPDATE OF thread_id, message_id, source, execution_lane
ON public.thread_input_deliveries
FOR EACH ROW EXECUTE FUNCTION public.require_input_delivery_lane_authority();

CREATE FUNCTION public.require_thread_lane_without_pending_input()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.execution_lane IS DISTINCT FROM OLD.execution_lane
       AND EXISTS (
           SELECT 1
             FROM public.thread_input_deliveries AS delivery
            WHERE delivery.thread_id = NEW.id
              AND delivery.state IN ('persisted', 'owned', 'queued', 'deferred')
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'thread_lane_change_has_pending_input',
            MESSAGE = 'Thread lane cannot change while durable input is pending',
            HINT = 'Admit or settle the exact delivery before changing lanes.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_thread_lane_without_pending_input
BEFORE UPDATE OF execution_lane ON public.threads
FOR EACH ROW EXECUTE FUNCTION public.require_thread_lane_without_pending_input();

CREATE FUNCTION public.require_stateless_input_delivery_claim()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    pending_event BOOLEAN := FALSE;
BEGIN
    IF NEW.unit_kind IS DISTINCT FROM 'session_turn' THEN
        RETURN NEW;
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM public.thread_input_deliveries AS delivery
          JOIN public.thread_messages AS message
            ON message.id = delivery.message_id
         WHERE delivery.thread_id = NEW.unit_id
           AND delivery.execution_lane = 'stateless'
           AND delivery.state IN ('persisted', 'owned', 'queued', 'deferred')
           AND message.rewound_at IS NULL
    ) INTO pending_event;

    IF pending_event
       AND NEW.state = 'leased'
       AND (
           OLD.state IS DISTINCT FROM NEW.state
           OR OLD.lease_token IS DISTINCT FROM NEW.lease_token
       )
       AND NEW.input_delivery_capable_lease_token
            IS DISTINCT FROM NEW.lease_token THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'stateless_input_delivery_requires_capable_claim',
            MESSAGE = 'Stateless event input requires a lane-aware executor claim',
            HINT = 'Retry the claim from an input-ledger-aware runtime.';
    END IF;

    IF NEW.consumed_seq IS NOT NULL
       AND (
           OLD.consumed_seq IS NULL
           OR NEW.consumed_seq > OLD.consumed_seq
       )
       AND EXISTS (
           SELECT 1
             FROM public.thread_input_deliveries AS delivery
             JOIN public.thread_messages AS message
               ON message.id = delivery.message_id
            WHERE delivery.thread_id = NEW.unit_id
              AND delivery.execution_lane = 'stateless'
              AND delivery.state IN ('persisted', 'owned', 'queued', 'deferred')
              AND message.rewound_at IS NULL
              AND message.seq <= NEW.consumed_seq
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'stateless_input_delivery_requires_admission',
            MESSAGE = 'A stateless event cannot be consumed before provider admission';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_stateless_input_delivery_claim
BEFORE UPDATE OF state, lease_token, consumed_seq ON public.run_queue
FOR EACH ROW EXECUTE FUNCTION public.require_stateless_input_delivery_claim();

COMMENT ON COLUMN public.thread_input_deliveries.execution_lane IS
    'Server-observed owning thread lane. A rolling-old writer defaults to pinned '
    'and is rejected when the live thread is stateless.';
COMMENT ON COLUMN public.thread_input_deliveries.owner_run_queue_lease_token IS
    'Exact stateless session_turn fencing token that owns provider admission.';
COMMENT ON COLUMN public.thread_input_deliveries.owner_executor IS
    'Stateless executor identity snapshot; paired with the exact run_queue lease.';
COMMENT ON COLUMN public.thread_input_deliveries.owner_executor_pod_uid IS
    'Kubernetes Pod UID snapshot for the stateless delivery claimant.';
COMMENT ON COLUMN public.run_queue.input_delivery_capable_lease_token IS
    '0185 rolling-upgrade marker. A session claim with pending event input must '
    'stamp the newly allocated lease token in the same UPDATE.';
COMMENT ON FUNCTION public.require_input_delivery_lane_authority() IS
    'Rejects rolling-old or forged stateless input-ledger writers before queueing.';
COMMENT ON FUNCTION public.require_thread_lane_without_pending_input() IS
    'Serializes lane changes with durable input so one stable delivery cannot '
    'become unclaimable between the pinned and stateless authorities.';
COMMENT ON FUNCTION public.require_stateless_input_delivery_claim() IS
    'Fences rolling-old stateless claims and refuses watermark consumption before '
    'the exact durable event reaches provider admission.';

COMMIT;
