-- migration:     0177_managed_repository_thread_detach.sql
-- description:   Allow authority-reducing persistent-thread detach while
--                retaining exact managed-repository attachment fences.
-- depends-on:    0176_managed_repository_authorities.sql
-- expected:      < 1s. One function create and one thread-trigger replacement;
--                no historical row rewrite or repository scan.
-- locks:         SHARE ROW EXCLUSIVE briefly while the thread trigger is
--                replaced.
-- transactional: yes

-- Migration 0176 deliberately prevents a thread carrying a historical
-- administrator-bearing URL from crossing an agent-attach boundary before the
-- repository has exact scoped authority.  Its combined trigger also rejected
-- the inverse transition (agent_id -> NULL), which can wedge agent deletion and
-- persistent-pod recycling.  Detaching removes runtime authority; it does not
-- expose the historical bearer to a new runtime.  Keep attach fail-closed and
-- leave the URL untouched until the normal live-key adoption path proves and
-- scrubs it.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE FUNCTION public.enforce_managed_thread_repository_url_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    repo_url TEXT;
    scrubbed_repo_url TEXT;
    repository_name TEXT;
BEGIN
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
       AND OLD.agent_id IS DISTINCT FROM NEW.agent_id
       AND NEW.agent_id IS NOT NULL THEN
        -- A historical bearer may never cross into a new runtime.  The
        -- agent_id -> NULL transition is intentionally allowed: it only
        -- removes runtime authority and lets lifecycle cleanup proceed.
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

DROP TRIGGER trg_managed_thread_repository_url_authority ON public.threads;

CREATE TRIGGER trg_managed_thread_repository_url_authority
BEFORE INSERT OR UPDATE OF metadata, agent_id
ON public.threads
FOR EACH ROW EXECUTE FUNCTION
    public.enforce_managed_thread_repository_url_authority();

COMMENT ON FUNCTION public.enforce_managed_thread_repository_url_authority() IS
    'Rolling-upgrade thread fence: fail-closed agent attachment requires exact '
    'scoped authority, while agent detachment remains available for lifecycle '
    'cleanup of historical credential-bearing rows.';

COMMIT;
