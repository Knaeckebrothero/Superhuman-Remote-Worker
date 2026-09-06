-- migration:     0176_managed_repository_authorities.sql
-- description:   Store encrypted, repository-scoped Gitea deploy-key authority
--                and fence credential-bearing managed repository URLs.
-- depends-on:    0175_job_workspace_contract_dispatch_fence.sql
-- expected:      < 1s. Two empty tables, indexes, functions, and triggers; no
--                historical row rewrite or repository scan.
-- locks:         SHARE ROW EXCLUSIVE briefly while URL/dispatch triggers are
--                installed on jobs, threads, and project_repositories.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE public.managed_repository_creation_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    authority_kind TEXT NOT NULL,
    authority_id UUID NOT NULL,
    project_id UUID,
    access_mode TEXT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1,
    intent_marker UUID NOT NULL DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'pending',
    failure_class TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    repository_created_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managed_repository_creation_kind_check
        CHECK (authority_kind IN ('job', 'thread', 'project_repository')),
    CONSTRAINT managed_repository_creation_access_mode_check
        CHECK (access_mode IN ('none', 'read', 'write')),
    CONSTRAINT managed_repository_creation_status_check
        CHECK (status IN (
            'pending', 'created', 'deleting', 'deleted', 'conflicted'
        )),
    CONSTRAINT managed_repository_creation_generation_check
        CHECK (generation > 0),
    CONSTRAINT managed_repository_creation_repo_name_check
        CHECK (repo_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    CONSTRAINT managed_repository_creation_owner_check
        CHECK (repository_owner ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    CONSTRAINT managed_repository_creation_generation_unique
        UNIQUE (repository_owner, repo_name, generation),
    CONSTRAINT managed_repository_creation_marker_unique UNIQUE (intent_marker)
);

CREATE UNIQUE INDEX uq_managed_repository_creation_live_repo
    ON public.managed_repository_creation_intents (repository_owner, repo_name)
    WHERE status IN ('pending', 'created', 'deleting');

CREATE UNIQUE INDEX uq_managed_repository_creation_live_scope
    ON public.managed_repository_creation_intents (
        authority_kind, authority_id, repository_owner, repo_name
    )
    WHERE status IN ('pending', 'created', 'deleting');

CREATE TABLE public.managed_repository_authorities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    authority_kind TEXT NOT NULL,
    authority_id UUID NOT NULL,
    project_id UUID,
    access_mode TEXT NOT NULL,
    creation_intent_id UUID REFERENCES
        public.managed_repository_creation_intents(id) ON DELETE RESTRICT,
    generation BIGINT NOT NULL DEFAULT 1,
    clean_repo_url TEXT NOT NULL,
    public_key TEXT NOT NULL,
    public_key_fingerprint TEXT NOT NULL,
    private_key_ciphertext TEXT NOT NULL,
    forge_key_id BIGINT,
    status TEXT NOT NULL DEFAULT 'provisioning',
    failure_class TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managed_repository_authority_kind_check
        CHECK (authority_kind IN ('job', 'thread', 'project_repository')),
    CONSTRAINT managed_repository_authority_access_mode_check
        CHECK (access_mode IN ('read', 'write')),
    CONSTRAINT managed_repository_authority_status_check
        CHECK (status IN (
            'provisioning', 'active', 'revoking', 'revoked', 'failed'
        )),
    CONSTRAINT managed_repository_authority_generation_check
        CHECK (generation > 0),
    CONSTRAINT managed_repository_authority_ciphertext_check
        CHECK (private_key_ciphertext LIKE 'v1:%'),
    CONSTRAINT managed_repository_authority_repo_name_check
        CHECK (repo_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    CONSTRAINT managed_repository_authority_owner_check
        CHECK (repository_owner ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    CONSTRAINT managed_repository_authority_url_check
        CHECK (
            clean_repo_url !~
                '^[A-Za-z][A-Za-z0-9+.-]*://[^/@[:space:]]+@'
            AND clean_repo_url !~ '^[^/[:space:]]+@[^:]+:'
        ),
    CONSTRAINT managed_repository_authority_generation_unique
        UNIQUE (repository_owner, repo_name, generation)
);

CREATE UNIQUE INDEX uq_managed_repository_authority_live_repo
    ON public.managed_repository_authorities (repository_owner, repo_name)
    WHERE status IN ('provisioning', 'active', 'revoking');

CREATE UNIQUE INDEX uq_managed_repository_authority_live_scope
    ON public.managed_repository_authorities (
        authority_kind, authority_id, repository_owner, repo_name
    )
    WHERE status IN ('provisioning', 'active', 'revoking');

CREATE INDEX idx_managed_repository_authority_scope
    ON public.managed_repository_authorities (authority_kind, authority_id);

CREATE INDEX idx_managed_repository_authority_creation_intent
    ON public.managed_repository_authorities (creation_intent_id)
    WHERE creation_intent_id IS NOT NULL;

CREATE FUNCTION public.managed_repository_url_has_userinfo(value TEXT)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(
        value ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/@[:space:]]+@'
        OR value ~ '^[^/[:space:]]+@[^:]+:',
        FALSE
    )
$$;

CREATE FUNCTION public.managed_repository_url_without_userinfo(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN value ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/@[:space:]]+@'
        THEN regexp_replace(
            value,
            '^([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]+@',
            '\1'
        )
        ELSE NULL
    END
$$;

CREATE FUNCTION public.managed_repository_json_has_private_authority(value JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(
        jsonb_path_exists(value, '$.**."managed_repository_credentials"')
        OR jsonb_path_exists(value, '$.**."managed_repository_authority"')
        OR jsonb_path_exists(value, '$.**."repository_auth"')
        OR jsonb_path_exists(value, '$.**."repository_credentials"'),
        FALSE
    )
$$;

CREATE FUNCTION public.enforce_managed_repository_url_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    repo_url TEXT;
    scrubbed_repo_url TEXT;
    repository_name TEXT;
    root_job_id UUID;
BEGIN
    IF TG_TABLE_NAME = 'project_repositories' THEN
        repo_url := NEW.repo_url;
        repository_name := NEW.name;
        IF TG_OP = 'UPDATE'
           AND OLD.is_managed
           AND OLD.name IS DISTINCT FROM NEW.name THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_identity_is_immutable',
                MESSAGE = 'Managed repository identity may not be replaced',
                HINT = 'Create a new managed repository instead.';
        END IF;
        IF TG_OP = 'UPDATE'
           AND NEW.is_managed
           AND NEW.role <> 'knowledge'
           AND (
               OLD.read_only IS DISTINCT FROM NEW.read_only
               OR OLD.role IS DISTINCT FROM NEW.role
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.managed_repository_authorities AS authority
                WHERE authority.authority_kind = 'project_repository'
                  AND authority.authority_id = NEW.id
                  AND authority.project_id = NEW.project_id
                  AND authority.repo_name = NEW.name
                  AND authority.status = 'active'
                  AND authority.access_mode = CASE
                      WHEN NEW.role = 'reference' OR NEW.read_only
                      THEN 'read'
                      ELSE 'write'
                  END
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_access_mode_requires_authority',
                MESSAGE = 'Managed repository access mode is not active',
                HINT = 'Rotate scoped authority before changing repository access.';
        END IF;
        IF NEW.is_managed
           AND NEW.credentials IS NOT NULL
           AND NEW.credentials <> '{}'::jsonb THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_credentials_are_server_owned',
                MESSAGE = 'Managed repository credentials may not be stored here';
        END IF;
        IF NEW.is_managed
           AND public.managed_repository_url_has_userinfo(repo_url) THEN
            scrubbed_repo_url :=
                public.managed_repository_url_without_userinfo(repo_url);
            IF scrubbed_repo_url IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'managed_repository_url_must_be_credential_free',
                    MESSAGE = 'Managed repository URLs may not contain userinfo',
                    HINT = 'Retry from a repository-authority-aware orchestrator.';
            END IF;
            -- Rolling-upgrade bridge for the immediately previous release:
            -- its managed-repository creator writes an administrator-bearing
            -- HTTP URL. Store only the credential-free identity; dispatch by a
            -- new replica still proves a scoped deploy key first. Project
            -- repository credentials remain empty and cannot smuggle the old
            -- bearer through a second column.
            NEW.repo_url := scrubbed_repo_url;
            NEW.credentials := '{}'::jsonb;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'jobs' THEN
        repo_url := COALESCE(NEW.context, '{}'::jsonb)->>'git_remote_url';
        repository_name := NEW.repo_name;
        IF public.managed_repository_json_has_private_authority(NEW.context)
           OR public.managed_repository_json_has_private_authority(
                  NEW.config_override
              )
           OR public.managed_repository_json_has_private_authority(
                  NEW.resolved_config
              ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_credentials_are_server_owned',
                MESSAGE = 'Managed repository credentials are server-owned';
        END IF;
        IF public.managed_repository_url_has_userinfo(repo_url)
           AND (
               TG_OP = 'INSERT'
               OR repo_url IS DISTINCT FROM
                    COALESCE(OLD.context, '{}'::jsonb)->>'git_remote_url'
           ) THEN
            scrubbed_repo_url :=
                public.managed_repository_url_without_userinfo(repo_url);
            IF scrubbed_repo_url IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'managed_repository_url_must_be_credential_free',
                    MESSAGE = 'Managed repository URLs may not contain userinfo',
                    HINT = 'Retry from a repository-authority-aware orchestrator.';
            END IF;
            -- Old replicas write the URL before repo_name in a second
            -- statement. Keep an explicit pending fence so a dispatcher can
            -- never claim the row in that gap. The exact authority binder
            -- removes this marker only after live key proof.
            NEW.context := jsonb_set(
                jsonb_set(
                    COALESCE(NEW.context, '{}'::jsonb),
                    '{git_remote_url}',
                    to_jsonb(scrubbed_repo_url),
                    true
                ),
                '{_managed_repository_authority_pending}',
                'true'::jsonb,
                true
            );
            repo_url := scrubbed_repo_url;
        ELSIF public.managed_repository_url_has_userinfo(repo_url)
           AND TG_OP = 'UPDATE'
           AND NEW.status = 'processing'
           AND (
               OLD.status IS DISTINCT FROM NEW.status
               OR OLD.assigned_agent_id IS DISTINCT FROM NEW.assigned_agent_id
               OR OLD.lease_expires_at IS DISTINCT FROM NEW.lease_expires_at
           ) THEN
            -- Historical rows are permitted to remain readable until adopted,
            -- but an old bearer may never cross a new lease/dispatch boundary.
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_url_must_be_credential_free',
                MESSAGE = 'Managed repository URLs may not contain userinfo',
                HINT = 'Adopt the repository before dispatch.';
        END IF;
        IF NEW.status = 'processing'
           AND (
               repository_name IS NOT NULL
               OR COALESCE(
                      NEW.context->>'_managed_repository_authority_pending',
                      'false'
                  ) = 'true'
           )
           AND (
               TG_OP = 'INSERT'
               OR OLD.status IS DISTINCT FROM NEW.status
               OR OLD.assigned_agent_id IS DISTINCT FROM NEW.assigned_agent_id
               OR OLD.lease_expires_at IS DISTINCT FROM NEW.lease_expires_at
               OR OLD.repo_name IS DISTINCT FROM NEW.repo_name
               OR COALESCE(OLD.context, '{}'::jsonb)->>'git_remote_url'
                    IS DISTINCT FROM repo_url
           )
           THEN
            WITH RECURSIVE lineage AS (
                SELECT NEW.id AS id, NEW.parent_job_id AS parent_job_id
                UNION ALL
                SELECT parent.id, parent.parent_job_id
                  FROM public.jobs AS parent
                  JOIN lineage ON parent.id = lineage.parent_job_id
            )
            SELECT id INTO root_job_id
              FROM lineage
             WHERE parent_job_id IS NULL
             LIMIT 1;
            IF NOT EXISTS (
                SELECT 1
                  FROM public.managed_repository_authorities AS authority
                 WHERE authority.repo_name = repository_name
                   AND authority.status = 'active'
                   AND authority.access_mode = 'write'
                   AND authority.clean_repo_url = repo_url
                   AND (
                       (
                           authority.authority_kind = 'job'
                           AND authority.authority_id = root_job_id
                       )
                       OR (
                           authority.authority_kind = 'project_repository'
                           AND EXISTS (
                               SELECT 1
                                 FROM public.project_repositories AS repository
                                WHERE repository.id = authority.authority_id
                                  AND repository.project_id = NEW.project_id
                                  AND repository.name = repository_name
                                  AND repository.is_managed
                                  AND repository.role = 'jobs'
                                  AND NOT repository.read_only
                           )
                       )
                   )
            ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_dispatch_requires_repository_authority',
                MESSAGE = 'Managed repository authority is not active',
                HINT = 'Adopt or provision the repository before dispatch.';
            END IF;
        END IF;
        IF NEW.status = 'processing'
           AND NEW.project_id IS NOT NULL
           AND (
               TG_OP = 'INSERT'
               OR OLD.status IS DISTINCT FROM NEW.status
               OR OLD.assigned_agent_id IS DISTINCT FROM NEW.assigned_agent_id
               OR OLD.lease_expires_at IS DISTINCT FROM NEW.lease_expires_at
           )
           AND EXISTS (
               SELECT 1
                 FROM public.project_repositories AS repository
                WHERE repository.project_id = NEW.project_id
                  AND repository.is_managed
                  AND repository.role <> 'knowledge'
                  AND (
                      repository.role <> 'jobs'
                      OR (
                          NEW.repo_name IS NULL
                          AND NEW.branch_name IS NOT NULL
                      )
                  )
                  AND (
                      public.managed_repository_url_has_userinfo(
                          repository.repo_url
                      )
                      OR NOT EXISTS (
                          SELECT 1
                            FROM public.managed_repository_authorities AS authority
                           WHERE authority.authority_kind = 'project_repository'
                             AND authority.authority_id = repository.id
                             AND authority.project_id = repository.project_id
                             AND authority.repo_name = repository.name
                             AND authority.clean_repo_url = repository.repo_url
                             AND authority.status = 'active'
                             AND authority.access_mode = CASE
                                 WHEN repository.role = 'reference'
                                      OR repository.read_only
                                 THEN 'read'
                                 ELSE 'write'
                             END
                      )
                  )
           ) THEN
            -- Old orchestrators do not carry the scoped-key runtime bundle and
            -- omit the is_managed marker. Block them at the authoritative claim
            -- boundary until a new replica has proven and adopted every managed
            -- source/reference row the workspace would clone, plus the exact
            -- shared jobs row used as the primary remote by a pre-0176 job
            -- that has project_id + branch_name but no per-job repo_name.
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_dispatch_requires_project_repository_authority',
                MESSAGE = 'Managed project repository authority is not active',
                HINT = 'Adopt project repository authority before dispatch.';
        END IF;
        RETURN NEW;
    END IF;

    repo_url := COALESCE(
        NEW.metadata, '{}'::jsonb
    )->'workspace_container'->>'git_remote_url';
    repository_name := COALESCE(
        NEW.metadata, '{}'::jsonb
    )->'workspace_container'->>'repo_name';
    IF public.managed_repository_json_has_private_authority(NEW.metadata) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_credentials_are_server_owned',
            MESSAGE = 'Managed repository credentials are server-owned';
    END IF;
    IF public.managed_repository_url_has_userinfo(repo_url)
       AND (
           TG_OP = 'INSERT'
           OR repo_url IS DISTINCT FROM COALESCE(
               OLD.metadata, '{}'::jsonb
           )->'workspace_container'->>'git_remote_url'
       ) THEN
        scrubbed_repo_url :=
            public.managed_repository_url_without_userinfo(repo_url);
        IF scrubbed_repo_url IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_url_must_be_credential_free',
                MESSAGE = 'Managed repository URLs may not contain userinfo',
                HINT = 'Retry from a repository-authority-aware orchestrator.';
        END IF;
        NEW.metadata := jsonb_set(
            jsonb_set(
                COALESCE(NEW.metadata, '{}'::jsonb),
                '{workspace_container,git_remote_url}',
                to_jsonb(scrubbed_repo_url),
                true
            ),
            '{workspace_container,_managed_repository_authority_pending}',
            'true'::jsonb,
            true
        );
        repo_url := scrubbed_repo_url;
    ELSIF public.managed_repository_url_has_userinfo(repo_url)
       AND TG_OP = 'UPDATE'
       AND OLD.agent_id IS DISTINCT FROM NEW.agent_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_url_must_be_credential_free',
            MESSAGE = 'Managed repository URLs may not contain userinfo',
            HINT = 'Adopt the repository before attaching.';
    END IF;
    IF NEW.agent_id IS NOT NULL
       AND (
           repository_name IS NOT NULL
           OR COALESCE(
                  NEW.metadata->'workspace_container'
                      ->>'_managed_repository_authority_pending',
                  'false'
              ) = 'true'
       )
       AND (
           TG_OP = 'INSERT'
           OR OLD.agent_id IS DISTINCT FROM NEW.agent_id
           OR COALESCE(
                  OLD.metadata, '{}'::jsonb
              )->'workspace_container'->>'repo_name'
                IS DISTINCT FROM repository_name
           OR COALESCE(
                  OLD.metadata, '{}'::jsonb
              )->'workspace_container'->>'git_remote_url'
                IS DISTINCT FROM repo_url
       )
       AND NOT EXISTS (
           SELECT 1
             FROM public.managed_repository_authorities AS authority
            WHERE authority.repo_name = repository_name
              AND authority.status = 'active'
              AND authority.access_mode = 'write'
              AND authority.clean_repo_url = repo_url
              AND authority.authority_kind = 'thread'
              AND authority.authority_id = NEW.id
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'thread_attach_requires_repository_authority',
            MESSAGE = 'Managed repository authority is not active',
            HINT = 'Adopt or provision the repository before attaching.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.enforce_managed_repository_owner_cleanup()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    owner_kind TEXT;
BEGIN
    owner_kind := CASE TG_TABLE_NAME
        WHEN 'jobs' THEN 'job'
        WHEN 'threads' THEN 'thread'
        WHEN 'project_repositories' THEN 'project_repository'
        ELSE NULL
    END;
    IF owner_kind IS NULL THEN
        RAISE EXCEPTION 'Unsupported managed repository owner table';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.managed_repository_authorities AS authority
         WHERE authority.authority_kind = owner_kind
           AND authority.authority_id = OLD.id
           AND authority.status IN ('provisioning', 'active', 'revoking')
    ) OR EXISTS (
        SELECT 1 FROM public.managed_repository_creation_intents AS intent
         WHERE intent.authority_kind = owner_kind
           AND intent.authority_id = OLD.id
           AND intent.status IN ('pending', 'created', 'deleting')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_cleanup_required',
            MESSAGE = 'Managed repository authority must be contained first',
            HINT = 'Use the server-owned repository cleanup path and retry.';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_managed_project_repository_url_authority
BEFORE INSERT OR UPDATE OF
    name, repo_url, credentials, is_managed, role, read_only
ON public.project_repositories
FOR EACH ROW EXECUTE FUNCTION public.enforce_managed_repository_url_authority();

CREATE TRIGGER trg_managed_job_repository_url_authority
BEFORE INSERT OR UPDATE OF
    context, config_override, resolved_config, status, assigned_agent_id,
    lease_expires_at, repo_name
ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_managed_repository_url_authority();

CREATE TRIGGER trg_managed_thread_repository_url_authority
BEFORE INSERT OR UPDATE OF metadata, agent_id
ON public.threads
FOR EACH ROW EXECUTE FUNCTION public.enforce_managed_repository_url_authority();

CREATE TRIGGER trg_managed_project_repository_cleanup
BEFORE DELETE ON public.project_repositories
FOR EACH ROW EXECUTE FUNCTION public.enforce_managed_repository_owner_cleanup();

CREATE TRIGGER trg_managed_job_repository_cleanup
BEFORE DELETE ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_managed_repository_owner_cleanup();

CREATE TRIGGER trg_managed_thread_repository_cleanup
BEFORE DELETE ON public.threads
FOR EACH ROW EXECUTE FUNCTION public.enforce_managed_repository_owner_cleanup();

COMMENT ON TABLE public.managed_repository_authorities IS
    'Server-owned encrypted per-repository Gitea deploy-key authority. Private '
    'material is decrypted only for an exact job/thread workspace delivery; '
    'ordinary repository/job/thread projections never join this table.';

COMMENT ON TABLE public.managed_repository_creation_intents IS
    'Durable exact-scope repository creation identity. The random marker is '
    'written to Gitea metadata before a 409/lost response may be adopted.';

COMMENT ON COLUMN public.managed_repository_authorities.private_key_ciphertext IS
    'AES-GCM ciphertext produced with APP_ENCRYPTION_KEY; plaintext is never '
    'written to PostgreSQL.';

COMMENT ON FUNCTION public.enforce_managed_repository_url_authority() IS
    'Rolling-upgrade fence: old writers cannot persist administrator-bearing '
    'managed URLs or dispatch/bind a managed repo without active scoped authority; '
    'legacy HTTP writes are stripped and held pending exact key proof.';

COMMENT ON FUNCTION public.enforce_managed_repository_owner_cleanup() IS
    'Fail-closed rolling fence: an old/direct owner delete cannot orphan a live '
    'repository creation intent or deploy-key authority.';

COMMIT;
