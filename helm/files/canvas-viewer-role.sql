-- Reconcile the dedicated Dynamic Canvas gateway login.
--
-- Inputs arrive through the environment and are copied into psql variables so
-- the password never appears in argv or script output. Run this only as the
-- owner/admin of the chart-owned application database. External PostgreSQL
-- operators should provision the same grants with their own control plane.
\set ON_ERROR_STOP on
\set QUIET on
\set ECHO none
\getenv canvas_role CANVAS_VIEWER_POSTGRES_USER
\getenv canvas_password CANVAS_VIEWER_POSTGRES_PASSWORD

SELECT :'canvas_role' ~ '^[a-z_][a-z0-9_]{0,62}$' AS canvas_role_valid
\gset
\if :canvas_role_valid
\else
  \warn 'Canvas gateway database role name is invalid'
  \quit 1
\endif
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SELECT pg_advisory_xact_lock(
    hashtextextended('srw:canvas-gateway-role', 0)
) AS canvas_role_lock
\gset

SELECT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'canvas_role'
) AS canvas_role_exists
\gset

\if :canvas_role_exists
  SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication
             OR rolbypassrls AS canvas_role_unsafe
  FROM pg_roles
  WHERE rolname = :'canvas_role'
  \gset
  \if :canvas_role_unsafe
    \warn 'Refusing to reconcile an elevated Canvas gateway database role'
    \quit 1
  \endif

  SELECT EXISTS (
      SELECT 1
      FROM pg_auth_members membership
      JOIN pg_roles canvas_role ON canvas_role.oid = (
          SELECT oid FROM pg_roles WHERE rolname = :'canvas_role'
      )
      WHERE membership.member = canvas_role.oid
         OR membership.roleid = canvas_role.oid
  ) AS canvas_role_has_membership
  \gset
  \if :canvas_role_has_membership
    \warn 'Refusing to reconcile a Canvas gateway role with role memberships'
    \quit 1
  \endif

  SELECT EXISTS (
      SELECT 1
      FROM pg_shdepend dependency
      JOIN pg_roles canvas_role ON canvas_role.oid = dependency.refobjid
      WHERE dependency.refclassid = 'pg_authid'::regclass
        AND canvas_role.rolname = :'canvas_role'
        AND dependency.deptype = 'o'
  ) AS canvas_role_owns_objects
  \gset
  \if :canvas_role_owns_objects
    \warn 'Refusing to reconcile a Canvas gateway role which owns database objects'
    \quit 1
  \endif
\else
  CREATE ROLE :"canvas_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'canvas_password';
\endif

-- Password rotation and safe attributes are idempotent. Keep the credential
-- only in PostgreSQL's password-aware CREATE/ALTER ROLE statements: wrapping
-- it in SELECT format(...) would expose it to ordinary statement logging.
-- ECHO is disabled above so psql cannot print the expanded statement locally.
ALTER ROLE :"canvas_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'canvas_password';
SELECT format(
    'ALTER ROLE %I IN DATABASE %I SET search_path = pg_catalog, public',
    :'canvas_role', current_database()
)
\gexec

-- Remove direct privileges from an earlier/experimental role contract before
-- installing the exact allowlist. Column ACLs are independent from table ACLs
-- and therefore need their own generated REVOKE statements.
SELECT format(
    'REVOKE %s (%s) ON TABLE %I.%I FROM %I',
    privilege_type,
    string_agg(format('%I', column_name), ', ' ORDER BY ordinal_position),
    table_schema,
    table_name,
    :'canvas_role'
)
FROM information_schema.column_privileges privileges
JOIN information_schema.columns columns
  USING (table_catalog, table_schema, table_name, column_name)
WHERE grantee = :'canvas_role'
GROUP BY table_schema, table_name, privilege_type
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %I',
    table_schema, table_name, :'canvas_role'
)
FROM information_schema.table_privileges
WHERE grantee = :'canvas_role'
GROUP BY table_schema, table_name
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM %I',
    sequence_schema, sequence_name, :'canvas_role'
)
FROM information_schema.sequences
WHERE has_sequence_privilege(
    :'canvas_role', format('%I.%I', sequence_schema, sequence_name), 'USAGE'
) OR has_sequence_privilege(
    :'canvas_role', format('%I.%I', sequence_schema, sequence_name), 'SELECT'
) OR has_sequence_privilege(
    :'canvas_role', format('%I.%I', sequence_schema, sequence_name), 'UPDATE'
)
\gexec
-- Clear direct database grants from an earlier or operator-created role before
-- restoring only CONNECT.  TEMP inherited from PostgreSQL's PUBLIC role is
-- intentionally outside the application-schema contract, but CREATE is not.
SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I',
    current_database(), :'canvas_role'
)
\gexec
SELECT format('REVOKE ALL PRIVILEGES ON SCHEMA %I FROM %I', nspname, :'canvas_role')
FROM pg_namespace
WHERE has_schema_privilege(:'canvas_role', oid, 'CREATE')
   OR has_schema_privilege(:'canvas_role', oid, 'USAGE')
\gexec

-- CREATE granted through PUBLIC cannot be denied for only this role. Refuse
-- such a database instead of claiming a least-privilege boundary. PostgreSQL
-- 15's default public schema already satisfies this condition.
SELECT has_schema_privilege(:'canvas_role', 'public', 'CREATE')
       AS canvas_role_can_create
\gset
\if :canvas_role_can_create
  \warn 'Canvas gateway role inherits CREATE on schema public; revoke it from PUBLIC first'
  \quit 1
\endif

SELECT has_database_privilege(
    :'canvas_role', current_database(), 'CREATE'
) AS canvas_role_can_create_database
\gset
\if :canvas_role_can_create_database
  \warn 'Canvas gateway role inherits CREATE on the application database; revoke it from PUBLIC first'
  \quit 1
\endif

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'canvas_role')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'canvas_role')
\gexec

SELECT format(
    'GRANT SELECT (id, is_admin, is_approved) ON TABLE public.users TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, user_id, metadata) ON TABLE public.threads TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, user_id, absolute_expires_at, revoked_at) '
    'ON TABLE public.srw_sessions TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (thread_id, canvas_id, source, title, renderer, editable, '
    'alt_text, presentation_revision, source_fingerprint, source_version, '
    'origin_generation, created_at, updated_at) '
    'ON TABLE public.canvases TO %I',
    :'canvas_role'
)
\gexec

SELECT format(
    'GRANT SELECT (id, user_id, thread_id, canvas_id, parent_srw_session_id, '
    'embedding_origin, cookie_mode, expires_at, closed_at), '
    'UPDATE (origin_session_id, last_seen_at) '
    'ON TABLE public.canvas_view_attachments TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, attachment_id, expected_presentation_revision, '
    'source_fingerprint, workspace_generation, origin_generation, expires_at, '
    'challenge_hash, browser_binding_hash, ready_receipt_hash, '
    'exchange_token_hash, authorized_at, consumed_at), '
    'UPDATE (challenge_hash, browser_binding_hash, ready_receipt_hash, '
    'consumed_at, consumed_origin_session_id) '
    'ON TABLE public.canvas_view_bootstraps TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, session_secret_hash, user_id, thread_id, canvas_id, '
    'parent_srw_session_id, source_fingerprint, workspace_generation, '
    'origin_generation, embedding_origin, cookie_mode, expires_at, revoked_at), '
    'INSERT (id, session_secret_hash, user_id, thread_id, canvas_id, '
    'parent_srw_session_id, issued_presentation_revision, source_fingerprint, '
    'workspace_generation, origin_generation, embedding_origin, cookie_mode, '
    'expires_at), UPDATE (expires_at, last_renewed_at, revoked_at, '
    'revocation_reason, updated_at) '
    'ON TABLE public.canvas_origin_sessions TO %I',
    :'canvas_role'
)
\gexec

COMMIT;
