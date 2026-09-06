-- migration:     0026_job_vector_retirement.sql
-- description:   Serialize job-owned vector inserts with permanent retirement.
--                The destination tombstone survives application-row deletion,
--                fencing delayed producers, including previous agent images.
-- depends-on:    0025_knowledge_multi_angle_search.sql
-- expected:      < 5s. New empty ledger, functions and six write triggers.
-- locks:         Brief SHARE ROW EXCLUSIVE for trigger installation; no data
--                backfill. Runtime retirement locks one scope and its owned rows.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';

CREATE TABLE public.job_vector_scopes (
    job_id UUID PRIMARY KEY,
    retired_at TIMESTAMPTZ
);

COMMENT ON TABLE public.job_vector_scopes IS
    'Destination write fence for the existing job_id scope. Session UUIDs also '
    'use this column and remain active: only permanent job deletion retires a '
    'scope. No cross-database FK or expiry; late writers must see the tombstone.';

CREATE FUNCTION public.protect_job_vector_scope_retirement()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Vector scope fences cannot be removed'
            USING ERRCODE='23514', CONSTRAINT='job_vector_scope_retirement_immutable';
    END IF;
    IF NEW.job_id IS DISTINCT FROM OLD.job_id
       OR (OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at) THEN
        RAISE EXCEPTION 'Vector scope retirement cannot be changed'
            USING ERRCODE='23514', CONSTRAINT='job_vector_scope_retirement_immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_vector_scope_retirement_immutable
BEFORE UPDATE OR DELETE ON public.job_vector_scopes
FOR EACH ROW EXECUTE FUNCTION public.protect_job_vector_scope_retirement();

CREATE FUNCTION public.require_active_job_vector_scope()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    retired TIMESTAMPTZ;
BEGIN
    INSERT INTO public.job_vector_scopes (job_id) VALUES (NEW.job_id)
        ON CONFLICT (job_id) DO NOTHING;
    -- FOR SHARE conflicts with the retirement's non-key UPDATE. Hold it until
    -- the producer transaction commits, including any retrieval-message rows.
    SELECT retired_at INTO retired FROM public.job_vector_scopes
     WHERE job_id=NEW.job_id FOR SHARE;
    IF retired IS NOT NULL THEN
        RAISE EXCEPTION 'The job vector scope is permanently retired'
            USING ERRCODE='23514', CONSTRAINT='job_vector_scope_retired';
    END IF;
    RETURN NEW;
END;
$$;

-- Ordinary updates cannot recreate deleted rows and are drained by DELETE's
-- row locks. Guard insertion and scope transfer, including legacy SQL writers.
CREATE TRIGGER memories_job_scope BEFORE INSERT OR UPDATE OF job_id ON public.memories
FOR EACH ROW EXECUTE FUNCTION public.require_active_job_vector_scope();
CREATE TRIGGER citations_job_scope BEFORE INSERT OR UPDATE OF job_id ON public.citations
FOR EACH ROW EXECUTE FUNCTION public.require_active_job_vector_scope();
CREATE TRIGGER annotations_job_scope BEFORE INSERT OR UPDATE OF job_id ON public.source_annotations
FOR EACH ROW EXECUTE FUNCTION public.require_active_job_vector_scope();
CREATE TRIGGER tags_job_scope BEFORE INSERT OR UPDATE OF job_id ON public.source_tags
FOR EACH ROW EXECUTE FUNCTION public.require_active_job_vector_scope();
CREATE TRIGGER embeddings_job_scope BEFORE INSERT OR UPDATE OF job_id ON public.source_embeddings
FOR EACH ROW EXECUTE FUNCTION public.require_active_job_vector_scope();
CREATE TRIGGER sources_job_scope BEFORE INSERT OR UPDATE OF job_id ON public.job_sources
FOR EACH ROW EXECUTE FUNCTION public.require_active_job_vector_scope();

CREATE FUNCTION public.retire_job_vector_scope(requested_job UUID)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.job_vector_scopes (job_id) VALUES (requested_job)
        ON CONFLICT (job_id) DO NOTHING;
    PERFORM 1 FROM public.job_vector_scopes WHERE job_id=requested_job FOR UPDATE;
    UPDATE public.job_vector_scopes SET retired_at=clock_timestamp()
     WHERE job_id=requested_job AND retired_at IS NULL;

    -- One vector transaction commits the fence and the whole existing API
    -- cleanup set. Retrieval messages cascade. Shared sources, project/session
    -- memories, knowledge and audit retention remain outside this job scope.
    DELETE FROM public.memories WHERE job_id=requested_job;
    DELETE FROM public.citations WHERE job_id=requested_job;
    DELETE FROM public.source_annotations WHERE job_id=requested_job;
    DELETE FROM public.source_tags WHERE job_id=requested_job;
    DELETE FROM public.source_embeddings WHERE job_id=requested_job;
    DELETE FROM public.job_sources WHERE job_id=requested_job;
END;
$$;

COMMIT;
