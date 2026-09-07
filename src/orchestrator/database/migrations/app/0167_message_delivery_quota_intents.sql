-- migration:     0167_message_delivery_quota_intents.sql
-- description:   Durable effective-audience and idempotent quota/delivery
--                intent ledger for Officer-aware worker messages (OC-07).
-- depends-on:    0166_job_message_routes_closed_state.sql
-- rolling:       additive columns carry conservative legacy_human defaults.
--                Old replicas keep their existing human limiter and their
--                outbound rows are mirrored into the new human ledger by a
--                trigger, so mixed-version traffic cannot become uncounted.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.message_log
    ADD COLUMN routing_generation UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN effective_audience TEXT NOT NULL DEFAULT 'legacy_human',
    ADD CONSTRAINT message_log_effective_audience_check CHECK (
        effective_audience IN (
            'legacy_human', 'human', 'officer', 'officer_and_user',
            'explicit_recipient'
        )
    );

ALTER TABLE public.job_message_routes
    ADD COLUMN routing_generation UUID,
    ADD COLUMN effective_audience TEXT NOT NULL DEFAULT 'legacy_human',
    ADD CONSTRAINT job_message_routes_effective_audience_check CHECK (
        effective_audience IN (
            'legacy_human', 'human', 'officer', 'officer_and_user',
            'explicit_recipient'
        )
    );

UPDATE public.job_message_routes
   SET routing_generation = route_id,
       effective_audience = CASE policy_snapshot->>'applied'
           WHEN 'officer_first' THEN 'officer'
           WHEN 'officer_and_user' THEN 'officer_and_user'
           ELSE 'human'
       END
 WHERE routing_generation IS NULL;

ALTER TABLE public.job_message_routes
    ALTER COLUMN routing_generation SET NOT NULL,
    ALTER COLUMN routing_generation SET DEFAULT gen_random_uuid();

CREATE TABLE public.message_delivery_intents (
    intent_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    routing_generation UUID NOT NULL,
    route_id UUID,
    job_id UUID REFERENCES public.jobs(id) ON DELETE SET NULL,
    project_id UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    user_id UUID REFERENCES public.users(id) ON DELETE SET NULL,
    bucket TEXT NOT NULL CHECK (bucket IN ('human', 'officer_internal')),
    effective_audience TEXT NOT NULL CHECK (
        effective_audience IN (
            'legacy_human', 'human', 'officer', 'officer_and_user',
            'explicit_recipient'
        )
    ),
    state TEXT NOT NULL DEFAULT 'reserved' CHECK (
        state IN ('reserved', 'attempted', 'accepted', 'failed')
    ),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempted_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    last_failed_at TIMESTAMPTZ,
    failure_class TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    UNIQUE (routing_generation, bucket)
);

COMMENT ON TABLE public.message_delivery_intents IS
    'OC-07 durable quota reservation and effective-audience identity. One '
    'row per routing generation and bucket; quota is reserved before any '
    'non-idempotent delivery and retries reuse this identity.';
COMMENT ON COLUMN public.message_delivery_intents.effective_audience IS
    'Server-resolved durable audience. Quota meaning is never reconstructed '
    'from message_log.direction.';

CREATE TABLE public.message_delivery_attempts (
    attempt_id BIGSERIAL PRIMARY KEY,
    intent_id UUID NOT NULL REFERENCES public.message_delivery_intents(intent_id)
        ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    state TEXT NOT NULL DEFAULT 'attempted' CHECK (
        state IN ('attempted', 'accepted', 'failed')
    ),
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ,
    failure_class TEXT,
    detail TEXT,
    UNIQUE (intent_id, attempt_number)
);

CREATE INDEX idx_message_delivery_human_job_reserved
    ON public.message_delivery_intents (job_id, reserved_at DESC)
    WHERE bucket = 'human';
CREATE INDEX idx_message_delivery_human_user_reserved
    ON public.message_delivery_intents (user_id, reserved_at DESC)
    WHERE bucket = 'human';
CREATE INDEX idx_message_delivery_internal_job_reserved
    ON public.message_delivery_intents (job_id, reserved_at DESC)
    WHERE bucket = 'officer_internal';
CREATE INDEX idx_message_delivery_internal_project_reserved
    ON public.message_delivery_intents (project_id, reserved_at DESC)
    WHERE bucket = 'officer_internal';

-- Preserve current-window accounting across the cutover. Each historical
-- outbound row becomes one conservative human intent; no delivery is replayed.
INSERT INTO public.message_delivery_intents (
    routing_generation, job_id, project_id, user_id, bucket,
    effective_audience, state, reserved_at, accepted_at, metadata
)
SELECT ml.routing_generation,
       ml.job_id,
       j.project_id,
       ml.user_id,
       'human',
       'legacy_human',
       CASE WHEN ml.status IN ('sent', 'delivered') THEN 'accepted' ELSE 'failed' END,
       ml.created_at,
       CASE WHEN ml.status IN ('sent', 'delivered') THEN ml.created_at ELSE NULL END,
       jsonb_build_object('cutover_backfill', true, 'message_id', ml.id)
  FROM public.message_log ml
  LEFT JOIN public.jobs j ON j.id = ml.job_id
 WHERE ml.direction = 'outbound'
   AND ml.status <> 'rate_limited'
ON CONFLICT (routing_generation, bucket) DO NOTHING;

CREATE OR REPLACE FUNCTION public.mirror_legacy_message_delivery_intent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.direction = 'outbound'
       AND NEW.status <> 'rate_limited'
       AND NEW.effective_audience = 'legacy_human' THEN
        INSERT INTO public.message_delivery_intents (
            routing_generation, job_id, project_id, user_id, bucket,
            effective_audience, state, reserved_at, accepted_at, metadata
        )
        SELECT NEW.routing_generation,
               NEW.job_id,
               j.project_id,
               NEW.user_id,
               'human',
               'legacy_human',
               CASE WHEN NEW.status IN ('sent', 'delivered')
                    THEN 'accepted' ELSE 'failed' END,
               NEW.created_at,
               CASE WHEN NEW.status IN ('sent', 'delivered')
                    THEN NEW.created_at ELSE NULL END,
               jsonb_build_object('legacy_replica', true, 'message_id', NEW.id)
          FROM (SELECT 1) AS one
          LEFT JOIN public.jobs j ON j.id = NEW.job_id
        ON CONFLICT (routing_generation, bucket) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER mirror_legacy_message_delivery_intent
AFTER INSERT ON public.message_log
FOR EACH ROW EXECUTE FUNCTION public.mirror_legacy_message_delivery_intent();

COMMIT;
