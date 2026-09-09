-- Preserve the location of a new exact Kubernetes cleanup capture. Existing
-- captures remain unknown: today's namespace must not be invented as history.
ALTER TABLE public.managed_repository_workspace_cleanup_intents
    ADD COLUMN resource_location JSONB;

ALTER TABLE public.managed_repository_workspace_cleanup_intents
    ADD CONSTRAINT workspace_cleanup_resource_location_shape CHECK (
        resource_location IS NULL OR (
            scope = 'workspace_container'
            AND capture_complete
            AND resources_captured_at IS NOT NULL
            AND jsonb_typeof(resource_location) = 'object'
            AND resource_location ?& ARRAY['namespace','pod','seedConfigMap','pvc','service']
            AND (resource_location - ARRAY['namespace','pod','seedConfigMap','pvc','service']) = '{}'::jsonb
            AND jsonb_typeof(resource_location->'namespace') = 'string'
            AND length(resource_location->>'namespace') BETWEEN 1 AND 63
            AND resource_location->>'namespace' ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
            AND jsonb_typeof(resource_location->'pod') = 'string'
            AND length(resource_location->>'pod') BETWEEN 1 AND 253
            AND resource_location->>'pod' ~ '^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$'
            AND jsonb_typeof(resource_location->'seedConfigMap') = 'string'
            AND length(resource_location->>'seedConfigMap') BETWEEN 1 AND 253
            AND resource_location->>'seedConfigMap' ~ '^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$'
            AND jsonb_typeof(resource_location->'pvc') = 'string'
            AND length(resource_location->>'pvc') BETWEEN 1 AND 253
            AND resource_location->>'pvc' ~ '^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$'
            AND jsonb_typeof(resource_location->'service') = 'string'
            AND length(resource_location->>'service') BETWEEN 1 AND 253
            AND resource_location->>'service' ~ '^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$'
        )
    );

CREATE FUNCTION public.protect_workspace_cleanup_capture_location()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.resource_location IS NOT NULL AND (
        NEW.resource_location IS DISTINCT FROM OLD.resource_location
        OR NEW.runtime_incarnation IS DISTINCT FROM OLD.runtime_incarnation
        OR NEW.pod_uid IS DISTINCT FROM OLD.pod_uid
        OR NEW.seed_configmap_uid IS DISTINCT FROM OLD.seed_configmap_uid
        OR NEW.pvc_uid IS DISTINCT FROM OLD.pvc_uid
        OR NEW.service_uid IS DISTINCT FROM OLD.service_uid
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            CONSTRAINT = 'workspace_cleanup_capture_location_immutable',
            MESSAGE = 'Captured workspace resource locations and UIDs are immutable';
    END IF;
    IF OLD.capture_complete AND OLD.resource_location IS NULL
       AND NEW.resource_location IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            CONSTRAINT = 'workspace_cleanup_capture_location_no_backfill',
            MESSAGE = 'A completed historical capture cannot acquire inferred resource locations';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_workspace_cleanup_protect_capture_location
BEFORE UPDATE ON public.managed_repository_workspace_cleanup_intents
FOR EACH ROW EXECUTE FUNCTION public.protect_workspace_cleanup_capture_location();

COMMENT ON COLUMN public.managed_repository_workspace_cleanup_intents.resource_location IS
    'Namespace and immutable Kubernetes resource names observed with this UID capture; NULL historical captures remain unproven.';
