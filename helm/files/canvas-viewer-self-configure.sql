-- Run only while authenticated as the restricted Canvas gateway login. An
-- ordinary role may set its own per-database defaults without CREATEROLE.
\set ON_ERROR_STOP on
\set QUIET on
\set ECHO none
\getenv canvas_role CANVAS_VIEWER_POSTGRES_USER

SELECT current_user = :'canvas_role' AND session_user = current_user
       AS canvas_identity_matches
\gset
\if :canvas_identity_matches
\else
  \warn 'Canvas self-configuration must authenticate as the restricted login'
  \quit 1
\endif

BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT format(
    'ALTER ROLE CURRENT_USER IN DATABASE %I '
    'SET search_path = pg_catalog, public, pg_temp',
    current_database()
)
\gexec
COMMIT;
