-- Fail closed before granting the Canvas gateway login access to application
-- objects. This file is intentionally owner-only: it contains no role-creation
-- or password operation and is valid for both CNPG and the legacy engine.
\set ON_ERROR_STOP on
\set QUIET on
\set ECHO none
\getenv canvas_role CANVAS_VIEWER_POSTGRES_USER

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
\else
  \warn 'Canvas gateway role does not exist; CNPG must reconcile DatabaseRole first'
  \quit 1
\endif

SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication
           OR rolbypassrls OR NOT rolcanlogin OR rolinherit
           OR rolconnlimit <> -1 AS canvas_role_unsafe
FROM pg_roles
WHERE rolname = :'canvas_role'
\gset
\if :canvas_role_unsafe
  \warn 'Refusing to grant access to a Canvas gateway role outside the safe identity contract'
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
  \warn 'Refusing to grant access to a Canvas gateway role with role memberships'
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
  \warn 'Refusing to grant access to a Canvas gateway role which owns database objects'
  \quit 1
\endif

SELECT datdba = (SELECT oid FROM pg_roles WHERE rolname = current_user)
       AS caller_owns_database
FROM pg_database
WHERE datname = current_database()
\gset
\if :caller_owns_database
\else
  \warn 'Canvas grant reconciler must run as the application database owner'
  \quit 1
\endif

WITH expected(table_name) AS (
    VALUES ('users'), ('threads'), ('srw_sessions'), ('canvases'),
           ('canvas_view_attachments'), ('canvas_view_bootstraps'),
           ('canvas_origin_sessions')
), owned AS (
    SELECT expected.table_name,
           relation.oid IS NOT NULL
             AND relation.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
             AS caller_owns_relation
    FROM expected
    LEFT JOIN pg_namespace namespace ON namespace.nspname = 'public'
    LEFT JOIN pg_class relation
      ON relation.relnamespace = namespace.oid
     AND relation.relname = expected.table_name
     AND relation.relkind IN ('r', 'p')
)
SELECT count(*) = 7 AND bool_and(caller_owns_relation)
       AS caller_owns_required_relations
FROM owned
\gset
\if :caller_owns_required_relations
\else
  \warn 'Canvas grant reconciler requires all seven migrated relations to exist and be owned by the application role'
  \quit 1
\endif

-- Direct stale CREATE grants can be removed by the grant reconciler. CREATE
-- inherited through PUBLIC cannot be denied for only this role, so distinguish
-- the two and refuse the database-wide case.
SELECT has_schema_privilege(:'canvas_role', 'public', 'CREATE')
       AND NOT EXISTS (
           SELECT 1
           FROM pg_namespace namespace
           CROSS JOIN LATERAL aclexplode(
               COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
           ) privilege
           JOIN pg_roles grantee ON grantee.oid = privilege.grantee
           WHERE namespace.nspname = 'public'
             AND grantee.rolname = :'canvas_role'
             AND privilege.privilege_type = 'CREATE'
       ) AS canvas_role_inherits_create
\gset
\if :canvas_role_inherits_create
  \warn 'Canvas gateway role inherits CREATE on schema public; revoke it from PUBLIC first'
  \quit 1
\endif

SELECT has_database_privilege(
    :'canvas_role', current_database(), 'CREATE'
) AND NOT EXISTS (
    SELECT 1
    FROM pg_database database
    CROSS JOIN LATERAL aclexplode(
        COALESCE(database.datacl, acldefault('d', database.datdba))
    ) privilege
    JOIN pg_roles grantee ON grantee.oid = privilege.grantee
    WHERE database.datname = current_database()
      AND grantee.rolname = :'canvas_role'
      AND privilege.privilege_type = 'CREATE'
) AS canvas_role_inherits_create_database
\gset
\if :canvas_role_inherits_create_database
  \warn 'Canvas gateway role inherits CREATE on the application database; revoke it from PUBLIC first'
  \quit 1
\endif

-- Commit CONNECT before the restricted-login probe. This lets the next phase
-- prove CNPG has applied LOGIN and the expected password before table grants.
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'canvas_role')
\gexec
COMMIT;
