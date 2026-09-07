-- Reconcile the dedicated Dynamic Canvas gateway login on the legacy bundled
-- StatefulSet or through the explicit external-operator workflow.
--
-- CloudNativePG installations must never run this file: CNPG's DatabaseRole
-- owns role identity, attributes, and password there. Object ACLs live in
-- canvas-viewer-grants.sql and are applied by the database owner.
--
-- Inputs arrive through the environment and are copied into psql variables so
-- the password never appears in argv or script output.
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
SET LOCAL search_path = pg_catalog, pg_temp;
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
  -- Every omitted attribute is a PostgreSQL safe default. Naming negative
  -- SUPERUSER/CREATEDB/REPLICATION/BYPASSRLS clauses is unnecessary and makes
  -- ALTER ROLE require those issuer attributes even when no value changes.
  CREATE ROLE :"canvas_role" WITH LOGIN NOINHERIT CONNECTION LIMIT -1
    PASSWORD :'canvas_password';
\endif

-- Existing roles passed the elevated-attribute and membership checks above.
-- Keep the credential only in PostgreSQL's password-aware statement: wrapping
-- it in SELECT format(...) would expose it to ordinary statement logging.
ALTER ROLE :"canvas_role" WITH LOGIN NOINHERIT CONNECTION LIMIT -1
  PASSWORD :'canvas_password';
SELECT format(
    'ALTER ROLE %I IN DATABASE %I '
    'SET search_path = pg_catalog, public, pg_temp',
    :'canvas_role', current_database()
)
\gexec

COMMIT;
